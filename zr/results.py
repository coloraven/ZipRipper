"""破解结果存储：桌面上的 ``password_consumer.csv``。

每成功恢复一个密码，就向桌面上的 CSV 追加一行，列定义为：

============  ==========================================
列            含义
============  ==========================================
完成时间       任务结束的本地时间（``YYYY-MM-DD HH:MM:SS``）
加密文件完整路径 被处理的加密文件绝对路径
恢复出的密码     John 恢复出的明文密码
文件哈希(SHA-256) 文件内容的 SHA-256，用于跨文件复用结果
============  ==========================================

跨文件复用
----------
文件被复制/改名/移动后内容哈希不变，因此可以通过哈希列直接取回历史密码，
跳过耗时的哈希提取与破解流程。:meth:`ResultStore.lookup` 负责在内存中建立
“哈希 → 密码”索引，并在 CSV 文件被外部修改后自动重新加载。

兼容性
------
早期版本只有三列。若检测到旧格式文件，会自动补齐哈希列并重写表头，
保证新旧数据可以在同一份文件中共存。
"""

from __future__ import annotations

import csv
import io
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import config


class ResultStore:
    """线程安全的 CSV 结果存储，支持按文件哈希查询历史密码。

    参数:
        path: CSV 文件路径，默认为桌面上的 :data:`config.CSV_FILENAME`。
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else config.csv_path()
        self._lock = threading.RLock()
        self._index: Dict[str, str] = {}
        self._index_mtime: Optional[float] = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate_if_needed()

    # ------------------------------------------------------------------
    # 读取 / 迁移
    # ------------------------------------------------------------------

    def _migrate_if_needed(self) -> None:
        """把旧的三列 CSV 迁移为四列格式（保留全部历史数据）。"""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.reader(handle))
            except Exception:
                return
            if not rows:
                return
            header = rows[0]
            if list(header) == list(config.CSV_HEADER):
                return
            # 只在列数不足时迁移，避免破坏用户自定义的表头。
            if len(header) >= len(config.CSV_HEADER):
                return
            migrated: List[List[str]] = [list(config.CSV_HEADER)]
            for row in rows[1:]:
                padded = list(row) + [""] * (len(config.CSV_HEADER) - len(row))
                migrated.append(padded[: len(config.CSV_HEADER)])
            self._rewrite(migrated)

    def _rewrite(self, rows: Iterable[Iterable[str]]) -> None:
        """以 UTF-8-SIG 编码整体重写 CSV（Excel 可直接打开中文表头）。"""
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        for row in rows:
            writer.writerow(row)
        with open(self.path, "w", encoding="utf-8-sig", newline="") as handle:
            handle.write(buffer.getvalue())
        self._index_mtime = None

    def _ensure_header(self) -> None:
        """文件不存在或为空时写入表头。"""
        if self.path.exists() and self.path.stat().st_size > 0:
            return
        with open(self.path, "w", encoding="utf-8-sig", newline="") as handle:
            csv.writer(handle).writerow(config.CSV_HEADER)
        self._index_mtime = None

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def append(self, file_path: str, password: str, file_hash: str, when: Optional[str] = None) -> Dict[str, str]:
        """追加一条破解成功记录。

        参数:
            file_path: 加密文件完整路径。
            password: 恢复出的密码。
            file_hash: 文件内容哈希（十六进制）。
            when: 完成时间，缺省为当前本地时间。

        返回:
            写入的行（便于界面直接展示）。
        """
        timestamp = when or time.strftime("%Y-%m-%d %H:%M:%S")
        row = [timestamp, str(Path(file_path)), password, file_hash]
        with self._lock:
            self._ensure_header()
            with open(self.path, "a", encoding="utf-8-sig", newline="") as handle:
                csv.writer(handle).writerow(row)
            # 同步更新内存索引，避免下一次查询重新读盘。
            if file_hash:
                self._index[file_hash.lower()] = password
            self._index_mtime = self.path.stat().st_mtime
        return {
            "完成时间": timestamp,
            "加密文件完整路径": row[1],
            "恢复出的密码": password,
            "文件哈希(SHA-256)": file_hash,
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _load_index(self, force: bool = False) -> Dict[str, str]:
        """构建（或刷新）“哈希 → 密码”的内存索引。"""
        if not self.path.exists():
            self._index = {}
            self._index_mtime = None
            return self._index
        mtime = self.path.stat().st_mtime
        if not force and self._index_mtime == mtime:
            return self._index
        index: Dict[str, str] = {}
        try:
            with open(self.path, "r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle)
                rows = list(reader)
        except Exception:
            return self._index
        for row in rows[1:]:
            if len(row) < len(config.CSV_HEADER):
                continue
            file_hash = (row[3] or "").strip().lower()
            password = row[2]
            if file_hash and password:
                index[file_hash] = password
        self._index = index
        self._index_mtime = mtime
        return index

    def lookup(self, file_hash: str) -> Optional[str]:
        """按文件哈希查询历史密码。

        参数:
            file_hash: 文件内容哈希（大小写不敏感）。

        返回:
            之前恢复出的密码；没有记录时返回 ``None``。
        """
        if not file_hash:
            return None
        with self._lock:
            index = self._load_index()
            return index.get(file_hash.lower())

    def lookup_with_time(self, file_hash: str) -> Optional[Dict[str, str]]:
        """按文件哈希查询历史密码及其完成时间。

        参数:
            file_hash: 文件内容哈希。

        返回:
            包含 ``password`` 与 ``time`` 的字典；无记录时返回 ``None``。
        """
        if not file_hash:
            return None
        with self._lock:
            if not self.path.exists():
                return None
            try:
                with open(self.path, "r", encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.reader(handle))
            except Exception:
                return None
            for row in rows[1:]:
                if len(row) >= len(config.CSV_HEADER) and row[3].strip().lower() == file_hash.lower():
                    if row[2]:
                        return {"password": row[2], "time": row[0]}
        return None

    def count(self) -> int:
        """返回 CSV 中已记录的成功条目数量。"""
        with self._lock:
            if not self.path.exists():
                return 0
            try:
                with open(self.path, "r", encoding="utf-8-sig", newline="") as handle:
                    return max(0, len(list(csv.reader(handle))) - 1)
            except Exception:
                return 0
