"""加密文件格式与 John ``*2john`` 提取脚本之间的映射关系。

John the Ripper 对每种加密格式都提供一个“提取器”，把文件内容转换成 John
可以识别的哈希行。本模块只负责三件事：

1. 按扩展名找到对应的提取器（:data:`TOOLS` / :data:`EXTENSION_MAP`）；
2. 生成调用命令（原生 exe、Python 脚本或 Perl 脚本）；
3. 解析提取器的输出，判断是否“未加密 / 不支持 / 成功得到哈希”，
   并推断该哈希对应的 OpenCL 格式名（供显卡加速使用）。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config

# ---------------------------------------------------------------------------
# 提取器描述
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HashTool:
    """描述一个 ``*2john`` 哈希提取器。

    属性:
        key: 逻辑名称，同时用于日志输出。
        title: 中文说明。
        extensions: 该提取器负责的文件扩展名集合（含点，小写）。
        script: John ``run`` 目录中的可执行文件或脚本名。
        interpreter: ``None`` 表示原生 exe，``"python"`` 表示用当前 Python
            解释器执行，``"perl"`` 表示用便携 Perl 执行。
        resources: 运行该提取器前必须就绪的资源键。
        hash_kind: 哈希家族名称，用于查找 OpenCL 格式。
    """

    key: str
    title: str
    extensions: frozenset
    script: str
    interpreter: Optional[str]
    resources: Tuple[str, ...]
    hash_kind: str


#: 所有受支持的提取器。
TOOLS: Tuple[HashTool, ...] = (
    HashTool(
        key="zip",
        title="ZIP 压缩包",
        extensions=frozenset({".zip"}),
        script="zip2john.exe",
        interpreter=None,
        resources=("jtr",),
        hash_kind="zip",
    ),
    HashTool(
        key="rar",
        title="RAR 压缩包",
        extensions=frozenset({".rar"}),
        script="rar2john.exe",
        interpreter=None,
        resources=("jtr",),
        hash_kind="rar",
    ),
    HashTool(
        key="7z",
        title="7-Zip 压缩包",
        extensions=frozenset({".7z"}),
        script="7z2john.pl",
        interpreter="perl",
        resources=("jtr", "perl"),
        hash_kind="7z",
    ),
    HashTool(
        key="pdf",
        title="PDF 文档",
        extensions=frozenset({".pdf"}),
        script="pdf2john.pl",
        interpreter="perl",
        resources=("jtr", "perl"),
        hash_kind="pdf",
    ),
    HashTool(
        key="office",
        title="Microsoft Office 文档",
        extensions=frozenset(
            {".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm", ".doc", ".xls", ".ppt"}
        ),
        script="office2john.py",
        interpreter="python",
        resources=("jtr",),
        hash_kind="office",
    ),
)

#: 扩展名（小写、含点）到提取器的映射。
EXTENSION_MAP: Dict[str, HashTool] = {
    ext: tool for tool in TOOLS for ext in tool.extensions
}

#: 按扩展名排序后的受支持扩展名列表，供界面文件过滤使用。
SUPPORTED_EXTENSIONS: Tuple[str, ...] = tuple(sorted(EXTENSION_MAP))

# ---------------------------------------------------------------------------
# OpenCL 格式映射
# ---------------------------------------------------------------------------

#: 哈希家族到 John OpenCL 格式名的映射。
#: 未出现在此表中的家族（例如 pkzip）只能使用 CPU 格式。
OPENCL_FORMATS: Dict[str, str] = {
    "zip": "ZIP-opencl",
    "rar": "rar-opencl",
    "rar5": "RAR5-opencl",
    "7z": "7z-opencl",
    "pdf": "pdf-opencl",
    "office": "office-opencl",
    "oldoffice": "oldoffice-opencl",
}

#: 从哈希行前缀判断哈希家族，顺序敏感（``$rar5$`` 必须先于 ``$rar$``）。
_HASH_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("$rar5$", "rar5"),
    ("$rar$", "rar"),
    ("$zip$", "zip"),
    ("$zip2$", "zip"),
    ("$zip3$", "zip"),
    ("$pkzip$", "pkzip"),
    ("$pkzip2$", "pkzip"),
    ("$7z$", "7z"),
    ("$pdf$", "pdf"),
    ("$oldoffice$", "oldoffice"),
    ("$office$", "office"),
)

# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------


@dataclass
class HashResult:
    """一次哈希提取的结果。

    属性:
        ok: 是否成功得到可用的哈希行。
        hash_line: 第一条哈希行（``文件名:$xxx$...``），用于判断格式。
        lines: 全部哈希行。一个压缩包可能有多个加密条目，John 需要全部哈希
            才能逐个尝试，因此这些行都会被写入哈希文件。
        kind: 哈希家族（见 :func:`classify_hash`），未知时为 ``None``。
        message: 面向用户的中文说明（失败原因或简要信息）。
        stderr: 提取器的标准错误输出，便于排查问题。
        tool: 实际使用的提取器名称。
    """

    ok: bool
    hash_line: str = ""
    lines: List[str] = field(default_factory=list)
    kind: Optional[str] = None
    message: str = ""
    stderr: str = ""
    tool: str = ""


# ---------------------------------------------------------------------------
# 提取器调用
# ---------------------------------------------------------------------------


@dataclass
class ExtractionContext:
    """调用提取器时需要的运行时上下文。

    属性:
        run_dir: John ``run`` 目录（提取器脚本与依赖 DLL 都在这里）。
        python_exe: 执行 ``.py`` 提取器的解释器路径。
        perl_exe: 执行 ``.pl`` 提取器的解释器路径（未安装时为 ``None``）。
        perl_env: 便携 Perl 需要的环境变量。
        timeout: 单次提取的超时秒数。
    """

    run_dir: Path
    python_exe: str
    perl_exe: Optional[Path] = None
    perl_env: Optional[Dict[str, str]] = None
    timeout: int = config.HASH_TIMEOUT


def build_command(tool: HashTool, ctx: ExtractionContext, path: str) -> List[str]:
    """根据提取器类型生成完整的命令行参数列表。

    参数:
        tool: 要使用的提取器。
        ctx: 运行时上下文（解释器路径等）。
        path: 待处理文件的完整路径。

    返回:
        可直接传给 :func:`subprocess.run` 的参数列表。
    """
    script = str(ctx.run_dir / tool.script)
    if tool.interpreter is None:
        return [script, path]
    if tool.interpreter == "python":
        return [ctx.python_exe, script, path]
    if tool.interpreter == "perl":
        if not ctx.perl_exe:
            raise RuntimeError("该格式需要 Perl 运行时，但便携 Perl 尚未就绪")
        return [str(ctx.perl_exe), script, path]
    raise RuntimeError(f"未知的解释器类型: {tool.interpreter}")


def run_extractor(tool: HashTool, ctx: ExtractionContext, path: str) -> HashResult:
    """调用提取器并解析结果。

    参数:
        tool: 要使用的提取器。
        ctx: 运行时上下文。
        path: 待处理文件的完整路径。

    返回:
        解析后的 :class:`HashResult`。
    """
    try:
        command = build_command(tool, ctx, path)
    except RuntimeError as exc:
        return HashResult(ok=False, message=str(exc), tool=tool.key)

    env = ctx.perl_env if tool.interpreter == "perl" else None
    try:
        completed = subprocess.run(
            command,
            cwd=str(ctx.run_dir),
            capture_output=True,
            text=True,
            timeout=ctx.timeout,
            env=env,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return HashResult(
            ok=False,
            message=f"哈希提取超时（超过 {ctx.timeout} 秒）",
            tool=tool.key,
        )
    except FileNotFoundError as exc:
        return HashResult(
            ok=False,
            message=f"找不到提取器或解释器: {exc}",
            tool=tool.key,
        )
    except Exception as exc:
        return HashResult(ok=False, message=f"哈希提取失败: {exc}", tool=tool.key)

    return parse_output(completed.stdout or "", completed.stderr or "", tool, path)


# ---------------------------------------------------------------------------
# 输出解析
# ---------------------------------------------------------------------------

#: 提取器输出中表示“文件未加密”的关键字。
_NOT_PROTECTED_PATTERNS = (
    "not password protected",
    "not encrypted",
    "zip container found",
    "no aes data found",
    "did not find any encrypted",
)

#: 提取器输出中表示“格式不受支持”的关键字。
_UNSUPPORTED_PATTERNS = (
    "not supported",
    "unsupported",
    "invalid ole file",
    "no supported streams found",
    "non-handled",
    "did not find",
    "unable to obtain hash",
)


def normalize_login(line: str, path: Optional[str] = None) -> str:
    """把哈希行中“含冒号的登录名”替换为文件名。

    John 用第一个冒号分隔登录名与哈希，因此形如 ``C:/dir/file.pdf:$pdf$...``
    的登录名会被误认为登录名是 ``C``，导致 “No password hashes loaded”。
    PDF 的 ``pdf2john.pl`` 正是输出这种带盘符的路径，这里统一规范化成
    ``file.pdf:$pdf$...``。

    参数:
        line: 提取器输出的一行哈希。
        path: 被处理的文件路径（用于取文件名）。

    返回:
        规范化后的哈希行（无需修改时原样返回）。
    """
    if ":$" not in line:
        return line
    login, rest = line.split(":$", 1)
    if ":" not in login:
        return line
    source = (path or login).replace("\\", "/")
    name = os.path.basename(source) or os.path.basename(login.replace("\\", "/"))
    return f"{name}:${rest}"


def parse_output(
    stdout: str,
    stderr: str,
    tool: HashTool,
    path: Optional[str] = None,
) -> HashResult:
    """解析提取器的标准输出与标准错误。

    成功时返回第一行包含 ``:$`` 的哈希行；失败时根据关键字判断是“未加密”
    还是“格式不受支持”，并给出中文提示。

    参数:
        stdout: 提取器的标准输出。
        stderr: 提取器的标准错误。
        tool: 产生该输出的提取器。
        path: 被处理的文件路径（用于规范化登录名）。

    返回:
        解析后的 :class:`HashResult`。
    """
    hash_line = ""
    lines: List[str] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if ":$" in line:
            normalized = normalize_login(line, path)
            lines.append(normalized)
            if not hash_line:
                hash_line = normalized
        # 少数提取器（例如 zip2john）会把哈希输出到标准输出但没有冒号前缀，
        # 这里只接受以 $ 开头的哈希串。
        elif line.startswith("$") and line.count("*") >= 2:
            synthesized = f"{os.path.basename(tool.script)}:{line}"
            lines.append(synthesized)
            if not hash_line:
                hash_line = synthesized

    if hash_line:
        return HashResult(
            ok=True,
            hash_line=hash_line,
            lines=lines,
            kind=classify_hash(hash_line),
            message="已生成哈希",
            stderr=stderr,
            tool=tool.key,
        )

    lowered = f"{stdout}\n{stderr}".lower()
    if any(token in lowered for token in _NOT_PROTECTED_PATTERNS):
        return HashResult(
            ok=False,
            message="文件未设置密码保护（无需破解）",
            stderr=stderr,
            tool=tool.key,
        )
    if any(token in lowered for token in _UNSUPPORTED_PATTERNS):
        return HashResult(
            ok=False,
            message="该文件的加密方式不受支持",
            stderr=stderr,
            tool=tool.key,
        )
    detail = (stderr or stdout).strip().splitlines()
    detail_text = detail[0][:200] if detail else "提取器没有输出哈希"
    return HashResult(
        ok=False,
        message=f"无法提取哈希：{detail_text}",
        stderr=stderr,
        tool=tool.key,
    )


def classify_hash(hash_line: str) -> Optional[str]:
    """根据哈希行前缀判断哈希家族。

    参数:
        hash_line: 形如 ``文件名:$zip$...`` 的哈希行。

    返回:
        家族名称（``zip``、``rar``、``rar5``、``7z``、``pdf``、``office``、
        ``oldoffice``、``pkzip``），无法识别时返回 ``None``。
    """
    for prefix, kind in _HASH_PREFIXES:
        if prefix in hash_line:
            return kind
    return None


def opencl_format(kind: Optional[str]) -> Optional[str]:
    """返回哈希家族对应的 OpenCL 格式名，不支持显卡加速时返回 ``None``。

    参数:
        kind: :func:`classify_hash` 返回的家族名称。
    """
    if not kind:
        return None
    return OPENCL_FORMATS.get(kind)


def tool_for(path: str) -> Optional[HashTool]:
    """按文件扩展名返回对应的提取器，未知扩展名返回 ``None``。

    参数:
        path: 文件路径（大小写不敏感）。
    """
    return EXTENSION_MAP.get(Path(path).suffix.lower())


def describe_supported() -> str:
    """返回用于界面提示的受支持扩展名说明。"""
    return "、".join(ext.lstrip(".").upper() for ext in SUPPORTED_EXTENSIONS)
