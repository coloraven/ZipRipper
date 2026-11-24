"""外部工具与字典的“缓存优先”资源管理器。

Python 版是在线工具：仓库里不再内置 John the Ripper、7-Zip 或 Perl，
所有外部资源都按需下载并保存在用户缓存目录中。本管理器保证：

* 同一个资源最多下载一次，后续运行直接复用缓存；
* 下载中断后从 ``.part`` 文件断点续传；
* 解包后的目录带有 JSON 清单文件，缓存损坏或版本变化时自动重建；
* 多个工作线程同时需要 John 时，通过按资源加锁串行化，避免重复下载。

应用使用的资源键：

``7zr``
    独立的 7-Zip 解包器，用于解开 ``.7z`` 资源包。
``jtr``
    John the Ripper 1.9.1 Windows x64 版本（``JtR/run/john.exe`` 等）。
``7z``
    完整 7-Zip 控制台（``7za.exe``），仅在需要解开 ``.7z`` 字典时使用。
``perl``
    Strawberry Perl 便携版，``7z2john.pl`` 依赖它。
``wordlist:rockyou``
    可选字典，仅在用户选择时下载。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Tuple

from . import config

# ---------------------------------------------------------------------------
# 资源清单
# ---------------------------------------------------------------------------

#: 下载时使用的 User-Agent（部分 CDN 会拒绝 urllib 默认值）。
USER_AGENT = f"{config.APP_NAME}/{config.APP_VERSION} (+https://github.com/illsk1lls/ZipRipper)"

#: 下载时每次读取的块大小（字节）。
DOWNLOAD_CHUNK = 256 * 1024

#: 每个已安装资源目录中写入的清单文件名。
MANIFEST_NAME = ".zr-resource.json"


@dataclass(frozen=True)
class Resource:
    """描述一个可下载、可解包的外部资源。

    属性:
        key: 供其他模块引用的稳定标识。
        title: 网页界面中显示的中文名称。
        url: 固定版本的下载地址。
        kind: ``file``、``7z`` 或 ``zip``，决定安装方式。
        archive_name: 下载目录中使用的文件名。
        target: 安装目录（相对缓存根目录）。
        marker: 判断资源可用时必须存在的文件（相对 ``target``）。
        requires: 安装前必须先就绪的其他资源键（``jtr`` 需要 ``7zr`` 解包）。
        optional: ``True`` 表示只在用户主动选择时才下载。
    """

    key: str
    title: str
    url: str
    kind: str
    archive_name: str
    target: str
    marker: str
    requires: Tuple[str, ...] = ()
    optional: bool = False


#: 应用已知的全部工具资源。
CATALOG: Dict[str, Resource] = {
    "7zr": Resource(
        key="7zr",
        title="7-Zip 独立解包器 (7zr.exe)",
        url="https://github.com/ip7z/7zip/releases/download/26.02/7zr.exe",
        kind="file",
        archive_name="7zr.exe",
        target="bin",
        marker="7zr.exe",
    ),
    "jtr": Resource(
        key="jtr",
        title="John the Ripper 1.9.1 (winX64)",
        url="https://github.com/openwall/john-packages/releases/download/v1.9.1-ce/winX64_1_JtR.7z",
        kind="7z",
        archive_name="winX64_1_JtR.7z",
        target="tools",
        marker="JtR/run/john.exe",
        requires=("7zr",),
    ),
    "7z": Resource(
        key="7z",
        title="7-Zip 控制台 (7za.exe)",
        url="https://github.com/ip7z/7zip/releases/download/26.02/7z2602-extra.7z",
        kind="7z",
        archive_name="7zExtra.7z",
        target="tools/JtR",
        marker="7za.exe",
        requires=("7zr",),
    ),
    "perl": Resource(
        key="perl",
        title="Strawberry Perl 便携版 (7z2john)",
        url="https://strawberryperl.com/download/5.16.3.1/strawberry-perl-5.16.3.1-64bit-portable.zip",
        kind="zip",
        archive_name="perlportable.zip",
        target="tools/perl",
        marker="perl/bin/perl.exe",
    ),
}

#: 可按需下载的可选字典（键与 :class:`Resource.key` 保持一致）。
WORDLIST_CATALOG: Dict[str, Resource] = {
    "wordlist:rockyou": Resource(
        key="wordlist:rockyou",
        title="rockyou.txt（约 133MB）",
        url="https://github.com/brannondorsey/naive-hashcat/releases/download/data/rockyou.txt",
        kind="file",
        archive_name="rockyou.txt",
        target="wordlists",
        marker="rockyou.txt",
        optional=True,
    ),
}


class ResourceError(RuntimeError):
    """资源无法下载或安装时抛出的异常。"""


# ---------------------------------------------------------------------------
# 管理器
# ---------------------------------------------------------------------------


class ResourceManager:
    """在缓存目录中下载、解包并校验外部资源。

    参数:
        root: 缓存根目录，默认为 :func:`config.cache_root`。
        on_change: 可选回调，状态变化时以 ``(key, state_dict)`` 调用，
            用于把下载进度推送给网页界面。
        refresh: 为 ``True`` 时即使缓存有效也会重新下载。
    """

    def __init__(
        self,
        root: Optional[Path] = None,
        on_change: Optional[Callable[[str, Dict], None]] = None,
        refresh: bool = False,
    ) -> None:
        self.root = Path(root) if root else config.cache_root()
        self.on_change = on_change
        self.refresh = refresh
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "downloads").mkdir(exist_ok=True)
        (self.root / "tools").mkdir(exist_ok=True)
        (self.root / "bin").mkdir(exist_ok=True)
        (self.root / "wordlists").mkdir(exist_ok=True)

        self._lock = threading.RLock()
        self._key_locks: Dict[str, threading.RLock] = {}
        self._states: Dict[str, Dict] = {}

    # ------------------------------------------------------------------
    # 状态上报
    # ------------------------------------------------------------------

    def _state(self, res: Resource) -> Dict:
        """返回（必要时创建）资源的可变状态字典。

        首次访问时会检查缓存目录，已安装的资源直接显示为“已就绪”，
        这样界面与 ``--check`` 在尚未触发任何下载时也能给出准确状态。
        """
        with self._lock:
            state = self._states.get(res.key)
            if state is None:
                ready = self.is_installed(res.key)
                state = {
                    "key": res.key,
                    "title": res.title,
                    "status": "ready" if ready else "missing",
                    "progress": 100 if ready else 0,
                    "message": "已缓存" if ready else "",
                    "path": str(self.cache_dir(res)),
                    "optional": res.optional,
                }
                self._states[res.key] = state
            return state

    def _update(self, res: Resource, **fields) -> None:
        """把 ``fields`` 合并进资源状态并通知观察者。"""
        state = self._state(res)
        with self._lock:
            state.update(fields)
            snapshot = dict(state)
        if self.on_change:
            try:
                self.on_change(res.key, snapshot)
            except Exception:  # 观察者出错绝不能影响主流程
                pass

    def state(self) -> Dict[str, Dict]:
        """返回所有已知资源状态的快照（以资源键为键）。"""
        with self._lock:
            keys = set(CATALOG) | set(WORDLIST_CATALOG) | set(self._states)
            result = {}
            for key in sorted(keys):
                res = CATALOG.get(key) or WORDLIST_CATALOG.get(key)
                if res is None:
                    continue
                result[key] = dict(self._state(res))
            return result

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    def cache_dir(self, res: Resource) -> Path:
        """返回资源的安装目录。"""
        return self.root / res.target

    def download_path(self, res: Resource) -> Path:
        """返回资源压缩包/可执行文件的下载路径。"""
        return self.root / "downloads" / res.archive_name

    def path(self, key: str) -> Path:
        """返回 ``key`` 的安装目录（不触发下载）。"""
        res = self._resource(key)
        return self.cache_dir(res)

    def _resource(self, key: str) -> Resource:
        """按资源键查找资源，未知键抛出 :class:`ResourceError`。"""
        res = CATALOG.get(key) or WORDLIST_CATALOG.get(key)
        if res is None:
            raise ResourceError(f"未知资源: {key}")
        return res

    def _key_lock(self, key: str) -> threading.RLock:
        """返回用于串行化 ``ensure()`` 的按资源锁。"""
        with self._lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._key_locks[key] = lock
            return lock

    # ------------------------------------------------------------------
    # 就绪判断 / 清单
    # ------------------------------------------------------------------

    def _manifest_path(self, res: Resource) -> Path:
        """返回标记资源已安装的清单文件路径。"""
        return self.cache_dir(res) / MANIFEST_NAME

    def _read_manifest(self, res: Resource) -> Dict:
        """读取资源安装清单（不存在时返回空字典）。"""
        path = self._manifest_path(res)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return {}

    def _write_manifest(self, res: Resource) -> None:
        """写入描述当前已安装资源的清单文件。"""
        manifest = {
            "key": res.key,
            "url": res.url,
            "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": config.APP_VERSION,
        }
        path = self._manifest_path(res)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)

    def is_installed(self, key: str) -> bool:
        """只看标记文件是否存在，不受 ``refresh`` 影响。

        用于“是否已缓存”这类只读判断，避免在状态查询中触发下载。
        """
        res = self._resource(key)
        return (self.cache_dir(res) / res.marker).exists()

    def is_ready(self, key: str) -> bool:
        """资源已安装且版本匹配时返回 ``True``。"""
        res = self._resource(key)
        if self.refresh:
            return False
        marker = self.cache_dir(res) / res.marker
        if not marker.exists():
            return False
        manifest = self._read_manifest(res)
        return manifest.get("url") in (None, res.url)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def ensure(self, key: str) -> Path:
        """确保 ``key`` 已安装，并返回其安装目录。

        该调用会阻塞到资源就绪（必要时下载与解包），失败时抛出
        :class:`ResourceError`。
        """
        res = self._resource(key)
        with self._key_lock(key):
            if self.is_ready(key):
                self._update(res, status="ready", progress=100, message="已缓存")
                return self.cache_dir(res)

            for dep in res.requires:
                self.ensure(dep)

            try:
                archive = self._fetch(res)
                self._install(res, archive)
            except ResourceError as exc:
                self._update(res, status="error", message=str(exc), progress=0)
                raise
            self._update(res, status="ready", progress=100, message="就绪")
            return self.cache_dir(res)

    def ensure_many(self, keys: Iterable[str]) -> Dict[str, Path]:
        """确保多个资源就绪，返回“资源键 → 目录”的映射。"""
        return {key: self.ensure(key) for key in dict.fromkeys(keys)}

    def prepare_all(self) -> Dict[str, Path]:
        """确保所有非可选资源就绪（对应界面上的“预取资源”按钮）。"""
        return self.ensure_many(CATALOG.keys())

    # 便捷访问器 ---------------------------------------------------------

    def jtr_run_dir(self) -> Path:
        """返回 John the Ripper 的 ``run`` 目录（必要时下载）。"""
        return self.ensure("jtr") / "JtR" / "run"

    def jtr_tool(self, name: str) -> Path:
        """返回 John ``run`` 目录下某个工具的路径。"""
        return self.jtr_run_dir() / name

    def perl_root(self) -> Path:
        """返回便携 Perl 的安装根目录。"""
        return self.ensure("perl")

    def perl_exe(self) -> Path:
        """返回便携 ``perl.exe`` 的路径。"""
        return self.perl_root() / "perl" / "bin" / "perl.exe"

    def perl_env(self) -> Dict[str, str]:
        """返回能让便携 Perl 找到自带 DLL 的环境变量。

        Strawberry Perl 便携版把动态库放在 ``c/bin``，模块放在
        ``perl/lib`` 与 ``perl/vendor/lib``；不把这些目录加入 ``PATH`` 时
        ``Compress::Raw::Lzma`` 会加载失败。
        """
        root = self.perl_root()
        parts = [
            str(root / "c" / "bin"),
            str(root / "perl" / "bin"),
            str(root / "perl" / "site" / "bin"),
        ]
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join(parts + [env.get("PATH", "")])
        env.pop("PERL5LIB", None)
        env.pop("PERL5OPT", None)
        return env

    def sevenzipr(self) -> Path:
        """返回独立解包器 ``7zr.exe`` 的路径。"""
        return self.ensure("7zr") / "7zr.exe"

    def sevenzip_console(self) -> Path:
        """返回 7-Zip 控制台 ``7za.exe`` 的路径。"""
        return self.ensure("7z") / "7za.exe"

    def wordlist_path(self, name: str) -> Path:
        """返回可选字典的缓存路径，必要时下载一次。

        参数:
            name: 字典名称，可写 ``rockyou`` 或完整的资源键
                ``wordlist:rockyou``。
        """
        key = name if name in WORDLIST_CATALOG else f"wordlist:{name}"
        res = WORDLIST_CATALOG.get(key)
        if res is None:
            raise ResourceError(f"未知字典: {name}")
        self.ensure(res.key)
        return self.cache_dir(res) / res.marker

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def _fetch(self, res: Resource) -> Path:
        """把资源下载到下载目录，支持断点续传。

        返回完整文件的路径。传输过程中使用 ``.part`` 文件，因此中断后再次
        调用会从上次的位置继续。
        """
        final = self.download_path(res)
        if final.exists() and final.stat().st_size > 0:
            return final

        part = final.with_name(final.name + ".part")
        have = part.stat().st_size if part.exists() else 0

        request = urllib.request.Request(res.url, headers={"User-Agent": USER_AGENT})
        if have:
            request.add_header("Range", f"bytes={have}-")

        self._update(res, status="downloading", progress=0, message="开始下载…")
        try:
            response = urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and have:
                # 文件其实已经下载完整，服务器拒绝了越界 Range 请求。
                part.replace(final)
                return final
            raise ResourceError(f"下载失败 ({exc.code}): {res.url}") from exc
        except Exception as exc:  # 网络层错误
            raise ResourceError(f"下载失败: {exc}") from exc

        with response:
            status = getattr(response, "status", 200)
            if have and status != 206:
                have = 0  # 服务器忽略 Range 头，只能从头下载
            length = int(response.headers.get("Content-Length") or 0)
            total = length + have if length else 0
            mode = "ab" if have else "wb"
            written = have
            last_report = 0.0
            with open(part, mode) as handle:
                while True:
                    block = response.read(DOWNLOAD_CHUNK)
                    if not block:
                        break
                    handle.write(block)
                    written += len(block)
                    now = time.time()
                    if now - last_report > 0.2:
                        last_report = now
                        percent = int(written * 100 / total) if total else 0
                        self._update(
                            res,
                            status="downloading",
                            progress=min(99, percent),
                            message=f"下载中 {written // 1048576}MB"
                            + (f" / {total // 1048576}MB" if total else ""),
                        )
        if total and written < total:
            raise ResourceError(
                f"下载不完整（{written}/{total} 字节）: {res.url}"
            )
        part.replace(final)
        return final

    # ------------------------------------------------------------------
    # 安装
    # ------------------------------------------------------------------

    def _install(self, res: Resource, archive: Path) -> None:
        """把下载好的文件安装到资源目录。

        ``kind="file"`` 直接复制文件，``kind="zip"`` 使用 Python 的
        :mod:`zipfile`，``kind="7z"`` 调用缓存中的 ``7zr.exe``。
        """
        target = self.cache_dir(res)
        self._update(res, status="extracting", progress=0, message="解包中…")
        if self.refresh and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)

        if res.kind == "file":
            destination = target / res.archive_name
            shutil.copyfile(archive, destination)
        elif res.kind == "zip":
            self._extract_zip(archive, target)
        elif res.kind == "7z":
            self._extract_7z(archive, target)
        else:  # 清单是静态的，理论上不会走到这里
            raise ResourceError(f"不支持的资源类型: {res.kind}")

        marker = target / res.marker
        if not marker.exists():
            raise ResourceError(
                f"安装校验失败，缺少 {res.marker}（目录: {target}）"
            )
        self._write_manifest(res)
        self._update(res, status="ready", progress=100, message="就绪")

    def _extract_zip(self, archive: Path, target: Path) -> None:
        """使用 :mod:`zipfile` 解开普通 ``.zip`` 压缩包。"""
        try:
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(target)
        except Exception as exc:
            raise ResourceError(f"ZIP 解包失败: {exc}") from exc

    def _extract_7z(self, archive: Path, target: Path) -> None:
        """使用缓存中的 ``7zr.exe`` 解开 ``.7z`` 压缩包。"""
        import subprocess

        extractor = self.sevenzipr()
        try:
            completed = subprocess.run(
                [str(extractor), "x", "-y", f"-o{target}", str(archive)],
                capture_output=True,
                text=True,
                timeout=1800,
            )
        except Exception as exc:
            raise ResourceError(f"7z 解包失败: {exc}") from exc
        if completed.returncode != 0:
            raise ResourceError(
                f"7z 解包失败（退出码 {completed.returncode}）: "
                f"{(completed.stderr or completed.stdout or '').strip()[:200]}"
            )
