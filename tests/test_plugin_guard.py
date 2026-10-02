"""开关用例：enabled=false 时所有 /gh 子指令都不响应（含发布这种写操作）。

需要 AstrBot 运行时（指令装饰器来自 core），缺运行时时整组跳过。
"""

from __future__ import annotations

import inspect
import os
import tempfile

import pytest

# AstrBot 会按当前工作目录解析 data/ 等运行时路径；先切走，免得污染插件目录
os.chdir(tempfile.mkdtemp(prefix="gh-triage-test-"))
pytest.importorskip("astrbot", reason="指令装饰器来自 AstrBot core，缺运行时时跳过")

from astrbot_plugin_github_triage import main as plugin_main  # noqa: E402

COMMANDS = (
    "gh_help",
    "gh_t",
    "gh_show",
    "gh_list",
    "gh_post",
    "gh_fetch",
    "gh_config",
)


class FakeEvent:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def plain_result(self, text: str):
        self.sent.append(text)
        return ("plain", text)


class FakeSelf:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled


async def _drain(agen):
    return [item async for item in agen]


async def _inner(self, event, *args, **kwargs):
    yield ("business", event, args, kwargs)


def test_all_commands_are_wrapped():
    for name in COMMANDS:
        handler = getattr(plugin_main.GithubTriagePlugin, name)
        assert getattr(handler, "__wrapped__", None) is not None, (
            f"{name} 没加 @requires_enabled"
        )


def test_guard_still_exposes_original_signature():
    """AstrBot 用 inspect.signature 解析指令参数，包装后签名必须原样可见。"""
    params = list(
        inspect.signature(
            plugin_main.GithubTriagePlugin.gh_post, eval_str=True
        ).parameters
    )
    assert params == ["self", "event", "target", "flags"]
    params_t = list(
        inspect.signature(plugin_main.GithubTriagePlugin.gh_t, eval_str=True).parameters
    )
    assert params_t == ["self", "event", "target"]


def test_disabled_blocks_before_business_logic():
    event = FakeEvent()
    out = _drain_sync(plugin_main.requires_enabled(_inner)(FakeSelf(False), event))
    assert len(out) == 1
    assert out[0][0] == "plain"
    assert "已在配置中关闭" in out[0][1]


def test_enabled_forwards_result():
    event = FakeEvent()
    out = _drain_sync(plugin_main.requires_enabled(_inner)(FakeSelf(True), event))
    assert len(out) == 1
    assert out[0][0] == "business"
    assert out[0][1] is event


def test_disabled_flags_gate_all_written_commands():
    """发布路径也必须在开关之内 —— 这是配置 hint 承诺的行为。"""
    event = FakeEvent()
    for name in COMMANDS:
        handler = getattr(plugin_main.GithubTriagePlugin, name)
        out = _drain_sync(handler(FakeSelf(False), event))
        assert len(out) == 1, f"{name} 在关闭状态下仍进入了业务逻辑"
        assert out[0][0] == "plain"


def _drain_sync(agen):
    import asyncio

    return asyncio.run(_drain(agen))


async def _inner_with_target(self, event, target, flags=""):
    """模拟一个慢指令：等 event 放行才产出。"""
    await event.gate.wait()
    yield ("done", target)


class GatedEvent(FakeEvent):
    def __init__(self) -> None:
        super().__init__()
        import asyncio as _asyncio

        self.gate = _asyncio.Event()


def test_dedupe_blocks_second_call_for_same_target():
    import asyncio as _asyncio

    async def run():
        plugin = FakeSelf(True)
        handler = plugin_main.dedupe_running(_inner_with_target)
        first_event, second_event = GatedEvent(), GatedEvent()

        first = _asyncio.ensure_future(
            _drain(handler(plugin, first_event, target="10200"))
        )
        await _asyncio.sleep(0)  # 让第一个进入等待
        second = await _drain(handler(plugin, second_event, target="10200"))

        assert second and second[0][0] == "plain"
        assert "正在处理中" in second[0][1]

        first_event.gate.set()
        assert await first == [("done", "10200")]

        # 释放后可以重新开始
        third_event = GatedEvent()
        third_event.gate.set()
        assert await _drain(handler(plugin, third_event, target="10200")) == [
            ("done", "10200")
        ]

    _asyncio.run(run())


def test_dedupe_allows_different_targets():
    import asyncio as _asyncio

    async def run():
        plugin = FakeSelf(True)
        handler = plugin_main.dedupe_running(_inner_with_target)
        one, two = GatedEvent(), GatedEvent()
        one.gate.set()
        two.gate.set()
        assert await _drain(handler(plugin, one, target="1")) == [("done", "1")]
        assert await _drain(handler(plugin, two, target="2")) == [("done", "2")]

    _asyncio.run(run())


class CountingClient:
    """只要被重复调用 get_issue 就炸。"""

    def __init__(self, issue):
        self.issue = issue
        self.issue_calls = 0

    async def get_issue(self, repo, number):
        self.issue_calls += 1
        if self.issue_calls > 1:
            raise AssertionError("issue 被重复请求了")
        return dict(self.issue)

    async def get_issue_comments(self, repo, number):
        return []


def test_issue_digest_reuses_first_fetch():
    import asyncio as _asyncio

    async def run():
        plugin = FakeSelf(True)
        plugin.client = CountingClient({"number": 7, "title": "bug", "body": "x"})
        plugin.config = {"include_sections": []}

        kind, issue = await plugin_main.GithubTriagePlugin._kind(plugin, "o/r", 7)
        digest, markdown = await plugin_main.GithubTriagePlugin._build_digest(
            plugin, "o/r", 7, kind, issue
        )

        assert kind == "issue"
        assert plugin.client.issue_calls == 1, "issue 只应请求一次"
        assert digest["repo"] == "o/r"
        assert markdown

    _asyncio.run(run())


def test_resolve_target_accepts_all_documented_forms():
    """回归用例：`owner/repo#编号` 曾因正则分组错位取 group(3) 而 IndexError。"""
    plugin = FakeSelf(True)
    plugin.repos = [{"repo": "AstrBotDevs/AstrBot"}]
    resolve = plugin_main.GithubTriagePlugin._resolve_target

    assert resolve(plugin, "AstrBotDevs/AstrBot#10200") == (
        "AstrBotDevs/AstrBot",
        10200,
    )
    assert resolve(plugin, "10200") == ("AstrBotDevs/AstrBot", 10200)
    assert resolve(plugin, "https://github.com/AstrBotDevs/AstrBot/pull/10200") == (
        "AstrBotDevs/AstrBot",
        10200,
    )
    assert resolve(plugin, "https://github.com/o/r/issues/5") == ("o/r", 5)


def test_resolve_target_rejects_unknown_and_ambiguous_input():
    plugin = FakeSelf(True)
    plugin.repos = [{"repo": "a/b"}, {"repo": "c/d"}]
    resolve = plugin_main.GithubTriagePlugin._resolve_target

    with pytest.raises(ValueError, match="多个仓库"):
        resolve(plugin, "10200")
    with pytest.raises(ValueError, match="无法识别"):
        resolve(plugin, "不是目标")


class SkillPlugin(FakeSelf):
    """只给 gh_t 走到 make_draft 所需的字段。"""

    def __init__(self) -> None:
        super().__init__(True)
        self.config = {}
        self.local_paths: dict[str, str] = {}
        self.context = object()  # make_draft 被替换掉了，这里只要有个东西能传进去

    def _chat_provider(self) -> str:
        return "fake-provider"

    def _resolve_target(self, target):
        return plugin_main.GithubTriagePlugin._resolve_target(self, target)

    async def _kind(self, repo, number):
        return "issue", {"number": number, "title": "t"}

    async def _build_digest(self, repo, number, kind, issue=None):
        return {"files": []}, "digest"


def test_missing_skill_is_reported_in_plain_language(monkeypatch):
    async def boom(*args, **kwargs):
        raise plugin_main.skills.SkillMissing("内置 Skill 缺失：gh-triage")

    monkeypatch.setattr(plugin_main.analyze, "make_draft", boom)

    event = FakeEvent()
    plugin = SkillPlugin()
    out = _drain_sync(
        plugin_main.GithubTriagePlugin.gh_t(plugin, event, target="o/r#1")
    )

    assert len(out) == 2, "先回「抓取 …」，再回缺失提示"
    assert "内置 Skill 缺失" in out[-1][1]
    assert "模型调用失败" not in out[-1][1]


def test_workspace_failure_message_names_the_path_and_next_steps():
    """还原失败时不能只甩一句异常：要带上是哪个路径、往哪查。"""
    text = plugin_main.GithubTriagePlugin._workspace_help(
        r"D:\code\AstrBot\src",
        plugin_main.WorkspaceError(r"不是 git 仓库：D:\code\AstrBot\src"),
    )

    assert "退回静态审查" in text
    assert r"D:\code\AstrBot\src" in text
    assert "不是 git 仓库" in text, "原始错误要保留"
    assert "根目录" in text and "装了 git" in text and "远端名" in text
