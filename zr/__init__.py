"""ZipRipper Python 版的核心包。

模块划分
--------
``config``
    全局常量、缓存/桌面路径、:class:`~zr.config.Settings` 设置对象。
``hashing``
    文件内容哈希（SHA-256），用于跨文件复用历史破解结果。
``resources``
    外部工具与字典的“缓存优先”下载/解包/校验管理器。
``hashers``
    加密格式与 John ``*2john`` 提取器的映射、输出解析、OpenCL 格式推导。
``engine``
    John the Ripper 的调用封装（哈希提取、破解、进度与取消）。
``jobs``
    批量任务队列、并发工作线程、取消/重试与结果复用逻辑。
``results``
    桌面 ``password_consumer.csv`` 的追加写入与按哈希查询。
``webui``
    基于标准库 ``http.server`` 的本地网页界面。
"""

from .config import APP_NAME, APP_VERSION  # noqa: F401  便于外部快速取版本号

__all__ = ["APP_NAME", "APP_VERSION"]
