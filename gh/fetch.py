"""把 GitHub 的原始 JSON 整理成给模型看的 digest。"""

from __future__ import annotations

import re
from typing import Any

from .client import GitHubClient

BODY_LIMIT = 8000
COMMENT_LIMIT = 10
NO_RESPONSE_MARKERS = ("_No response_", "_No Response_", "No response", "无回复")
HEADING_RE = re.compile(r"^#{2,4}\s*(.+?)\s*$")
DIFF_PLACEHOLDER = "（diff 已截断：仅保留前面的文件，其余未送入模型）"

# 高危信号：自动扫描只是**候选**，写进评论前必须逐条核实
SIGNAL_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"except\s*(?:Exception|BaseException)?\s*:\s*(?:\n\s*)?pass\b",
        "空异常兜底（except → pass）",
    ),
    (r"except\s*BaseException", "捕获 BaseException"),
    (r"shell\s*=\s*True", "shell=True"),
    (r"\bos\.system\(", "os.system 调用"),
    (r"\beval\(|\bexec\(", "eval / exec"),
    (r"verify\s*=\s*False", "关闭 TLS 校验"),
    (r"#\s*noqa|#\s*type:\s*ignore", "绕过静态检查的注释"),
    (r"\bassert\s+\w", "新增断言"),
)
SIGNAL_FILE_RULES: tuple[tuple[str, str], ...] = (
    (r"(^|/)tests?/|(^|/)test_[^/]*\.py$|[^/]*_test\.(?:py|go|ts|js)$", "改动测试文件"),
    (
        r"(^|/)requirements[^/]*\.txt$|(^|/)pyproject\.toml$|(^|/)package\.json$",
        "改动依赖或构建清单",
    ),
    (r"^\.github/workflows/", "改动 CI 配置"),
)


def clip(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原文 {len(text)} 字符）"


def find_empty_fields(body: str) -> list[str]:
    """找出 issue 模板里没填的字段（GitHub 表单会把空字段渲染成 ``_No response_``）。"""
    empty: list[str] = []
    current: str | None = None
    for line in str(body or "").splitlines():
        stripped = line.strip()
        heading = HEADING_RE.match(stripped)
        if heading:
            current = heading.group(1)
            continue
        if current is None or not stripped:
            continue
        if any(marker in stripped for marker in NO_RESPONSE_MARKERS):
            empty.append(current)
        current = None
    return empty


def scan_signals(diff: str, files: list[dict[str, Any]] | None = None) -> list[str]:
    """扫 diff 新增行与改动文件名，列出高危信号。

    这些只是**候选**：写进评论前必须逐条核实是否成立。
    """
    hits: list[str] = []
    lines = str(diff or "").splitlines()
    added = "\n".join(
        line[1:]
        for line in lines
        if line.startswith("+") and not line.startswith("+++")
    )
    removed = "\n".join(
        line[1:]
        for line in lines
        if line.startswith("-") and not line.startswith("---")
    )

    for pattern, label in SIGNAL_PATTERNS:
        if label not in hits and re.search(pattern, added):
            hits.append(label)
    if re.search(r"\bassert\s+\w", removed):
        hits.append("删除了断言")

    for item in files or []:
        name = str(item.get("filename") or "")
        for pattern, label in SIGNAL_FILE_RULES:
            if label not in hits and re.search(pattern, name):
                hits.append(label)
    return hits


def issue_digest(
    issue: dict[str, Any], comments: list[dict[str, Any]]
) -> dict[str, Any]:
    body = str(issue.get("body") or "")
    return {
        "kind": "issue",
        "number": int(issue.get("number") or 0),
        "title": str(issue.get("title") or ""),
        "author": str((issue.get("user") or {}).get("login") or ""),
        "state": str(issue.get("state") or ""),
        "labels": [
            str((label or {}).get("name") or "")
            for label in (issue.get("labels") or [])
        ],
        "body": clip(body, BODY_LIMIT),
        "empty_fields": find_empty_fields(body),
        "comments": [
            {
                "author": str((c.get("user") or {}).get("login") or ""),
                "body": clip(str(c.get("body") or ""), 1200),
            }
            for c in (comments or [])[:COMMENT_LIMIT]
        ],
        "url": str(issue.get("html_url") or ""),
    }


async def pr_digest(
    client: GitHubClient,
    repo: str,
    number: int,
    *,
    max_diff_chars: int = 60000,
) -> dict[str, Any]:
    pull = await client.get_pull(repo, number)
    files = await client.get_pull_files(repo, number)
    commits = await client.get_pull_commits(repo, number)
    diff = await client.get_pull_diff(repo, number)

    truncated = len(diff) > max_diff_chars
    changed_files = int(pull.get("changed_files") or len(files))
    # GitHub 的改动文件列表有分页上限：被截断时必须讲出来，否则模型会以为列全了
    files_truncated = len(files) < changed_files
    return {
        "kind": "pr",
        "number": int(pull.get("number") or number),
        "title": str(pull.get("title") or ""),
        "author": str((pull.get("user") or {}).get("login") or ""),
        "state": str(pull.get("state") or ""),
        "draft": bool(pull.get("draft")),
        "mergeable": pull.get("mergeable"),
        "base": str(((pull.get("base") or {}).get("ref")) or ""),
        "head": str(((pull.get("head") or {}).get("ref")) or ""),
        "base_sha": str(((pull.get("base") or {}).get("sha")) or "")[:8],
        "head_sha": str(((pull.get("head") or {}).get("sha")) or "")[:8],
        "body": clip(str(pull.get("body") or ""), BODY_LIMIT),
        "changed_files": changed_files,
        "files_truncated": files_truncated,
        "additions": int(pull.get("additions") or 0),
        "deletions": int(pull.get("deletions") or 0),
        "files": [
            {
                "filename": str(f.get("filename") or ""),
                "status": str(f.get("status") or ""),
                "additions": int(f.get("additions") or 0),
                "deletions": int(f.get("deletions") or 0),
            }
            for f in files
        ],
        "commits": [
            {
                "sha": str(c.get("sha") or "")[:8],
                "message": str(
                    (c.get("commit") or {}).get("message") or ""
                ).splitlines()[0],
            }
            for c in commits
        ],
        "diff": diff[:max_diff_chars] + ("\n" + DIFF_PLACEHOLDER if truncated else ""),
        "diff_truncated": truncated,
        "signals": scan_signals(diff, files),
        "url": str(pull.get("html_url") or ""),
    }


def render_markdown(digest: dict[str, Any]) -> str:
    """把 digest 渲染成给模型的 Markdown 原文（不改写作者措辞）。"""
    repo = digest.get("repo", "")
    lines = [f"# {repo}#{digest['number']} {digest['title']}", ""]

    if digest["kind"] == "issue":
        lines.append(
            f"- 类型：issue｜作者：{digest['author']}｜状态：{digest['state']}"
        )
        if digest["labels"]:
            lines.append(f"- 现有标签：{'、'.join(digest['labels'])}")
        lines += ["", "## 正文原文", digest["body"] or "（空）"]
        if digest["empty_fields"]:
            lines += ["", "## 模板里未填的字段", "、".join(digest["empty_fields"])]
        if digest["comments"]:
            lines += ["", "## 评论"]
            lines += [f"- @{c['author']}：{c['body']}" for c in digest["comments"]]
        return "\n".join(lines)

    lines.append(
        f"- 类型：PR｜作者：{digest['author']}｜{digest['base']} ← {digest['head']}"
        f"｜head SHA：{digest.get('head_sha') or '未知'}"
        f"｜{digest['changed_files']} 文件 +{digest['additions']}/-{digest['deletions']}"
        f"｜draft={digest['draft']}｜mergeable={digest['mergeable']}"
    )
    lines += ["", "## 作者描述原文", digest["body"] or "（空）"]

    lines += ["", "## 改动文件"]
    lines += [
        f"- {f['status']} {f['filename']} (+{f['additions']}/-{f['deletions']})"
        for f in digest["files"]
    ]

    lines += ["", "## 提交"]
    lines += [f"- {c['sha']} {c['message']}" for c in digest["commits"]]

    if digest.get("files_truncated"):
        listed = len(digest.get("files") or [])
        lines += [
            "",
            f"> 注意：改动文件只列出前 {listed} 个（共 {digest.get('changed_files')} 个），未列出的文件不要写进结论。",
        ]

    signals = digest.get("signals") or []
    if signals:
        lines += ["", "## 自动扫描命中的信号（候选，需核实）"]
        lines += [f"- {item}" for item in signals]

    lines += ["", "## diff", "```diff", digest["diff"] or "（无）", "```"]
    return "\n".join(lines)
