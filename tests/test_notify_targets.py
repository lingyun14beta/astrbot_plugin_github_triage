"""通知会话用例：UMO 解析、生效列表的来源与渲染。

`/gh watch` 与配置项 `notify_targets` 都会给出待推送的会话，这里盯住三件事：
写法不认识的 UMO 能被识别出来、命令写过的列表优先于配置、列表为空时才回落到配置。
需要 AstrBot 运行时（指令装饰器来自 core），缺运行时时整组跳过。
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

# AstrBot 会按当前工作目录解析 data/ 等运行时路径；先切走，免得污染插件目录
os.chdir(tempfile.mkdtemp(prefix="gh-triage-test-"))
pytest.importorskip("astrbot", reason="指令装饰器来自 AstrBot core，缺运行时时跳过")

from astrbot_plugin_github_triage import main as plugin_main  # noqa: E402

Plugin = plugin_main.GithubTriagePlugin
NOTIFY_KEY = plugin_main.NOTIFY_KEY


class TargetPlugin:
    """只带 _notify_targets 需要的字段：KV 宿主 + 配置。"""

    def __init__(self, stored: object = None, notify_targets=None) -> None:
        self.store: dict[str, object] = {}
        if stored is not None:
            self.store[NOTIFY_KEY] = stored
        self.config = {"notify_targets": notify_targets or []}

    async def get_kv_data(self, key, default=None):
        return self.store.get(key, default)

    async def put_kv_data(self, key, value) -> None:
        self.store[key] = value


def _targets(plugin):
    return asyncio.run(Plugin._notify_targets(plugin))


# ---------- UMO 解析 ----------


def test_parse_umo_accepts_all_message_types():
    assert Plugin._parse_umo("aiocqhttp:GroupMessage:123456") == (
        "aiocqhttp",
        "GroupMessage",
        "123456",
    )
    assert Plugin._parse_umo("telegram:FriendMessage:42") == (
        "telegram",
        "FriendMessage",
        "42",
    )
    assert Plugin._parse_umo("webchat:OtherMessage:chat-1") == (
        "webchat",
        "OtherMessage",
        "chat-1",
    )


def test_parse_umo_keeps_colons_inside_session_id():
    """session_id 里带冒号时要原样保留（split 只切前两段）。"""
    assert Plugin._parse_umo("aiocqhttp:GroupMessage:g:1") == (
        "aiocqhttp",
        "GroupMessage",
        "g:1",
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "123456",  # 只有 session_id
        "aiocqhttp:123456",  # 少一段
        "aiocqhttp:group:123456",  # MessageType 写法不对
        "aiocqhttp:GroupMessage:",  # session_id 空
        ":GroupMessage:123456",  # platform_id 空
    ],
)
def test_parse_umo_rejects_malformed(raw):
    assert Plugin._parse_umo(raw) is None


# ---------- 生效列表与来源 ----------


def test_config_targets_are_used_when_command_list_absent():
    plugin = TargetPlugin(notify_targets=["aiocqhttp:GroupMessage:1"])
    assert _targets(plugin) == (["aiocqhttp:GroupMessage:1"], "配置")


def test_command_list_takes_precedence_over_config():
    plugin = TargetPlugin(
        stored=["aiocqhttp:GroupMessage:2"],
        notify_targets=["aiocqhttp:GroupMessage:1"],
    )
    assert _targets(plugin) == (["aiocqhttp:GroupMessage:2"], "命令")


def test_empty_command_list_does_not_fall_back_to_config():
    """`/gh watch off` 掉最后一个会话后，不应该又冒出配置里那条。"""
    plugin = TargetPlugin(stored=[], notify_targets=["aiocqhttp:GroupMessage:1"])
    assert _targets(plugin) == ([], "命令")


def test_config_entries_are_trimmed_and_blanks_dropped():
    plugin = TargetPlugin(
        notify_targets=["  aiocqhttp:GroupMessage:1  ", "", "   ", 42]
    )
    assert _targets(plugin) == (["aiocqhttp:GroupMessage:1", "42"], "配置")


def test_non_list_config_is_ignored():
    plugin = TargetPlugin()
    plugin.config = {"notify_targets": "aiocqhttp:GroupMessage:1"}
    assert _targets(plugin) == ([], "无")


# ---------- 列表渲染 ----------


def test_format_targets_labels_kind_and_marks_current_session():
    text = Plugin._format_targets(
        ["aiocqhttp:GroupMessage:123456", "telegram:FriendMessage:42"],
        current="telegram:FriendMessage:42",
    )
    lines = text.splitlines()
    assert lines[0] == "- aiocqhttp｜群聊｜123456"
    assert lines[1] == "- telegram｜私聊｜42 ← 本会话"


def test_format_targets_flags_unparsable_entry():
    text = Plugin._format_targets(["aiocqhttp:group:1"])
    assert "写法不认识" in text
