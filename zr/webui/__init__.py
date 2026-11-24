"""本地网页界面子包。

``server``
    仅依赖标准库的 HTTP 服务：静态页面、JSON 接口、目录浏览与结果下载。
``static/``
    单页前端（``index.html`` / ``app.js`` / ``style.css``），无框架、无构建步骤。
"""

from .server import App, serve  # noqa: F401

__all__ = ["App", "serve"]
