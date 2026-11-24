"""批量任务调度：队列、并发执行、取消、重试与结果复用。

一个“任务”对应一个待破解的加密文件。任务按批次加入，批次内所有任务共享同一
套 :class:`zr.config.Settings`（攻击模式、字典、掩码等）。调度器用固定数量的
工作线程消费队列，因此天然支持批量处理。

单个任务的执行流程
------------------
1. **计算文件哈希**：得到文件内容指纹（SHA-256）。
2. **查历史结果**：若 ``password_consumer.csv`` 中已存在相同哈希的记录，
   直接复用其中的密码，写入一行新记录后结束，**不再执行耗时的破解流程**。
3. **准备工具资源**：按需下载/复用 John、7-Zip、Perl（有缓存则秒过）。
4. **提取密码哈希**：调用对应的 ``*2john`` 提取器。
5. **运行 John**：按字典 / 掩码 / 增量模式破解，实时汇报进度。
6. **记录结果**：成功则追加 CSV；失败则标记为“未找到”。

并发安全
--------
内容相同的两个文件（哈希一致）共用同一个任务工作目录，因此调度器为每个哈希
维护一把锁：同一份内容在同一时刻只允许一个任务执行，后到的任务会等待并在
前者成功后直接复用结果。
"""

from __future__ import annotations

import json
import queue
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from . import config, hashers, hashing
from .engine import AttackResult, JohnEngine
from .results import ResultStore


class JobStatus(str, Enum):
    """任务状态枚举（取值同时作为界面上的状态标识）。"""

    QUEUED = "queued"
    FINGERPRINTING = "fingerprinting"
    EXTRACTING = "extracting"
    RUNNING = "running"
    REUSED = "reused"
    DONE = "done"
    NOT_FOUND = "not_found"
    NOT_PROTECTED = "not_protected"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: 视为“已结束”的状态集合。
FINISHED_STATUSES = frozenset(
    {
        JobStatus.REUSED,
        JobStatus.DONE,
        JobStatus.NOT_FOUND,
        JobStatus.NOT_PROTECTED,
        JobStatus.UNSUPPORTED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }
)

#: 视为“成功”的状态集合（会在 CSV 中留下记录）。
SUCCESS_STATUSES = frozenset({JobStatus.REUSED, JobStatus.DONE})


@dataclass
class Job:
    """一个待破解文件的完整状态。

    属性:
        id: 短随机标识，用于界面操作。
        path: 文件完整路径。
        size: 文件字节数。
        mtime: 文件修改时间（秒）。
        settings: 本批次共享的攻击配置。
        status: 当前 :class:`JobStatus`。
        message: 面向用户的中文状态说明。
        progress: 0-100 的进度百分比。
        password: 恢复出的密码（若有）。
        file_hash: 文件内容哈希（SHA-256）。
        hash_kind: 哈希家族（zip/rar/7z/pdf/office…）。
        log: 最近的 John 输出行。
        cancel_event: 置位后请求中止本任务。
        created_at / started_at / finished_at: 时间戳。
    """

    id: str
    path: str
    size: int
    mtime: float
    settings: config.Settings
    status: JobStatus = JobStatus.QUEUED
    message: str = "排队中"
    progress: int = 0
    password: Optional[str] = None
    file_hash: str = ""
    hash_kind: Optional[str] = None
    log: List[str] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    @property
    def name(self) -> str:
        """文件名（不含目录）。"""
        return Path(self.path).name

    @property
    def extension(self) -> str:
        """小写扩展名（含点）。"""
        return Path(self.path).suffix.lower()

    def to_dict(self, log_lines: int = 30) -> Dict:
        """返回供网页界面使用的字典表示。

        参数:
            log_lines: 携带的日志行数（只取最后若干行以控制响应体积）。
        """
        return {
            "id": self.id,
            "path": self.path,
            "name": self.name,
            "size": self.size,
            "status": self.status.value,
            "message": self.message,
            "progress": self.progress,
            "password": self.password,
            "file_hash": self.file_hash,
            "hash_kind": self.hash_kind,
            "attack": self.settings.attack,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log": self.log[-log_lines:],
        }


class JobManager:
    """任务队列与工作线程池。

    参数:
        engine: John 引擎实例。
        results: CSV 结果存储实例。
        on_update: 可选回调，任务状态变化时以 :class:`Job` 调用。
    """

    def __init__(
        self,
        engine: JohnEngine,
        results: ResultStore,
        on_update: Optional[Callable[[Job], None]] = None,
    ) -> None:
        self.engine = engine
        self.results = results
        self.on_update = on_update
        self._lock = threading.RLock()
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._queue: "queue.Queue[Job]" = queue.Queue()
        self._workers: List[threading.Thread] = []
        # 初始为 0 个线程；第一次 add() 时按 settings.workers 创建。
        self._worker_count = 0
        self._stopping = threading.Event()
        self._hash_locks: Dict[str, threading.Lock] = {}
        # 进程内结果缓存：键为 (文件哈希, 攻击配置签名)。
        self._attempts: Dict[str, Optional[str]] = {}

    # ------------------------------------------------------------------
    # 队列管理
    # ------------------------------------------------------------------

    def add(self, paths: Iterable[str], settings: config.Settings) -> Dict[str, List]:
        """把一批文件加入任务队列。

        参数:
            paths: 文件完整路径序列。
            settings: 本批次共享的攻击配置。

        返回:
            ``{"added": [任务字典...], "skipped": [{"path":..., "reason":...}]}``。
        """
        settings = settings.normalized()
        self._ensure_workers(settings.workers)

        added: List[Dict] = []
        skipped: List[Dict] = []
        with self._lock:
            for raw in paths:
                path = str(Path(str(raw).strip().strip('"')).expanduser())
                if not path:
                    continue
                file_path = Path(path)
                if not file_path.is_file():
                    skipped.append({"path": path, "reason": "文件不存在或不是普通文件"})
                    continue
                if hashers.tool_for(path) is None:
                    skipped.append(
                        {
                            "path": path,
                            "reason": f"不支持的文件类型（支持：{hashers.describe_supported()}）",
                        }
                    )
                    continue
                if any(
                    job.path.lower() == path.lower() and job.status not in FINISHED_STATUSES
                    for job in self._jobs.values()
                ):
                    skipped.append({"path": path, "reason": "已在队列中"})
                    continue

                job = Job(
                    id=uuid.uuid4().hex[:8],
                    path=path,
                    size=file_path.stat().st_size,
                    mtime=file_path.stat().st_mtime,
                    settings=settings,
                )
                self._jobs[job.id] = job
                self._order.append(job.id)
                self._queue.put(job)
                added.append(job.to_dict())
        self._notify_many(added)
        return {"added": added, "skipped": skipped}

    def cancel(self, job_id: str) -> bool:
        """请求取消一个任务。

        排队中的任务会立即标记为已取消；运行中的任务会通知 John 保存断点后
        退出。返回 ``True`` 表示请求已受理。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.cancel_event.set()
            if job.status == JobStatus.QUEUED:
                job.status = JobStatus.CANCELLED
                job.message = "已取消（未开始）"
                job.finished_at = time.time()
        self._notify(job)
        return True

    def retry(self, job_id: str) -> bool:
        """把已结束的任务重新加入队列。

        会重置状态、密码、进度与日志，但保留文件哈希以便复用历史结果。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in FINISHED_STATUSES:
                return False
            job.status = JobStatus.QUEUED
            job.message = "排队中"
            job.progress = 0
            job.password = None
            job.log = []
            job.cancel_event = threading.Event()
            job.started_at = None
            job.finished_at = None
            # 用户主动重试时，清除“本批次已尝试”的记忆，确保真的重跑。
            self._attempts.pop(
                f"{job.file_hash}|{self._settings_signature(job.settings)}", None
            )
            self._queue.put(job)
        self._notify(job)
        return True

    def remove(self, job_id: str) -> bool:
        """移除一个已结束的任务，并删除其工作目录。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in FINISHED_STATUSES:
                return False
            del self._jobs[job_id]
            if job_id in self._order:
                self._order.remove(job_id)
        self._cleanup_workdir(job)
        return True

    def clear_finished(self) -> int:
        """移除所有已结束的任务（并清理其工作目录），返回移除数量。"""
        removed: List[Job] = []
        with self._lock:
            for job_id in list(self._order):
                job = self._jobs.get(job_id)
                if job and job.status in FINISHED_STATUSES:
                    del self._jobs[job_id]
                    self._order.remove(job_id)
                    removed.append(job)
        for job in removed:
            self._cleanup_workdir(job)
        return len(removed)

    def shutdown(self) -> None:
        """停止所有工作线程并请求运行中的任务中止。"""
        self._stopping.set()
        with self._lock:
            for job in self._jobs.values():
                if job.status not in FINISHED_STATUSES:
                    job.cancel_event.set()
        for _ in self._workers:
            self._queue.put(None)  # type: ignore[arg-type]
        for worker in self._workers:
            worker.join(timeout=5)

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def snapshot(self) -> List[Dict]:
        """返回全部任务的字典列表（按加入顺序）。"""
        with self._lock:
            return [self._jobs[job_id].to_dict() for job_id in self._order if job_id in self._jobs]

    def stats(self) -> Dict[str, int]:
        """返回按状态汇总的任务数量。"""
        counts: Dict[str, int] = {}
        with self._lock:
            for job in self._jobs.values():
                key = job.status.value
                counts[key] = counts.get(key, 0) + 1
        counts["total"] = len(self._jobs)
        return counts

    # ------------------------------------------------------------------
    # 工作线程
    # ------------------------------------------------------------------

    def _ensure_workers(self, workers: int) -> None:
        """按需扩充工作线程数量（只会增加，不会减少）。"""
        with self._lock:
            workers = max(1, min(config.MAX_WORKERS, workers))
            if workers <= self._worker_count:
                return
            for index in range(self._worker_count, workers):
                thread = threading.Thread(
                    target=self._worker_loop,
                    name=f"zr-worker-{index}",
                    daemon=True,
                )
                thread.start()
                self._workers.append(thread)
            self._worker_count = workers

    def _worker_loop(self) -> None:
        """工作线程主循环：从队列取任务并执行，直到收到停止信号。"""
        while not self._stopping.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                break
            if job.status == JobStatus.CANCELLED:
                self._queue.task_done()
                continue
            try:
                self._execute(job)
            except Exception as exc:  # 兜底：任何异常都要落到任务状态里
                with self._lock:
                    job.status = JobStatus.FAILED
                    job.message = f"任务异常：{exc}"
                    job.finished_at = time.time()
                self._notify(job)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------
    # 单个任务执行
    # ------------------------------------------------------------------

    def _execute(self, job: Job) -> None:
        """执行一个任务的完整流程（哈希 → 复用查询 → 提取 → 破解 → 记录）。"""
        job.started_at = time.time()
        self._set(job, JobStatus.FINGERPRINTING, "计算文件哈希…", 0)

        # 1) 计算文件内容哈希
        try:
            file_hash = hashing.file_hash(
                job.path,
                on_progress=lambda done, total: self._set_progress(
                    job, int(done * 100 / total) if total else 0
                ),
                cancel_event=job.cancel_event,
            )
        except InterruptedError:
            self._set(job, JobStatus.CANCELLED, "已取消（计算哈希阶段）", job.progress)
            return
        except Exception as exc:
            self._set(job, JobStatus.FAILED, f"计算文件哈希失败：{exc}", job.progress)
            return

        job.file_hash = file_hash
        self._set(job, JobStatus.FINGERPRINTING, "文件哈希已计算，准备破解…", 100)

        # 2) 同一份内容串行执行，避免两个相同文件同时占用同一个工作目录。
        lock = self._hash_lock(file_hash)
        with lock:
            if self._reuse_or_skip(job):
                return
            self._run_pipeline(job)

    def _reuse_or_skip(self, job: Job) -> bool:
        """尝试复用历史结果或本批次已有的失败结论。

        参数:
            job: 当前任务（已计算出 ``file_hash``）。

        返回:
            ``True`` 表示任务已处理完毕，调用方应直接返回。
        """
        # 2a) 桌面 CSV 中已有相同哈希的记录 → 直接复用密码
        cached = self.results.lookup_with_time(job.file_hash)
        if cached:
            job.password = cached["password"]
            row = self.results.append(job.path, job.password, job.file_hash)
            self._set(
                job,
                JobStatus.REUSED,
                f"命中历史结果（相同文件哈希，记录于 {cached['time']}），已跳过破解",
                100,
            )
            job.message = (
                f"命中历史结果（相同文件哈希，记录于 {cached['time']}），已跳过破解；"
                f"本次记录时间 {row['完成时间']}"
            )
            return True

        # 2b) 本批次中已尝试过相同内容且同样配置 → 不重复跑
        signature = self._settings_signature(job.settings)
        key = f"{job.file_hash}|{signature}"
        if key in self._attempts:
            previous = self._attempts[key]
            if previous:
                job.password = previous
                self.results.append(job.path, previous, job.file_hash)
                self._set(
                    job,
                    JobStatus.REUSED,
                    "命中本批次中相同内容的破解结果，已跳过重复破解",
                    100,
                )
            else:
                self._set(
                    job,
                    JobStatus.NOT_FOUND,
                    "本批次中相同内容的文件已用相同配置尝试过且未找到密码，已跳过",
                    100,
                )
            return True
        return False

    def _run_pipeline(self, job: Job) -> None:
        """执行工具准备、哈希提取与 John 破解。

        参数:
            job: 当前任务。
        """
        extension = job.extension

        # 3) 准备工具资源（首次会下载，之后走缓存）
        self._set(job, JobStatus.EXTRACTING, "准备 John / 7-Zip / Perl 资源…", 0)
        try:
            self.engine.prepare([extension])
        except Exception as exc:
            self._fail(job, f"准备工具资源失败：{exc}")
            return

        # 4) 提取密码哈希
        self._set(job, JobStatus.EXTRACTING, "提取密码哈希…", 0)
        result = self.engine.extract(job.path)
        if not result.ok:
            if "未设置密码保护" in result.message:
                self._set(job, JobStatus.NOT_PROTECTED, result.message, 100)
            elif "不支持" in result.message:
                self._set(job, JobStatus.UNSUPPORTED, result.message, 100)
            else:
                self._fail(job, result.message)
            return

        job.hash_kind = result.kind
        workdir = self._workdir(job.file_hash)
        workdir.mkdir(parents=True, exist_ok=True)
        pwhash = workdir / "pwhash"
        lines = result.lines or [result.hash_line]
        pwhash.write_text("\n".join(lines) + "\n", encoding="utf-8", errors="replace")
        logins = [line.split(":", 1)[0] for line in lines if ":" in line]

        # 5) 运行 John
        resume = (workdir / "zr_session.rec").exists()
        self._set(
            job,
            JobStatus.RUNNING,
            "John 正在破解（从断点继续）…" if resume else "John 正在破解…",
            0,
        )
        attack = self.engine.attack(
            pwhash=pwhash,
            workdir=workdir,
            settings=job.settings,
            kind=job.hash_kind,
            on_event=lambda name, payload: self._on_engine_event(job, name, payload),
            cancel_event=job.cancel_event,
            resume=resume,
            logins=logins,
        )
        self._finish_attack(job, attack)

    def _finish_attack(self, job: Job, attack: AttackResult) -> None:
        """根据引擎返回的攻击结果收尾任务。

        参数:
            job: 当前任务。
            attack: 引擎返回的 :class:`AttackResult`。
        """
        signature = self._settings_signature(job.settings)
        key = f"{job.file_hash}|{signature}"
        if attack.password:
            row = self.results.append(job.path, attack.password, job.file_hash)
            job.password = attack.password
            self._attempts[key] = attack.password
            self._set(
                job,
                JobStatus.DONE,
                f"密码已恢复并写入 CSV（{row['完成时间']}）",
                100,
            )
            return

        # 只有真正“穷尽”了字典/掩码才记下失败结论；取消/超时/出错不算，
        # 否则用户点击“重试”时会被错误地跳过。
        if attack.status == "exhausted":
            self._attempts[key] = None
        if attack.status == "cancelled":
            self._set(job, JobStatus.CANCELLED, attack.message or "已取消", attack.progress)
        elif attack.status == "timeout":
            self._set(job, JobStatus.NOT_FOUND, attack.message or "超时", attack.progress)
        elif attack.status == "error":
            self._fail(job, attack.message or "John 运行失败")
        else:
            self._set(job, JobStatus.NOT_FOUND, attack.message or "未找到密码", attack.progress)
    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _on_engine_event(self, job: Job, name: str, payload: object) -> None:
        """把引擎事件转换为任务状态更新。"""
        if name == "progress":
            self._set_progress(job, int(payload))  # type: ignore[arg-type]
        elif name == "log":
            with self._lock:
                job.log.append(str(payload))
                del job.log[:-200]
            self._notify(job)
        elif name == "password":
            with self._lock:
                job.password = str(payload)
            self._notify(job)

    def _set(self, job: Job, status: JobStatus, message: str, progress: Optional[int] = None) -> None:
        """更新任务状态并通知观察者。"""
        with self._lock:
            job.status = status
            job.message = message
            if progress is not None:
                job.progress = max(0, min(100, progress))
            if status in FINISHED_STATUSES and job.finished_at is None:
                job.finished_at = time.time()
        self._notify(job)

    def _set_progress(self, job: Job, value: int) -> None:
        """更新任务进度（只在数值变化时通知，减少界面刷新压力）。"""
        value = max(0, min(100, value))
        with self._lock:
            if job.progress == value:
                return
            job.progress = value
        self._notify(job)

    def _fail(self, job: Job, message: str) -> None:
        """把任务标记为失败。"""
        self._set(job, JobStatus.FAILED, message, job.progress)

    def _notify(self, job: Job) -> None:
        """调用外部观察者（异常不影响调度）。"""
        if self.on_update:
            try:
                self.on_update(job)
            except Exception:
                pass

    def _notify_many(self, jobs: List[Dict]) -> None:
        """批量加入任务后统一通知一次（避免逐个刷屏）。"""
        if not self.on_update:
            return
        with self._lock:
            for item in jobs:
                job = self._jobs.get(item["id"])
                if job:
                    self._notify(job)

    def _hash_lock(self, file_hash: str) -> threading.Lock:
        """返回内容哈希对应的互斥锁。"""
        with self._lock:
            lock = self._hash_locks.get(file_hash)
            if lock is None:
                lock = threading.Lock()
                self._hash_locks[file_hash] = lock
            return lock

    def _workdir(self, file_hash: str) -> Path:
        """返回某个文件内容对应的任务工作目录。"""
        return config.jobs_root() / (file_hash[:32] or "unknown")

    def _cleanup_workdir(self, job: Job) -> None:
        """删除任务的工作目录（若存在且没有其他任务共用该目录）。

        工作目录按“文件内容哈希”命名，因此内容相同的多个文件会共用同一个
        目录；只要还有任务在使用，就不能删除。
        """
        if not job.file_hash:
            return
        with self._lock:
            shared = any(
                other.file_hash == job.file_hash and other.id != job.id
                for other in self._jobs.values()
            )
        if shared:
            return
        path = self._workdir(job.file_hash)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _settings_signature(settings: config.Settings) -> str:
        """生成只包含“影响破解结果”的配置签名。

        用于判断两次尝试是否等价：文件哈希相同且配置签名相同，则失败结论可以
        复用，不必再跑一遍。
        """
        payload = {
            "attack": settings.attack,
            "wordlist": settings.wordlist,
            "rules": settings.use_rules,
            "mask": settings.mask,
            "charset": settings.custom_charset,
            "gpu": settings.use_gpu,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
