"""通知会话用例：UMO 解析、配置项的清洗与渲染。

通知只走配置项 `notify_targets` 一个入口（`/gh watch` 已移除），所以这里盯住：
写法不对的条目能被挡下来、从 `/sid` 复制来的引号能被剥掉、渲染读得懂。
需要 AstrBot 运行时（指令装饰器来自 core），缺运行时时整组跳过。
"""

from __future__ import annotations

import os
import tempfile

import pytest

# AstrBot 会按当前工作目录解析 data/ 等运行时路径；先切走，免得污染插件目录
os.chdir(tempfile.mkdtemp(prefix="gh-triage-test-"))
pytest.importorskip("astrbot", reason="指令装饰器来自 AstrBot core，缺运行时时跳过")

from astrbot_plugin_github_triage import main as plugin_main  # noqa: E402

Plugin = plugin_main.GithubTriagePlugin


class TargetPlugin(Plugin):
    """只带 _notify_targets 需要的字段：配置。"""

    def __init__(self, notify_targets=None) -> None:
        self.config = {
            "notify_targets": [] if notify_targets is None else notify_targets
        }


def _targets(raw):
    return TargetPlugin(raw)._notify_targets()


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
    """session_id 里带冒号时要原样保留（只切前两段）。"""
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


# ---------- 配置项清洗 ----------


def test_sid_output_wrapper_is_stripped():
    """/sid 输出的是「UMO」，连书名号一起复制也要能用。"""
    assert (
        Plugin._clean_target("「aiocqhttp:GroupMessage:1」")
        == "aiocqhttp:GroupMessage:1"
    )
    assert (
        Plugin._clean_target('"aiocqhttp:GroupMessage:1"') == "aiocqhttp:GroupMessage:1"
    )
    assert (
        Plugin._clean_target("  aiocqhttp:GroupMessage:1  ")
        == "aiocqhttp:GroupMessage:1"
    )


def test_clean_target_leaves_normal_value_alone():
    assert (
        Plugin._clean_target("aiocqhttp:GroupMessage:1") == "aiocqhttp:GroupMessage:1"
    )


def test_targets_are_cleaned_and_malformed_entries_dropped():
    targets = _targets(
        [
            "aiocqhttp:GroupMessage:1",
            "「telegram:FriendMessage:42」",
            "",
            "   ",
            "aiocqhttp:group:oops",
            "123456",
            None,
            "webchat:OtherMessage:chat-1",
        ]
    )
    assert targets == [
        "aiocqhttp:GroupMessage:1",
        "telegram:FriendMessage:42",
        "webchat:OtherMessage:chat-1",
    ]


def test_missing_or_non_list_config_gives_empty_list():
    assert TargetPlugin()._notify_targets() == []
    assert TargetPlugin("aiocqhttp:GroupMessage:1")._notify_targets() == []
    assert TargetPlugin(None)._notify_targets() == []


# ---------- 渲染 ----------


def test_format_targets_labels_kind():
    text = Plugin._format_targets(
        ["aiocqhttp:GroupMessage:123456", "telegram:FriendMessage:42"]
    )
    assert text.splitlines() == [
        "- aiocqhttp｜群聊｜123456",
        "- telegram｜私聊｜42",
    ]
