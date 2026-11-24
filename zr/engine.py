"""John the Ripper 引擎封装。

本模块把“调用 John”这件事封装成一个类，向上层（任务调度器）提供三个能力：

1. :meth:`JohnEngine.prepare` —— 按文件扩展名准备所需的提取器资源；
2. :meth:`JohnEngine.extract` —— 从加密文件提取哈希；
3. :meth:`JohnEngine.attack` —— 用字典 / 掩码 / 增量模式跑 John，并实时汇报进度。

设计要点：

* John 的 ``john.exe`` 是一个启动器，会自动选择最优的 SIMD 版本，因此始终
  通过它来调用；工作目录固定为任务的私有目录，``.rec`` / ``.log`` / ``.pot``
  都落在那里，互不干扰，也天然支持“中断后恢复”。
* 显卡加速通过 ``--format=<xxx>-opencl`` 启用。使用前会先用 ``--show`` 做一次
  廉价的“格式是否接受该哈希”探测，避免在显卡不支持该哈希时白跑一趟；真正
  运行失败时还会自动回退到 CPU 自动识别格式。
* 所有对外文本均为中文，方便阅读日志。
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from . import config, hashers
from .hashers import ExtractionContext, HashResult
from .resources import ResourceManager

#: Windows 上创建新进程组，便于用 CTRL_BREAK 让 John 优雅保存断点。
_CREATE_NEW_PROCESS_GROUP = 0x00000200

#: John 状态行中的百分比，例如 ``0g 0:00:00:12 3.45% (ETA: ...)``。
_PROGRESS_RE = re.compile(r"(\d+\.\d+)%")

#: 破解成功行，例如 ``password123      (secret.docx)``。
_CRACK_RE = re.compile(r"^\s*(?P<pw>.+?)\s+\((?P<name>[^()]*)\)\s*$")

#: 日志中需要忽略的“噪声”行（状态行、提示行等）。
_LOG_NOISE = (
    "p/s",
    "Session completed",
    "Loaded ",
    "Press Ctrl-C",
    "Remaining ",
    "Proceeding with",
    "Use the ",
    "Cost ",
    "Note:",
    "LWS=",
    "Device ",
    "Warning:",
    "No password hashes",
    "Cracked ",
)


@dataclass
class AttackResult:
    """一次破解尝试的结果。

    属性:
        password: 找到的明文密码，未找到时为 ``None``。
        status: ``found`` / ``exhausted`` / ``error`` / ``cancelled`` / ``timeout``。
        message: 面向用户的中文说明。
        progress: 结束时的进度百分比（0-100）。
        log: 最后若干行 John 输出，便于界面展示。
    """

    password: Optional[str] = None
    status: str = "exhausted"
    message: str = ""
    progress: int = 0
    log: List[str] = field(default_factory=list)


class JohnEngine:
    """对 John the Ripper 命令行的高层封装。

    参数:
        resources: 资源管理器，负责下载与缓存 John、7-Zip、Perl。
        python_exe: 执行 ``.py`` 提取器的解释器，默认为当前解释器。
    """

    def __init__(
        self,
        resources: ResourceManager,
        python_exe: Optional[str] = None,
    ) -> None:
        self.resources = resources
        self.python_exe = python_exe or sys.executable
        self._opencl_devices: Optional[List[str]] = None

    # ------------------------------------------------------------------
    # 资源准备
    # ------------------------------------------------------------------

    def prepare(self, extensions: Sequence[str]) -> None:
        """确保处理这些扩展名所需的资源都已就绪。

        参数:
            extensions: 小写扩展名（含点）序列，例如 ``[".zip", ".7z"]``。
        """
        needed: List[str] = []
        for ext in extensions:
            tool = hashers.EXTENSION_MAP.get(ext)
            if tool is None:
                continue
            needed.extend(tool.resources)
        self.resources.ensure_many(needed)

    def extraction_context(self, tool: hashers.HashTool) -> ExtractionContext:
        """为指定提取器构造运行时上下文（按需准备 Perl）。

        参数:
            tool: 要运行的提取器。
        """
        perl_exe = None
        perl_env = None
        if tool.interpreter == "perl":
            perl_exe = self.resources.perl_exe()
            perl_env = self.resources.perl_env()
        return ExtractionContext(
            run_dir=self.resources.jtr_run_dir(),
            python_exe=self.python_exe,
            perl_exe=perl_exe,
            perl_env=perl_env,
            timeout=config.HASH_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # 哈希提取
    # ------------------------------------------------------------------

    def extract(self, path: str) -> HashResult:
        """从加密文件中提取 John 哈希。

        参数:
            path: 加密文件的完整路径。

        返回:
            :class:`hashers.HashResult`；失败时 ``message`` 为中文原因。
        """
        tool = hashers.tool_for(path)
        if tool is None:
            return HashResult(
                ok=False,
                message=f"不支持的文件类型：{Path(path).suffix or '无扩展名'}",
            )
        try:
            self.prepare([Path(path).suffix.lower()])
            ctx = self.extraction_context(tool)
        except Exception as exc:
            return HashResult(ok=False, message=f"准备提取器失败：{exc}")
        return hashers.run_extractor(tool, ctx, path)

    # ------------------------------------------------------------------
    # 显卡探测
    # ------------------------------------------------------------------

    def opencl_devices(self) -> List[str]:
        """返回可用的 OpenCL 设备描述列表（结果会缓存）。

        通过 ``john --list=opencl-devices`` 探测；John 尚未下载时直接返回空
        列表，避免“查询状态”这种只读操作触发 28MB 的下载。
        """
        if self._opencl_devices is not None:
            return self._opencl_devices
        if not self.resources.is_installed("jtr"):
            return []
        devices: List[str] = []
        try:
            run_dir = self.resources.jtr_run_dir()
            completed = subprocess.run(
                [str(run_dir / "john.exe"), "--list=opencl-devices"],
                cwd=str(run_dir),
                capture_output=True,
                text=True,
                timeout=120,
                errors="replace",
            )
            for line in (completed.stdout or "").splitlines():
                stripped = line.strip()
                if stripped.startswith("Device #") or stripped.startswith("Board name"):
                    devices.append(stripped)
        except Exception:
            devices = []
        # 只保留形如 "Device #0 ... name: xxx" 的条目用于界面展示。
        self._opencl_devices = [d for d in devices if d.startswith("Device #")]
        return self._opencl_devices

    def has_opencl(self) -> bool:
        """是否存在可用的 OpenCL 设备。"""
        return bool(self.opencl_devices())

    # ------------------------------------------------------------------
    # 破解
    # ------------------------------------------------------------------

    def attack(
        self,
        pwhash: Path,
        workdir: Path,
        settings: config.Settings,
        kind: Optional[str],
        on_event: Optional[Callable[[str, object], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        resume: bool = False,
        logins: Optional[Sequence[str]] = None,
    ) -> AttackResult:
        """运行 John 尝试破解哈希。

        参数:
            pwhash: 已写入哈希的文件（位于 ``workdir`` 内，可能含多条哈希）。
            workdir: 任务私有目录，John 的会话与 pot 文件都写在这里。
            settings: 本次批次的攻击配置。
            kind: 哈希家族（来自 :func:`hashers.classify_hash`）。
            on_event: 回调，事件名为 ``progress`` / ``log`` / ``password``。
            cancel_event: 置位后请求中止 John。
            resume: 是否从上次中断的会话继续（``.rec`` 存在时生效）。
            logins: 哈希行中的“登录名”列表（即文件名部分），用于从 John 输出
                中准确识别破解成功行，避免把状态行误判为密码。

        返回:
            :class:`AttackResult`。
        """
        session = "zr_session"
        pot = workdir / "john.pot"
        log: List[str] = []
        progress = {"value": 0}
        found = {"password": None}

        def emit(name: str, payload: object) -> None:
            """安全地调用外部回调，回调异常不影响引擎。"""
            if on_event:
                try:
                    on_event(name, payload)
                except Exception:
                    pass

        fmt = self._select_format(settings, kind, pwhash, workdir)

        def handle_line(line: str) -> None:
            """处理 John 的一行输出：记录日志、更新进度、捕获密码。"""
            line = line.rstrip()
            if not line:
                return
            log.append(line)
            del log[:-200]  # 只保留最后 200 行，避免内存无限增长
            emit("log", line)

            match_progress = _PROGRESS_RE.search(line)
            if match_progress:
                value = min(99, int(float(match_progress.group(1))))
                if value > progress["value"]:
                    progress["value"] = value
                    emit("progress", value)

            if not any(token in line for token in _LOG_NOISE):
                match_crack = _CRACK_RE.match(line)
                if match_crack:
                    name = match_crack.group("name").strip()
                    matched = not logins or any(
                        name == login or name.startswith(login) for login in logins
                    )
                    if matched:
                        password = match_crack.group("pw").strip()
                        found["password"] = password
                        emit("password", password)

        def run_once(use_resume: bool):
            """构造参数并执行一次 John，返回 ``(状态, 退出码)``。"""
            args = self._build_args(pwhash, pot, session, settings, fmt, use_resume)
            emit("log", "执行命令: " + " ".join(args))
            return self._spawn(
                args,
                workdir,
                handle_line,
                cancel_event,
                settings.job_timeout,
                progress,
                emit,
            )

        status, returncode = run_once(resume)
        if resume and status == "error":
            # 断点文件可能来自旧版本或已损坏，删掉后从头开始，避免卡死。
            emit("log", f"断点恢复失败（退出码 {returncode}），改为从头开始…")
            for rec in workdir.glob("*.rec"):
                try:
                    rec.unlink()
                except OSError:
                    pass
            progress["value"] = 0
            status, returncode = run_once(False)

        # 进程结束后以 pot 文件为准，避免因输出格式变化而漏掉密码。
        password = self._read_password(pwhash, pot, fmt, workdir)
        if password is None:
            password = found["password"]

        if status == "cancelled":
            message = "任务已取消"
        elif status == "timeout":
            message = f"超过时间上限（{settings.job_timeout} 秒）"
        elif status == "error":
            message = f"John 运行失败（退出码 {returncode}），详见任务日志"
        elif password:
            status = "found"
            message = "密码已恢复"
        else:
            # 正常结束且未找到密码：字典/掩码已穷尽，失败结论可被后续任务复用。
            status = "exhausted"
            message = "字典/掩码已穷尽，未找到密码"

        if password:
            progress["value"] = 100
        emit("progress", progress["value"])
        return AttackResult(
            password=password,
            status=status,
            message=message,
            progress=progress["value"],
            log=log,
        )

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _select_format(
        self,
        settings: config.Settings,
        kind: Optional[str],
        pwhash: Path,
        workdir: Path,
    ) -> Optional[str]:
        """决定是否使用 OpenCL 格式。

        只有在用户允许、存在显卡、且该哈希家族有 OpenCL 格式、并且该格式确实
        能加载这份哈希时才返回格式名；否则返回 ``None``（交给 John 自动识别，
        即 CPU 模式）。
        """
        if not settings.use_gpu:
            return None
        if not self.has_opencl():
            return None
        candidate = hashers.opencl_format(kind)
        if not candidate:
            return None
        if self._format_loads(candidate, pwhash, workdir):
            return candidate
        return None

    def _format_loads(self, fmt: str, pwhash: Path, workdir: Path) -> bool:
        """探测某个格式能否加载给定哈希（通过 ``--show`` 的剩余哈希数判断）。"""
        empty_pot = workdir / "format_probe.pot"
        try:
            completed = subprocess.run(
                [
                    str(self.resources.jtr_run_dir() / "john.exe"),
                    "--show",
                    f"--format={fmt}",
                    f"--pot={empty_pot}",
                    str(pwhash),
                ],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=120,
                errors="replace",
            )
        except Exception:
            return False
        match = re.search(r"(\d+)\s+left", completed.stdout or "")
        return bool(match and int(match.group(1)) > 0)

    def _build_args(
        self,
        pwhash: Path,
        pot: Path,
        session: str,
        settings: config.Settings,
        fmt: Optional[str],
        resume: bool,
    ) -> List[str]:
        """构造 John 命令行参数。

        参数:
            pwhash: 哈希文件路径。
            pot: pot 文件路径。
            session: 会话名。
            settings: 攻击配置。
            fmt: 选定的格式（``None`` 表示自动识别）。
            resume: 是否续跑上次会话。
        """
        john = str(self.resources.jtr_run_dir() / "john.exe")
        if resume:
            # ``--restore`` 会从 ``.rec`` 中读取格式、pot 路径与攻击参数，
            # 不能再附加 ``--pot`` 或哈希文件（否则 John 会报“选项组合非法”）。
            return [john, f"--restore={session}"]

        args = [john, f"--pot={pot}", f"--session={session}"]
        if fmt:
            args.append(f"--format={fmt}")

        if settings.attack == "mask":
            args.append(f"--mask={settings.mask}")
            if settings.custom_charset:
                args.append(f"--1={settings.custom_charset}")
        elif settings.attack == "incremental":
            args.append(f"--incremental={config.DEFAULT_INCREMENTAL}")
        else:
            wordlist = self.resolve_wordlist(settings.wordlist)
            args.append(f"--wordlist={wordlist}")
            if settings.use_rules:
                args.append(f"--rules={config.DEFAULT_RULES}")

        args.append(str(pwhash))
        return args

    def resolve_wordlist(self, custom: str) -> str:
        """把用户选择的字典解析成实际可用的文件路径。

        规则:
            * 留空 → 使用 John 自带的 ``password.lst``；
            * 指向 ``.7z`` → 用缓存的 7-Zip 控制台解包到缓存目录后取其中第一个
              文本文件；
            * 其他情况直接返回该路径（需已存在）。
        """
        if not custom:
            return str(self.resources.jtr_run_dir() / config.DEFAULT_WORDLIST_NAME)

        path = Path(custom)
        if not path.exists():
            raise FileNotFoundError(f"字典文件不存在：{custom}")

        if path.suffix.lower() == ".7z":
            console = self.resources.sevenzip_console()
            target = config.cache_root() / "wordlists" / path.stem
            target.mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                [str(console), "x", "-y", f"-o{target}", str(path)],
                capture_output=True,
                text=True,
                timeout=1800,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"字典解包失败：{(completed.stderr or '').strip()[:200]}"
                )
            candidates = sorted(target.rglob("*.txt")) + sorted(target.rglob("*.lst"))
            if not candidates:
                raise RuntimeError("解包后的字典中找不到 .txt/.lst 文件")
            return str(candidates[0])
        return str(path)

    def _spawn(
        self,
        args: List[str],
        workdir: Path,
        handle_line: Callable[[str], None],
        cancel_event: Optional[threading.Event],
        timeout: int,
        progress: Dict,
        emit: Callable[[str, object], None],
    ):
        """启动 John 并持续读取输出。

        参数:
            args: 命令行参数。
            workdir: 工作目录。
            handle_line: 每行输出的处理函数。
            cancel_event: 取消事件。
            timeout: 超时秒数（0 表示不限制）。
            progress: 进度字典（供超时前汇报）。
            emit: 事件回调。

        返回:
            ``(状态, 退出码)``；状态为 ``finished`` / ``cancelled`` / ``timeout`` /
            ``error``（John 非零退出）。
        """
        creationflags = _CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        proc = subprocess.Popen(
            args,
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            errors="replace",
            creationflags=creationflags,
        )

        def reader() -> None:
            """后台线程：逐行读取 John 输出并交给处理函数。"""
            assert proc.stdout is not None
            for line in proc.stdout:
                try:
                    handle_line(line)
                except Exception:
                    pass

        thread = threading.Thread(target=reader, name="john-reader", daemon=True)
        thread.start()

        deadline = time.time() + timeout if timeout else None
        result = "finished"
        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                result = "cancelled"
                break
            if deadline and time.time() > deadline:
                result = "timeout"
                break
            time.sleep(0.2)

        if result != "finished":
            self._terminate(proc, emit)
        try:
            proc.wait(timeout=30)
        except Exception:
            proc.kill()
        thread.join(timeout=5)
        if result == "finished" and proc.returncode not in (0, None):
            result = "error"
        return result, proc.returncode

    def _terminate(self, proc: subprocess.Popen, emit: Callable[[str, object], None]) -> None:
        """尽量优雅地终止 John，让它可以保存 ``.rec`` 断点，然后确保进程树清空。

        流程：
            1. 发送 CTRL_BREAK（John 会保存会话并输出 “Session aborted”）；
            2. 最多等待 10 秒；
            3. 无论是否退出，都清理整个进程树——因为 ``john.exe`` 只是一个
               启动器，真正的 ``john-avx2-omp.exe`` 是它的子进程，显卡加速时
               收尾阶段可能卡住，留下孤儿进程占住会话文件。
        """
        emit("log", "正在请求 John 保存断点并退出…")
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                proc.terminate()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        self._kill_tree(proc.pid, emit)

    def _kill_tree(self, pid: int, emit: Callable[[str, object], None]) -> None:
        """强制结束指定进程及其子进程。

        参数:
            pid: 启动器进程号。
            emit: 事件回调（仅用于日志）。
        """
        if os.name != "nt":
            return
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=30,
            )
        except Exception:
            pass
        # taskkill /T 有时覆盖不到启动器派生的孙进程；Windows 中子进程的
        # ParentProcessId 不会因父进程退出而改变，所以按父进程号再清理一次。
        script = (
            f"Get-CimInstance Win32_Process -Filter \"ParentProcessId={pid}\" | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        try:
            subprocess.run(
                ["powershell", "-nop", "-c", script],
                capture_output=True,
                timeout=60,
            )
        except Exception:
            emit("log", "清理 John 子进程时出错（可忽略，下次运行会自动处理）")

    def _read_password(
        self,
        pwhash: Path,
        pot: Path,
        fmt: Optional[str],
        workdir: Path,
    ) -> Optional[str]:
        """从 pot 文件读取已破解的密码。

        实现方式：用 ``john --show`` 输出 ``登录名:密码:附加字段`` 形式的行，
        再根据哈希行中的登录名定位密码字段的位置。之所以不能简单地“取第二个
        字段”，是因为：

        * ZIP 的登录名形如 ``包名/内部文件名``，密码后面还跟着若干附加字段；
        * PDF 的登录名可能是带盘符的完整路径（本身含冒号）。

        因此这里用 ``登录名中冒号的数量 + 1`` 作为密码字段的下标，既能处理
        附加字段，也能处理含冒号的路径。

        参数:
            pwhash: 哈希文件路径（可能包含多条哈希）。
            pot: pot 文件路径。
            fmt: 使用的格式（``None`` 表示自动识别）。
            workdir: 工作目录。
        """
        if not pot.exists() or pot.stat().st_size == 0:
            return None

        logins: List[str] = []
        try:
            for raw in pwhash.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if line and ":" in line:
                    logins.append(line.split(":", 1)[0])
        except Exception:
            logins = []

        args = [
            str(self.resources.jtr_run_dir() / "john.exe"),
            "--show",
            f"--pot={pot}",
        ]
        if fmt:
            args.append(f"--format={fmt}")
        args.append(str(pwhash))
        try:
            completed = subprocess.run(
                args,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=120,
                errors="replace",
            )
        except Exception:
            return None

        for line in (completed.stdout or "").splitlines():
            line = line.strip()
            if not line or "password hash" in line:
                continue
            fields = line.split(":")
            for login in logins:
                index = login.count(":") + 1
                if len(fields) > index and ":".join(fields[:index]) == login:
                    password = fields[index]
                    if password:
                        return password
            # 登录名对不上时退回常见形态 ``登录名:密码``。
            if len(fields) >= 2 and fields[1]:
                return fields[1]
        return None
