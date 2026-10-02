"""抓取层纯逻辑用例：自动扫描信号、模板空字段、digest 渲染。不需要 AstrBot 运行时。"""

from __future__ import annotations

import asyncio

from astrbot_plugin_github_triage.gh import fetch

HIGH_RISK_DIFF = """diff --git a/app/core.py b/app/core.py
--- a/app/core.py
+++ b/app/core.py
@@ -1,2 +1,8 @@
+try:
+    risky()
+except Exception:
+    pass
+subprocess.run(cmd, shell=True)
-    assert user is not None
diff --git a/tests/test_core.py b/tests/test_core.py
--- a/tests/test_core.py
+++ b/tests/test_core.py
+def test_new():
+    pass
"""

CLEAN_DIFF = """diff --git a/app/math.py b/app/math.py
--- a/app/math.py
+++ b/app/math.py
+def add(a, b):
+    return a + b
"""


def test_scan_signals_catches_high_risk_patterns():
    signals = fetch.scan_signals(HIGH_RISK_DIFF, [{"filename": "tests/test_core.py"}])
    assert "空异常兜底（except → pass）" in signals
    assert "shell=True" in signals
    assert "删除了断言" in signals
    assert "改动测试文件" in signals


def test_scan_signals_is_quiet_on_clean_diff():
    assert fetch.scan_signals(CLEAN_DIFF, [{"filename": "app/math.py"}]) == []


def test_scan_signals_flags_dependency_and_ci_changes():
    signals = fetch.scan_signals(
        "", [{"filename": "requirements.txt"}, {"filename": ".github/workflows/ci.yml"}]
    )
    assert "改动依赖或构建清单" in signals
    assert "改动 CI 配置" in signals


def test_find_empty_fields_reads_github_form_placeholders():
    body = "### 问题描述\n\n有 bug\n\n### 复现步骤\n\n_No response_\n\n### 版本\n\n_No response_"
    assert fetch.find_empty_fields(body) == ["复现步骤", "版本"]


def test_find_empty_fields_returns_nothing_for_filled_template():
    assert fetch.find_empty_fields("### 问题描述\n\n有 bug\n") == []


def test_render_markdown_includes_head_sha_and_signals():
    digest = {
        "kind": "pr",
        "repo": "o/r",
        "number": 42,
        "title": "t",
        "author": "a",
        "state": "open",
        "draft": False,
        "mergeable": True,
        "base": "master",
        "head": "fix/x",
        "base_sha": "95e98b8a",
        "head_sha": "0b398ad9",
        "changed_files": 1,
        "additions": 5,
        "deletions": 1,
        "files": [
            {"filename": "a.py", "status": "modified", "additions": 5, "deletions": 1}
        ],
        "commits": [{"sha": "0b398ad9", "message": "fix: x"}],
        "diff": "+x",
        "diff_truncated": False,
        "signals": ["shell=True"],
        "url": "https://example.invalid",
        "body": "b",
    }
    md = fetch.render_markdown(digest)
    assert "head SHA：0b398ad9" in md
    assert "自动扫描命中的信号" in md
    assert "shell=True" in md


def test_issue_digest_keeps_body_and_flags_empty_fields():
    issue = {
        "number": 7,
        "title": "bug",
        "user": {"login": "reporter"},
        "state": "open",
        "labels": [{"name": "bug"}],
        "body": "### 问题描述\n\n崩溃\n\n### 版本\n\n_No response_",
        "html_url": "https://example.invalid/7",
    }
    digest = fetch.issue_digest(
        issue, [{"user": {"login": "other"}, "body": "我也遇到了"}]
    )
    assert digest["labels"] == ["bug"]
    assert digest["empty_fields"] == ["版本"]
    assert digest["comments"][0]["author"] == "other"


class FakePullClient:
    """只实现 pr_digest 需要的那四个方法。"""

    def __init__(self, *, changed_files: int, return_files: int) -> None:
        self.pull = {"number": 7, "title": "t", "changed_files": changed_files}
        self.return_files = return_files

    async def get_pull(self, repo, number):
        return dict(self.pull)

    async def get_pull_files(self, repo, number):
        return [
            {"filename": f"f{i}.py", "status": "modified"}
            for i in range(self.return_files)
        ]

    async def get_pull_commits(self, repo, number):
        return []

    async def get_pull_diff(self, repo, number):
        return "+x"


def test_pr_digest_marks_truncated_file_list():
    async def run():
        client = FakePullClient(changed_files=250, return_files=100)
        digest = await fetch.pr_digest(client, "o/r", 7)

        assert digest["changed_files"] == 250
        assert digest["files_truncated"] is True
        md = fetch.render_markdown({**digest, "signals": []})
        assert "只列出前 100 个" in md

    asyncio.run(run())


def test_pr_digest_has_no_truncation_flag_when_complete():
    async def run():
        client = FakePullClient(changed_files=3, return_files=3)
        digest = await fetch.pr_digest(client, "o/r", 7)

        assert digest["files_truncated"] is False
        assert "只列出前" not in fetch.render_markdown({**digest, "signals": []})

    asyncio.run(run())
