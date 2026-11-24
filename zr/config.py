"""ZipRipper（Python 版）的全局配置模块。

本模块集中存放所有可调常量、缓存目录与默认选项，是整个项目中唯一允许被
任意模块导入、且不导入其他业务模块的地方，以避免循环依赖。

设计说明
--------
* 所有路径都通过小函数惰性解析，修改环境变量后下一次调用立即生效，
  方便便携部署与自动化测试。
* Windows 下桌面路径从注册表 ``User Shell Folders`` 读取，因此重定向桌面、
  OneDrive 桌面也能被正确处理。
* 缓存目录可用环境变量 ``ZR_CACHE_DIR`` 覆盖，测试与高级用户都依赖它。
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# 应用标识
# ---------------------------------------------------------------------------

#: 应用名称，用于浏览器标题与界面标题。
APP_NAME = "ZipRipper"

#: Python 版的版本号，与 CMD 版相互独立。
APP_VERSION = "2.0.0"

#: 界面副标题。
APP_TAGLINE = "John the Ripper 前端 · 批量 / 掩码 / 字典"

# ---------------------------------------------------------------------------
# 结果文件
# ---------------------------------------------------------------------------

#: 每恢复出一个密码就追加一行的 CSV 文件名。
CSV_FILENAME = "password_consumer.csv"

#: CSV 表头。前三列固定为：完成时间、加密文件完整路径、恢复出的密码；
#: 第四列为文件内容哈希，用于跨文件复用历史结果（路径不同但内容相同）。
CSV_HEADER = ("完成时间", "加密文件完整路径", "恢复出的密码", "文件哈希(SHA-256)")

#: 用于标识文件内容的哈希算法（与 CSV 第四列对应）。
HASH_ALGORITHM = "sha256"

#: 计算文件哈希时的读取块大小（字节）。
HASH_CHUNK = 1024 * 1024

# ---------------------------------------------------------------------------
# 目录解析
# ---------------------------------------------------------------------------


def _local_app_data() -> Path:
    """返回当前用户的应用程序数据目录（Windows 为 ``%LOCALAPPDATA%``）。

    POSIX 系统回退到 ``~/.local/share``，无法判断平台时回退到用户主目录。
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base)
        return Path.home() / "AppData" / "Local"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg)
    return Path.home() / ".local" / "share"


def cache_root() -> Path:
    """返回缓存根目录，用于存放工具、任务目录与下载文件。

    可通过环境变量 ``ZR_CACHE_DIR`` 覆盖，便于便携安装或自动化测试。
    """
    override = os.environ.get("ZR_CACHE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (_local_app_data() / "ZipRipper").resolve()


def jobs_root() -> Path:
    """返回存放每个任务工作目录的文件夹。"""
    return cache_root() / "jobs"


def downloads_root() -> Path:
    """返回存放下载文件（含 ``.part`` 断点文件）的文件夹。"""
    return cache_root() / "downloads"


def logs_root() -> Path:
    """返回存放程序日志的文件夹。"""
    return cache_root() / "logs"


def desktop_dir() -> Path:
    """返回当前用户的桌面目录。

    Windows 下以注册表 ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\
    Explorer\\User Shell Folders`` 中的 ``Desktop`` 为准，可正确处理重定向与
    OneDrive 桌面；读取失败时回退到 ``~/Desktop``。
    """
    if sys.platform == "win32":
        try:
            import winreg  # 仅在需要时导入，避免非 Windows 平台报错

            key_path = (
                r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\User Shell Folders"
            )
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "Desktop")
            expanded = os.path.expandvars(str(value))
            if expanded:
                return Path(expanded)
        except Exception:  # 注册表访问失败不影响主流程
            pass
    return Path.home() / "Desktop"


def csv_path() -> Path:
    """返回桌面上的 CSV 结果文件完整路径。"""
    return desktop_dir() / CSV_FILENAME


# ---------------------------------------------------------------------------
# 引擎默认值
# ---------------------------------------------------------------------------

#: John the Ripper 自带、默认使用的字典文件名。
DEFAULT_WORDLIST_NAME = "password.lst"

#: 字典模式默认附加的规则集（与 CMD 版保持一致）。
DEFAULT_RULES = "single,all"

#: 暴力破解模式默认使用的增量字符集。
DEFAULT_INCREMENTAL = "ASCII"

#: 支持的三类攻击模式：字典、掩码、增量暴力。
ATTACK_MODES = ("wordlist", "mask", "incremental")

#: 同时执行任务的最大并发数。
MAX_WORKERS = 8

#: 单个任务的默认超时秒数（0 表示不限制）。
DEFAULT_JOB_TIMEOUT = 0

#: 单次哈希提取的默认超时秒数。
HASH_TIMEOUT = 300


# ---------------------------------------------------------------------------
# 设置对象
# ---------------------------------------------------------------------------


@dataclass
class Settings:
    """描述“如何攻击一批文件”的选项集合。

    一个 :class:`Settings` 实例会随任务一起加入批次，因此同一批文件共享同一套
    攻击配置。该对象只是纯数据，可安全复制与序列化。

    属性:
        attack: 取值来自 :data:`ATTACK_MODES`。
        wordlist: 自定义字典的绝对路径；留空表示使用 John 自带的 ``password.lst``。
        use_rules: 是否在字典之上叠加 :data:`DEFAULT_RULES` 规则。
        mask: John 掩码表达式，例如 ``?u?l?l?l?d?d``。
        custom_charset: 自定义字符集，非空时以 ``--1=`` 传给 John。
        use_gpu: 检测到显卡时优先使用 OpenCL 格式。
        workers: 同时执行的任务数量。
        job_timeout: 单任务墙钟超时秒数（0 表示不限制）。
    """

    attack: str = "wordlist"
    wordlist: str = ""
    use_rules: bool = True
    mask: str = ""
    custom_charset: str = ""
    use_gpu: bool = True
    workers: int = 1
    job_timeout: int = DEFAULT_JOB_TIMEOUT

    # ------------------------------------------------------------------
    # 校验与转换
    # ------------------------------------------------------------------

    def normalized(self) -> "Settings":
        """返回经过校验的副本。

        非法取值会被替换为安全默认值而不是抛出异常，保证浏览器传来的错误请求
        不会让工作线程崩溃。
        """
        attack = self.attack if self.attack in ATTACK_MODES else "wordlist"
        try:
            workers = int(self.workers)
        except (TypeError, ValueError):
            workers = 1
        workers = max(1, min(MAX_WORKERS, workers))
        try:
            timeout = max(0, int(self.job_timeout))
        except (TypeError, ValueError):
            timeout = DEFAULT_JOB_TIMEOUT
        wordlist = str(self.wordlist or "").strip()
        mask = str(self.mask or "").strip()
        charset = str(self.custom_charset or "").strip()
        if attack == "mask" and not mask:
            # 掩码模式却没有掩码没有意义，自动退回字典模式。
            attack = "wordlist"
        return Settings(
            attack=attack,
            wordlist=wordlist,
            use_rules=bool(self.use_rules),
            mask=mask,
            custom_charset=charset,
            use_gpu=bool(self.use_gpu),
            workers=workers,
            job_timeout=timeout,
        )

    def to_dict(self) -> Dict[str, Any]:
        """返回便于 JSON 序列化的字典表示。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Settings":
        """从 JSON 数据构造 :class:`Settings` 实例。

        未知字段会被忽略，缺失字段回退到默认值，因此接口向前兼容。
        """
        data = data or {}
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in known}
        return cls(**payload).normalized()
