"""基于 Python 标准库 ``http.server`` 的本地网页界面。

设计目标
--------
* **零第三方依赖**：只用标准库，用户只需要自备 Python 与 John 等工具，
  不需要 ``pip install``。
* **单页应用**：一个 HTML 文件加少量 JS/CSS，轮询 ``/api/state`` 获取任务与
  资源状态，逻辑简单、易于阅读和二次开发。
* **仅监听本机**：默认绑定 ``127.0.0.1``，只对本机浏览器开放。

接口一览
--------
===============  ======  ====================================================
路径              方法    说明
===============  ======  ====================================================
``/``            GET     主页面
``/static/*``    GET     静态资源（JS/CSS/图标）
``/api/state``   GET     任务、资源、统计、默认设置的快照
``/api/browse``  GET     浏览本机目录，用于批量选择文件
``/api/roots``   GET     磁盘根目录列表
``/api/jobs``    POST    批量添加任务
``/api/jobs/…``  POST    取消 / 重试 / 移除 / 清空任务
``/api/resources`` POST   预取工具资源或下载可选字典
``/api/shutdown`` POST   关闭服务
===============  ======  ====================================================
"""

from __future__ import annotations

import ctypes
import json
import mimetypes
import string
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import config, hashers
from ..engine import JohnEngine
from ..jobs import JobManager
from ..resources import CATALOG, ResourceError, ResourceManager
from ..results import ResultStore

#: 静态文件目录。
STATIC_DIR = Path(__file__).resolve().parent / "static"

#: 浏览目录时跳过的系统目录名（小写比较）。
_SKIP_DIRS = {
    "$recycle.bin",
    "system volume information",
    "config.msi",
    "recovery",
    "windows",
    "$windows.~bt",
    "$windows.~ws",
    "perflogs",
}

#: 允许的静态文件扩展名，避免任意文件读取。
_STATIC_SUFFIXES = {".html", ".js", ".css", ".svg", ".png", ".ico", ".json"}


# ---------------------------------------------------------------------------
# 应用上下文
# ---------------------------------------------------------------------------


class App:
    """Web 层需要共享的全部运行时对象。

    参数:
        resources: 资源管理器。
        engine: John 引擎。
        results: CSV 结果存储。
        manager: 任务调度器。
        defaults: 界面表单的初始设置。
        refresh: 是否在每次启动时强制刷新资源缓存。
    """

    def __init__(
        self,
        resources: ResourceManager,
        engine: JohnEngine,
        results: ResultStore,
        manager: JobManager,
        defaults: config.Settings,
        refresh: bool = False,
    ) -> None:
        self.resources = resources
        self.engine = engine
        self.results = results
        self.manager = manager
        self.defaults = defaults
        self.refresh = refresh
        self.server: Optional[ThreadingHTTPServer] = None
        self.started_at = time.time()

    # ------------------------------------------------------------------
    # 状态快照
    # ------------------------------------------------------------------

    def state(self) -> Dict[str, Any]:
        """构造 ``/api/state`` 返回的快照。

        包含任务列表、资源状态、统计信息、引擎能力与 CSV 路径，
        界面每秒轮询一次即可获得全部信息。
        """
        return {
            "app": {
                "name": config.APP_NAME,
                "version": config.APP_VERSION,
                "tagline": config.APP_TAGLINE,
                "uptime": int(time.time() - self.started_at),
            },
            "engine": {
                "opencl_devices": self.engine.opencl_devices(),
                "supported": list(hashers.SUPPORTED_EXTENSIONS),
                "cache_root": str(self.resources.root),
                "csv_path": str(self.results.path),
                "desktop": str(config.desktop_dir()),
            },
            "resources": self.resources.state(),
            "jobs": self.manager.snapshot(),
            "stats": self.manager.stats(),
            "results": {
                "csv_path": str(self.results.path),
                "count": self.results.count(),
            },
            "defaults": self.defaults.to_dict(),
        }


# ---------------------------------------------------------------------------
# 目录浏览辅助
# ---------------------------------------------------------------------------


def _windows_drives() -> List[str]:
    """返回 Windows 上的所有磁盘根目录（如 ``C:\\``）。"""
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
    except Exception:
        bitmask = 0
    drives: List[str] = []
    for index, letter in enumerate(string.ascii_uppercase):
        if bitmask & (1 << index):
            drives.append(f"{letter}:\\")
    return drives


def list_roots() -> List[Dict[str, str]]:
    """返回文件浏览器的根节点列表。

    在 Windows 上列出所有盘符，其他平台返回 ``/`` 与用户主目录。
    """
    if sys.platform == "win32":
        return [{"name": drive, "path": drive} for drive in _windows_drives()]
    return [
        {"name": "/", "path": "/"},
        {"name": "~", "path": str(Path.home())},
    ]


def _is_hidden(name: str) -> bool:
    """判断目录/文件是否应被文件浏览器隐藏。"""
    lowered = name.lower()
    return (
        name.startswith(".")
        or lowered in _SKIP_DIRS
        or lowered.startswith("$")
    )


def browse(path: Optional[str]) -> Dict[str, Any]:
    """列出目录内容，供界面批量选择文件。

    参数:
        path: 目标目录；为空时返回根节点列表。

    返回:
        ``{"path", "parent", "dirs", "files", "roots"}``；其中 ``files`` 的每一项
        都带有 ``supported`` 标记，界面据此决定是否允许勾选。
    """
    roots = list_roots()
    if not path:
        return {"path": "", "parent": "", "dirs": [], "files": [], "roots": roots}

    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"目录不存在：{path}")
    if target.is_file():
        target = target.parent

    dirs: List[Dict[str, str]] = []
    files: List[Dict[str, Any]] = []
    try:
        entries = list(target.iterdir())
    except PermissionError as exc:
        raise PermissionError(f"没有访问权限：{path}") from exc

    for entry in sorted(entries, key=lambda item: item.name.lower()):
        if _is_hidden(entry.name):
            continue
        try:
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": str(entry)})
            elif entry.is_file():
                files.append(
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "size": entry.stat().st_size,
                        "supported": hashers.tool_for(str(entry)) is not None,
                    }
                )
        except OSError:
            continue

    parent = "" if target.parent == target else str(target.parent)
    return {
        "path": str(target),
        "parent": parent,
        "dirs": dirs,
        "files": files,
        "roots": roots,
    }


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------


def make_handler(app: App) -> type:
    """创建一个绑定了 :class:`App` 的请求处理类。

    参数:
        app: 共享的应用上下文。

    返回:
        可传给 :class:`ThreadingHTTPServer` 的处理类。
    """

    class Handler(BaseHTTPRequestHandler):
        """处理静态文件与 ``/api/*`` JSON 接口。"""

        server_version = f"{config.APP_NAME}/{config.APP_VERSION}"

        # -- 基础工具 --------------------------------------------------

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            """把访问日志降级为静默（只在出错时打印），保持控制台整洁。"""
            if getattr(self, "command", "") and "api" in (self.path or ""):
                return
            if args and isinstance(args[0], str) and args[0].startswith(("4", "5")):
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send_json(self, payload: Any, status: int = 200) -> None:
            """发送 JSON 响应。"""
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, message: str, status: int = 400) -> None:
            """发送错误 JSON。"""
            self._send_json({"ok": False, "error": message}, status)

        def _read_json(self) -> Dict[str, Any]:
            """读取并解析请求体中的 JSON。"""
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception as exc:
                raise ValueError(f"请求体不是合法 JSON：{exc}") from exc

        # -- 路由 ------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            """处理 GET 请求。"""
            path = self.path.split("?", 1)[0]
            try:
                if path in ("/", "/index.html"):
                    self._serve_static("index.html")
                elif path.startswith("/static/"):
                    self._serve_static(path[len("/static/"):])
                elif path == "/api/state":
                    self._send_json(app.state())
                elif path == "/api/roots":
                    self._send_json({"roots": list_roots()})
                elif path == "/api/browse":
                    query = self.path.split("?", 1)[1] if "?" in self.path else ""
                    params = dict(
                        part.split("=", 1) for part in query.split("&") if "=" in part
                    )
                    raw_path = params.get("path", "")
                    decoded = _url_decode(raw_path)
                    self._send_json({"ok": True, **browse(decoded)})
                elif path == "/api/results/download":
                    self._serve_csv()
                else:
                    self._send_error_json("未知接口", 404)
            except (FileNotFoundError, PermissionError) as exc:
                self._send_error_json(str(exc), 400)
            except Exception as exc:
                self._send_error_json(f"服务端异常：{exc}", 500)

        def do_POST(self) -> None:  # noqa: N802
            """处理 POST 请求。"""
            path = self.path.split("?", 1)[0]
            try:
                payload = self._read_json()
                if path == "/api/jobs":
                    self._handle_add_jobs(payload)
                elif path == "/api/jobs/cancel":
                    self._handle_simple(payload, "cancel")
                elif path == "/api/jobs/retry":
                    self._handle_simple(payload, "retry")
                elif path == "/api/jobs/remove":
                    self._handle_simple(payload, "remove")
                elif path == "/api/jobs/clear":
                    removed = app.manager.clear_finished()
                    self._send_json({"ok": True, "removed": removed})
                elif path == "/api/resources":
                    self._handle_resources(payload)
                elif path == "/api/shutdown":
                    self._send_json({"ok": True})
                    threading.Thread(target=self._shutdown, daemon=True).start()
                else:
                    self._send_error_json("未知接口", 404)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
            except Exception as exc:
                self._send_error_json(f"服务端异常：{exc}", 500)

        # -- 具体接口 --------------------------------------------------

        def _handle_add_jobs(self, payload: Dict[str, Any]) -> None:
            """添加一批任务。"""
            paths = payload.get("paths") or []
            if not isinstance(paths, list) or not paths:
                self._send_error_json("请至少选择一个文件")
                return
            settings = config.Settings.from_dict(payload.get("settings"))
            result = app.manager.add([str(item) for item in paths], settings)
            self._send_json({"ok": True, **result})

        def _handle_simple(self, payload: Dict[str, Any], action: str) -> None:
            """处理单任务操作（取消 / 重试 / 移除）。"""
            job_id = str(payload.get("id") or "")
            handler = getattr(app.manager, action)
            ok = bool(handler(job_id))
            self._send_json({"ok": ok})

        def _handle_resources(self, payload: Dict[str, Any]) -> None:
            """预取工具资源或下载可选字典。

            请求体支持两种写法::

                {"keys": ["jtr", "perl"]}      # 预取指定资源
                {"keys": "all"}                # 预取全部非可选资源
                {"wordlist": "rockyou"}        # 下载可选字典并返回其路径
            """
            wordlist = payload.get("wordlist")
            if wordlist:
                try:
                    path = app.resources.wordlist_path(str(wordlist))
                except ResourceError as exc:
                    self._send_error_json(str(exc))
                    return
                self._send_json({"ok": True, "path": str(path)})
                return

            keys = payload.get("keys") or []
            if keys == "all":
                keys = list(CATALOG)
            keys = [str(key) for key in keys if str(key) in CATALOG]
            threading.Thread(
                target=app.resources.ensure_many,
                args=(keys,),
                daemon=True,
                name="zr-resource-prefetch",
            ).start()
            self._send_json({"ok": True, "keys": keys})

        # -- 静态文件 --------------------------------------------------

        def _serve_csv(self) -> None:
            """以附件形式下载桌面上的结果 CSV。"""
            csv_file = app.results.path
            if not csv_file.exists():
                self._send_error_json("结果文件尚不存在", 404)
                return
            data = csv_file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{config.CSV_FILENAME}"',
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_static(self, relative: str) -> None:
            """输出静态文件（只允许白名单扩展名）。"""
            candidate = (STATIC_DIR / relative).resolve()
            if not str(candidate).startswith(str(STATIC_DIR)) or not candidate.is_file():
                self._send_error_json("文件不存在", 404)
                return
            if candidate.suffix.lower() not in _STATIC_SUFFIXES:
                self._send_error_json("不允许的文件类型", 403)
                return
            content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type.endswith("javascript"):
                content_type += "; charset=utf-8"
            data = candidate.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _shutdown(self) -> None:
            """在后台线程中关闭服务器，让响应先发出去。"""
            time.sleep(0.2)
            if app.server:
                app.server.shutdown()

    return Handler


def _url_decode(value: str) -> str:
    """解码 URL 查询参数中的百分号转义与 ``+``。"""
    from urllib.parse import unquote_plus

    return unquote_plus(value)


# ---------------------------------------------------------------------------
# 服务器启动
# ---------------------------------------------------------------------------


def create_server(app: App, host: str, port: int) -> Tuple[ThreadingHTTPServer, int]:
    """创建并绑定 HTTP 服务器，端口被占用时自动向后寻找。

    参数:
        app: 应用上下文。
        host: 监听地址。
        port: 首选端口。

    返回:
        ``(服务器实例, 实际端口)``。
    """
    handler = make_handler(app)
    last_error: Optional[Exception] = None
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer((host, candidate), handler)
            server.daemon_threads = True
            app.server = server
            return server, candidate
        except OSError as exc:
            last_error = exc
    raise RuntimeError(f"无法绑定端口 {port}-{port + 19}：{last_error}")


def serve(app: App, host: str, port: int, open_browser: bool = True) -> None:
    """启动服务器并进入主循环。

    参数:
        app: 应用上下文。
        host: 监听地址。
        port: 首选端口。
        open_browser: 是否自动用默认浏览器打开界面。
    """
    server, actual_port = create_server(app, host, port)
    url = f"http://{host}:{actual_port}/"
    print(f"[{config.APP_NAME}] 网页界面已启动: {url}")
    print(f"[{config.APP_NAME}] 结果文件: {app.results.path}")
    print(f"[{config.APP_NAME}] 缓存目录: {app.resources.root}")
    print("按 Ctrl+C 退出。")

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        server.server_close()
        app.manager.shutdown()
