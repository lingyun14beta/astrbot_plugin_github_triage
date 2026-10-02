"""读取**插件内置**的 Skill 文本。

按需求，这里**不做**本地 ``data/skills`` 回退 —— 说明书跟着插件走，改插件目录即可。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

ROOT_SKILL = "gh-triage"
FORMAT_SKILLS = ("gh-issue-format", "gh-code-review")

# 改动文件后缀 → code-review 里的参考文件（按需挑，不整包注入）
EXT_REFERENCE: dict[str, str] = {
    ".py": "reference/python.md",
    ".pyi": "reference/python.md",
    ".vue": "reference/vue.md",
    ".ts": "reference/typescript.md",
    ".tsx": "reference/typescript.md",
    ".js": "reference/typescript.md",
    ".jsx": "reference/react.md",
    ".rs": "reference/rust.md",
    ".go": "reference/go.md",
    ".java": "reference/java.md",
    ".c": "reference/c.md",
    ".h": "reference/c.md",
    ".cc": "reference/cpp.md",
    ".cpp": "reference/cpp.md",
    ".hpp": "reference/cpp.md",
    ".css": "reference/css-less-sass.md",
    ".scss": "reference/css-less-sass.md",
    ".less": "reference/css-less-sass.md",
    ".qml": "reference/qt.md",
    ".ui": "reference/qt.md",
}

# 命中这些关键词的改动额外带上安全清单
SECURITY_KEYWORDS = (
    "auth",
    "token",
    "secret",
    "crypto",
    "password",
    "permission",
    "sandbox",
    "upload",
    "download",
    "eval",
    "exec",
    "subprocess",
    "path",
    "sql",
)


class SkillMissing(Exception):
    """内置 Skill 缺失，说明插件目录被改动过。"""


def _strip_frontmatter(text: str) -> str:
    """去掉 YAML frontmatter：正文才是要注入的指令。"""
    if not text.startswith("---"):
        return text
    parts = text.split("---", 2)
    return parts[2].strip() if len(parts) == 3 else text


def list_skills() -> list[str]:
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(p.name for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())


def read_skill(name: str) -> str:
    path = SKILLS_DIR / name / "SKILL.md"
    if not path.is_file():
        raise SkillMissing(f"内置 Skill 缺失：{name}（应为 {path}）")
    return _strip_frontmatter(path.read_text(encoding="utf-8"))


def read_reference(skill: str, filename: str) -> str | None:
    path = SKILLS_DIR / skill / filename
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def pick_references(paths: Iterable[str], *, limit: int = 3) -> list[str]:
    """按改动文件后缀挑 code-review 的参考文件，最多 ``limit`` 份。"""
    picked: list[str] = []
    for raw in paths:
        ref = EXT_REFERENCE.get(Path(str(raw)).suffix.lower())
        if ref and ref not in picked:
            picked.append(ref)
        if len(picked) >= limit:
            break

    lowered = " ".join(str(p).lower() for p in paths)
    if (
        any(word in lowered for word in SECURITY_KEYWORDS)
        and "reference/security-review-guide.md" not in picked
    ):
        picked.append("reference/security-review-guide.md")
    return picked
