"""发布层纯逻辑用例：AI 声明补齐与发布前自检。不需要 AstrBot 运行时。"""

from __future__ import annotations

from astrbot_plugin_github_triage.gh import publish

FAKE_TOKEN = "ghp_" + "a" * 24


def test_ensure_disclosure_appends_when_missing():
    body = publish.ensure_disclosure("**结论**：可以合并", "> 🤖 本评论由 AI 生成")
    assert body.endswith("> 🤖 本评论由 AI 生成")


def test_ensure_disclosure_does_not_duplicate():
    body = publish.ensure_disclosure(
        "好的\n\n> 🤖 本评论由 AI 生成", "> 🤖 本评论由 AI 生成"
    )
    assert body.count("本评论由 AI 生成") == 1


def test_ensure_disclosure_skips_when_configured_empty():
    assert publish.ensure_disclosure("正文", "") == "正文"


def test_self_check_blocks_empty_body():
    blocks, _ = publish.self_check("   ")
    assert blocks == ["正文为空"]


def test_self_check_blocks_token_leak():
    blocks, _ = publish.self_check(f"这是我的 token {FAKE_TOKEN}", kind="pr")
    assert any("token" in item for item in blocks)


def test_self_check_allows_normal_pr_body():
    body = "**结论**：可合并\n\n🟡 `gh/fetch.py:132` 少了 head SHA。"
    blocks, _ = publish.self_check(body, kind="pr")
    assert blocks == []


def test_self_check_warns_on_missing_location_and_absolute_wording():
    blocks, warns = publish.self_check(
        "**结论**：完全没问题，所有路径都覆盖了。", kind="pr"
    )
    assert blocks == []
    assert any("文件:行号" in item for item in warns)
    assert any("绝对措辞" in item for item in warns)


def test_self_check_does_not_require_location_for_issues():
    _, warns = publish.self_check("**需求本质**：希望能配置。", kind="issue")
    assert all("文件:行号" not in item for item in warns)
