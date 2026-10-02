"""发布：把草稿发到 GitHub（普通评论或 PR review），并补上 AI 声明。"""

from __future__ import annotations

import re
from typing import Any

from .client import GitHubClient

DISCLOSURE_MARKERS = ("AI 生成", "AI-generated", "本评论由 AI")

# 疑似凭证：命中直接阻断发布
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)(?:token|secret|password)\s*[:=]\s*['\"][^'\"]{12,}['\"]"),
)

# 绝对措辞：只提醒，不阻断
ABSOLUTE_WORDS = ("完全", "都", "一定", "绝对", "所有", "从不", "永远", "必然")

LOCATION_RE = re.compile(r"[\w./-]+\.\w+:\d+")


def ensure_disclosure(body: str, disclosure: str) -> str:
    """末尾补 AI 声明；已有类似声明时不重复追加。"""
    text = (body or "").strip()
    note = (disclosure or "").strip()
    if not note:
        return text
    if note in text or any(marker in text for marker in DISCLOSURE_MARKERS):
        return text
    return f"{text}\n\n---\n{note}"


def self_check(body: str, *, kind: str = "issue") -> tuple[list[str], list[str]]:
    """发布前自检 → (阻断项, 提醒项)；阻断项非空时不要发布。"""
    blocks: list[str] = []
    warnings: list[str] = []
    text = str(body or "").strip()

    if not text:
        blocks.append("正文为空")
        return blocks, warnings

    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            blocks.append("正文里疑似出现 token / 密钥")
            break

    if kind == "pr" and not LOCATION_RE.search(text):
        warnings.append("没看到 `文件:行号` 形式的位置引用")

    hits = [word for word in ABSOLUTE_WORDS if word in text]
    if hits:
        warnings.append("含绝对措辞：" + "、".join(hits))
    return blocks, warnings


async def publish(
    client: GitHubClient,
    *,
    repo: str,
    number: int,
    body: str,
    kind: str,
    as_review: bool = False,
    event: str = "COMMENT",
) -> dict[str, Any]:
    """PR 且 ``as_review`` 时提交 review，其余情况发普通评论。"""
    if kind == "pr" and as_review:
        return await client.create_review(repo, number, body, event=event)
    return await client.post_comment(repo, number, body)
