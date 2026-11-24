"""文件内容哈希计算工具。

本模块的唯一职责是计算加密文件的“内容指纹”，用于跨文件复用历史破解结果：

* 同一个文件被复制、改名、移动到别的目录后，只要内容相同，哈希就相同，
  程序便可以直接从 ``password_consumer.csv`` 中取回之前恢复出的密码，
  跳过耗时的哈希提取与 John 破解流程。
* 采用流式读取，内存占用恒定，可处理数 GB 的大文件。
* 支持进度回调与取消事件，便于网页界面显示“计算哈希”进度并允许中途取消。

默认算法为 SHA-256（见 :data:`config.HASH_ALGORITHM`）；如需更快的 MD5，
只需修改配置中的算法名，本模块会自动适配。
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Callable, Optional

from . import config

#: 支持的哈希算法名到 hashlib 构造函数的映射。
_ALGORITHMS = {
    "sha256": hashlib.sha256,
    "sha1": hashlib.sha1,
    "md5": hashlib.md5,
}


def new_hasher(algorithm: Optional[str] = None):
    """创建一个 hashlib 哈希对象。

    参数:
        algorithm: 算法名，默认使用 :data:`config.HASH_ALGORITHM`。

    返回:
        已初始化的 hashlib 对象。
    """
    name = (algorithm or config.HASH_ALGORITHM).lower()
    factory = _ALGORITHMS.get(name)
    if factory is None:
        raise ValueError(f"不支持的哈希算法: {name}")
    return factory()


def file_hash(
    path: str,
    algorithm: Optional[str] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> str:
    """计算文件内容哈希并返回十六进制字符串。

    参数:
        path: 文件完整路径。
        algorithm: 哈希算法名，默认 SHA-256。
        on_progress: 可选回调，参数为 ``(已读字节, 总字节)``。
        cancel_event: 可选取消事件；置位时抛出 :class:`InterruptedError`。

    返回:
        小写十六进制摘要。

    异常:
        FileNotFoundError: 文件不存在。
        InterruptedError: 调用方请求取消。
    """
    file_path = Path(path)
    total = file_path.stat().st_size
    hasher = new_hasher(algorithm)
    done = 0
    with open(file_path, "rb") as handle:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("文件哈希计算已取消")
            block = handle.read(config.HASH_CHUNK)
            if not block:
                break
            hasher.update(block)
            done += len(block)
            if on_progress is not None:
                try:
                    on_progress(done, total)
                except Exception:  # 回调异常不影响计算
                    pass
    return hasher.hexdigest()


def file_hash_prefix(path: str, algorithm: Optional[str] = None, length: int = 16) -> str:
    """计算文件哈希并截断为指定长度，用于界面上的简短展示。

    参数:
        path: 文件完整路径。
        algorithm: 哈希算法名，默认 SHA-256。
        length: 保留的十六进制字符数。

    返回:
        截断后的十六进制摘要。
    """
    return file_hash(path, algorithm)[:length]
