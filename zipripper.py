"""ZipRipper（Python 版）命令行入口。

用法示例::

    python zipripper.py                    # 启动本地网页界面并自动打开浏览器
    python zipripper.py --port 9000        # 指定端口
    python zipripper.py --no-browser       # 不自动打开浏览器
    python zipripper.py --prefetch         # 预下载全部工具资源后退出
    python zipripper.py --check            # 只检查环境（Python / John / 显卡）

运行后会启动一个只监听本机的 HTTP 服务，并用默认浏览器打开网页界面；
在界面里可以批量选择加密文件、设置字典/掩码/并发数，并实时查看进度。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from zr import config, hashers
from zr.engine import JohnEngine
from zr.jobs import JobManager
from zr.resources import ResourceManager
from zr.results import ResultStore
from zr.webui.server import App, serve


def setup_logging() -> Path:
    """初始化文件日志并返回日志文件路径。

    日志写入缓存目录下的 ``logs/zipripper.log``，控制台只输出必要的提示。
    """
    log_dir = config.logs_root()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "zipripper.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8")],
    )
    return log_file


def build_app(
    cache_dir: Optional[Path],
    refresh: bool,
    workers: int,
) -> App:
    """组装应用上下文（资源管理器、引擎、结果存储、任务调度器）。

    参数:
        cache_dir: 自定义缓存目录；``None`` 表示使用默认位置。
        refresh: 是否强制重新下载资源。
        workers: 默认并发任务数。

    返回:
        可供 Web 层使用的 :class:`zr.webui.server.App` 实例。
    """
    resources = ResourceManager(root=cache_dir, refresh=refresh)
    results = ResultStore()
    engine = JohnEngine(resources)
    manager = JobManager(engine, results)
    defaults = config.Settings(workers=max(1, workers)).normalized()
    return App(
        resources=resources,
        engine=engine,
        results=results,
        manager=manager,
        defaults=defaults,
        refresh=refresh,
    )


def print_environment(app: App) -> None:
    """打印环境检查结果（Python、缓存、结果文件、显卡、支持格式）。"""
    print(f"应用版本      : {config.APP_NAME} {config.APP_VERSION}（Python 版）")
    print(f"Python 解释器 : {sys.executable} ({sys.version.split()[0]})")
    print(f"缓存目录      : {app.resources.root}")
    print(f"结果文件      : {app.results.path}")
    print(f"支持格式      : {hashers.describe_supported()}")

    devices = app.engine.opencl_devices()
    if devices:
        print("OpenCL 设备   :")
        for device in devices:
            print(f"  - {device}")
    else:
        print("OpenCL 设备   : 未检测到（将使用 CPU 模式）")

    print("资源状态      :")
    for key, state in app.resources.state().items():
        print(f"  - {key:<16} {state['status']:<12} {state['message']}")


def run_prefetch(app: App) -> int:
    """预下载全部工具资源，完成后返回退出码。

    只处理非可选的工具资源（John、7-Zip、Perl）；可选字典（rockyou）
    需要用户明确选择才会下载，避免无意中拉取上百 MB 数据。

    参数:
        app: 应用上下文。

    返回:
        ``0`` 表示全部成功，``1`` 表示存在失败项。
    """
    from zr.resources import CATALOG

    failed = 0
    for key in CATALOG:
        print(f"准备 {key} …", end="", flush=True)
        try:
            app.resources.ensure(key)
            print(" 完成")
        except Exception as exc:
            failed += 1
            print(f" 失败：{exc}")
    return 1 if failed else 0


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    """解析命令行参数。

    参数:
        argv: 参数列表，默认取 ``sys.argv[1:]``。
    """
    parser = argparse.ArgumentParser(
        prog="zipripper.py",
        description="ZipRipper（Python 版）：批量破解 ZIP/RAR/7z/PDF/Office 密码，"
        "使用 John the Ripper，提供本地网页界面。",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，仅本机）")
    parser.add_argument("--port", type=int, default=8765, help="首选端口（被占用时自动顺延）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--cache-dir", default="", help="自定义缓存目录（默认 %%LOCALAPPDATA%%\\ZipRipper）")
    parser.add_argument("--refresh", action="store_true", help="忽略缓存，强制重新下载工具资源")
    parser.add_argument("--workers", type=int, default=1, help="默认并发任务数（1-8）")
    parser.add_argument("--prefetch", action="store_true", help="预下载全部工具资源后退出")
    parser.add_argument("--check", action="store_true", help="只检查环境并打印报告后退出")
    parser.add_argument("--verbose", action="store_true", help="在控制台输出调试日志")
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    """程序主入口。

    参数:
        argv: 命令行参数列表。

    返回:
        进程退出码。
    """
    # 让中文输出在 Windows 控制台也能正常显示，并关闭输出缓冲，
    # 这样双击启动器时能立即看到地址与进度提示。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass

    args = parse_args(argv)
    log_file = setup_logging()
    if args.verbose:
        logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
        logging.getLogger().setLevel(logging.DEBUG)

    cache_dir = Path(args.cache_dir).expanduser() if args.cache_dir else None
    app = build_app(cache_dir, args.refresh, args.workers)
    logging.info("启动 ZipRipper Python 版，缓存目录 %s", app.resources.root)

    if args.check:
        print_environment(app)
        print(f"日志文件      : {log_file}")
        return 0

    if args.prefetch:
        print_environment(app)
        code = run_prefetch(app)
        print(f"日志文件      : {log_file}")
        return code

    print(f"[{config.APP_NAME}] 缓存目录: {app.resources.root}")
    print(f"[{config.APP_NAME}] 日志文件: {log_file}")
    try:
        serve(app, args.host, args.port, open_browser=not args.no_browser)
    except KeyboardInterrupt:
        print("已退出。")
    except Exception as exc:
        logging.exception("服务异常退出")
        print(f"启动失败：{exc}")
        return 1
    finally:
        logging.info("ZipRipper 已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
