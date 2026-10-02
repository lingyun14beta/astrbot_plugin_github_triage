"""给模型的只读工具：读文件 / 搜代码 / 列目录（可选：跑白名单命令）。

安全边界（跟评论里常说的"路径穿越"是同一类问题，自己先守住）：

- 所有路径经 ``resolve()`` 后必须落在 workspace root 内，越界直接拒绝；
- 读取与搜索都有行数 / 命中数 / 输出上限，避免把上下文撑爆；
- 命令执行默认关闭；开启时也只允许白名单 argv，``shell=False``，带超时与输出上限。

这些工具让模型能看到 diff 以外的上下文（整个函数、调用方、相邻文件），
而不是只对着一小段 diff 猜。
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

MAX_LINES = 400
MAX_HITS = 60
MAX_OUTPUT = 20000
MAX_FILE_BYTES = 400_000
COMMAND_TIMEOUT = 180.0

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".turbo",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
BINARY_SUFFIX = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".bmp",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".whl",
    ".so",
    ".dll",
    ".exe",
    ".pyc",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".mp3",
    ".mp4",
    ".mov",
    ".wav",
}


class PathOutsideRoot(Exception):
    """路径越界：解析后不在 workspace root 内。"""


def resolve_inside(root: Path | str, raw: str) -> Path:
    """把相对路径解析到 root 内；越界抛 ``PathOutsideRoot``。"""
    base = Path(str(root)).resolve()
    candidate = (base / str(raw or ".").strip()).resolve()
    if candidate != base and not candidate.is_relative_to(base):
        raise PathOutsideRoot(f"路径越界，已拒绝：{raw}")
    return candidate


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（输出已截断）"


def read_lines(root: Path | str, raw: str, start: int = 1, end: int = 0) -> str:
    """按行区间读文件，返回带行号的内容。``end <= 0`` 表示自动取 ``MAX_LINES`` 行。"""
    path = resolve_inside(root, raw)
    if not path.is_file():
        return f"不是文件：{raw}"
    if path.stat().st_size > MAX_FILE_BYTES:
        return f"文件太大（{path.stat().st_size} 字节），请改用搜索。"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"读取失败：{exc}"

    first = max(1, int(start or 1))
    last = int(end or 0)
    if last <= 0:
        last = min(len(lines), first + MAX_LINES - 1)
    last = min(last, first + MAX_LINES - 1, len(lines))
    if first > len(lines):
        return f"共 {len(lines)} 行，起始行 {first} 超出范围。"

    body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(first, last + 1))
    return f"{raw}（第 {first}-{last} 行，共 {len(lines)} 行）\n{body}"


def search(
    root: Path | str, pattern: str, glob: str = "*", max_hits: int = MAX_HITS
) -> str:
    """在 root 内按正则搜索文本文件，返回 ``文件:行号: 内容``。"""
    base = Path(str(root)).resolve()
    try:
        rx = re.compile(str(pattern))
    except re.error as exc:
        return f"正则不合法：{exc}"

    limit = max(1, min(int(max_hits or MAX_HITS), 200))
    hits: list[str] = []
    for path in sorted(base.rglob("*")):
        if len(hits) >= limit:
            break
        if not path.is_file():
            continue
        rel_parts = set(path.relative_to(base).parts)  # 只看仓库内路径，
        # 否则仓库上层目录碰巧叫 build / dist 时整仓都会被跳过
        if rel_parts & SKIP_DIRS:
            continue
        if path.suffix.lower() in BINARY_SUFFIX:
            continue
        rel = path.relative_to(base).as_posix()
        if (
            glob
            and glob != "*"
            and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(path.name, glob))
        ):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                hits.append(f"{rel}:{i}: {line.strip()[:180]}")
                if len(hits) >= limit:
                    break

    if not hits:
        return "没有命中。"
    return _clip("\n".join(hits))


def list_dir(root: Path | str, raw: str = ".") -> str:
    """列出目录内容（目录带 ``/`` 后缀）。"""
    path = resolve_inside(root, raw)
    if not path.is_dir():
        return f"不是目录：{raw}"
    entries = []
    for child in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name)):
        if child.name in SKIP_DIRS:
            continue
        entries.append(child.name + ("/" if child.is_dir() else ""))
        if len(entries) >= 200:
            entries.append("…（条目过多，已截断）")
            break
    rel = path.relative_to(Path(str(root)).resolve()).as_posix() or "."
    return f"{rel}/ 下共 {len(entries)} 项：\n" + "\n".join(entries)


def _build_argv(kind: str, target: str, python: str | None = None) -> list[str] | None:
    """白名单：只有这些 argv 能被拼出来，其余一律拒绝。

    ``python`` 指定跑 ruff / pytest 的解释器：待审仓库的依赖通常在它自己的虚拟环境里，
    用 AstrBot 的解释器会得到一堆 import 错误。
    """
    exe = python or sys.executable
    if kind == "ruff_check":
        return [exe, "-m", "ruff", "check", target]
    if kind == "ruff_format_check":
        return [exe, "-m", "ruff", "format", "--check", target]
    if kind == "pytest":
        return [exe, "-m", "pytest", target, "-q", "--no-header", "-x"]
    if kind == "git_show":
        return ["git", "show", "--stat", "--oneline", "HEAD"]
    if kind == "git_log":
        return ["git", "log", "-n", "20", "--oneline"]
    return None


async def run_check(
    root: Path | str, kind: str, target: str = ".", python: str = ""
) -> str:
    """跑一条白名单命令（不经过 shell），返回输出。"""
    base = Path(str(root)).resolve()
    safe_target = "."
    if kind in ("ruff_check", "ruff_format_check", "pytest"):
        try:
            path = resolve_inside(base, target or ".")
        except PathOutsideRoot as exc:
            return str(exc)
        if not path.exists():
            return f"路径不存在：{target}"
        safe_target = str(path.relative_to(base)) or "."

    exe = str(python or "").strip()
    if exe and not Path(exe).exists():
        return f"配置的解释器不存在：{exe}"

    argv = _build_argv(str(kind), safe_target, exe or None)
    if argv is None:
        return f"不允许的命令：{kind}（可选：ruff_check / ruff_format_check / pytest / git_show / git_log）"

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(base),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=COMMAND_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return f"命令超时（{COMMAND_TIMEOUT:.0f}s）：{' '.join(argv)}"
    text = (out or b"").decode("utf-8", "replace").strip()
    return _clip(
        f"$ {' '.join(argv)}（退出码 {proc.returncode}）\n{text or '（无输出）'}"
    )


@dataclass
class ReadFileTool(FunctionTool[AstrAgentContext]):
    """读文件指定行区间，补上 diff 看不到的上下文。"""

    name: str = "gh_read_file"
    description: str = (
        "读取本地检出（PR head）里某个文件的行区间，返回带行号的内容。"
        "需要看清某个改动所在函数的完整逻辑时使用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "仓库内相对路径，例如 dashboard/src/a.vue",
                },
                "start_line": {"type": "integer", "description": "起始行，默认 1"},
                "end_line": {
                    "type": "integer",
                    "description": "结束行，0 或省略表示自动取一段",
                },
            },
            "required": ["path"],
        }
    )
    root: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        try:
            return read_lines(
                self.root,
                str(kwargs.get("path") or ""),
                int(kwargs.get("start_line") or 1),
                int(kwargs.get("end_line") or 0),
            )
        except PathOutsideRoot as exc:
            return str(exc)


@dataclass
class SearchCodeTool(FunctionTool[AstrAgentContext]):
    """按正则搜代码，用来找调用方与相关实现。"""

    name: str = "gh_search_code"
    description: str = (
        "在本地检出里按正则搜索文本文件，返回 文件:行号: 内容。"
        "用来找某个函数的调用方、某个配置键的引用、某个字符串出现的位置。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python 正则"},
                "glob": {
                    "type": "string",
                    "description": "文件名过滤，例如 *.py，默认 *",
                },
                "max_hits": {
                    "type": "integer",
                    "description": "最多返回多少条，默认 60",
                },
            },
            "required": ["pattern"],
        }
    )
    root: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        return search(
            self.root,
            str(kwargs.get("pattern") or ""),
            str(kwargs.get("glob") or "*"),
            int(kwargs.get("max_hits") or MAX_HITS),
        )


@dataclass
class ListDirTool(FunctionTool[AstrAgentContext]):
    """看目录结构，确认文件是否存在、命名是否符合预期。"""

    name: str = "gh_list_dir"
    description: str = (
        "列出本地检出里某个目录的内容。确认文件是否存在、新文件放得对不对时使用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "仓库内相对路径，默认根目录"}
            },
            "required": [],
        }
    )
    root: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        try:
            return list_dir(self.root, str(kwargs.get("path") or "."))
        except PathOutsideRoot as exc:
            return str(exc)


@dataclass
class RunCheckTool(FunctionTool[AstrAgentContext]):
    """跑白名单检查命令。默认不启用，需要显式打开配置。"""

    name: str = "gh_run_check"
    description: str = (
        "在本地检出里跑一条只读检查命令并返回输出。"
        "kind 只能取：ruff_check、ruff_format_check、pytest（这三个会用 target 指定路径）、"
        "git_show、git_log。不能执行任意命令。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "命令类型",
                    "enum": [
                        "ruff_check",
                        "ruff_format_check",
                        "pytest",
                        "git_show",
                        "git_log",
                    ],
                },
                "target": {
                    "type": "string",
                    "description": "路径（相对仓库根），默认 .",
                },
            },
            "required": ["kind"],
        }
    )
    root: Any = None
    python: Any = ""

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        try:
            return await run_check(
                self.root,
                str(kwargs.get("kind") or ""),
                str(kwargs.get("target") or "."),
                str(self.python or ""),
            )
        except PathOutsideRoot as exc:
            return str(exc)
