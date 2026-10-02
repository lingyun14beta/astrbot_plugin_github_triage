"""配置读取用例：仓库名归一化之后，repos 与 local_paths 才能对得上。

GitHub 的 owner/repo 大小写不敏感，配置里写成 `AstrBotDevs/AstrBot` 或
`astrbotdevs/astrbot` 都是同一个仓库；两边用同一套归一化，查表才不会错位。
需要 AstrBot 运行时（指令装饰器来自 core），缺运行时时整组跳过。
"""

from __future__ import annotations

import os
import tempfile

import pytest

os.chdir(tempfile.mkdtemp(prefix="gh-triage-test-"))
pytest.importorskip("astrbot", reason="指令装饰器来自 AstrBot core，缺运行时时跳过")

from astrbot_plugin_github_triage import main as plugin_main  # noqa: E402

Plugin = plugin_main.GithubTriagePlugin


class ConfigPlugin(Plugin):
    """只带配置读取需要的字段。"""

    def __init__(self, config) -> None:
        self.config = config


def _plugin(repos=None, local_paths=None):
    return ConfigPlugin({"repos": repos or [], "local_paths": local_paths or []})


# ---------- 键归一化 ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AstrBotDevs/AstrBot", "astrbotdevs/astrbot"),
        ("astrbotdevs/astrbot", "astrbotdevs/astrbot"),
        ("  AstrBotDevs/AstrBot/  ", "astrbotdevs/astrbot"),
        ("Owner/Repo", "owner/repo"),
        ("", ""),
        ("no-slash", ""),
        ("a/b/c", ""),
        (None, ""),
    ],
)
def test_repo_key_normalizes_case_and_stray_slashes(raw, expected):
    assert Plugin._repo_key(raw) == expected


# ---------- repos ----------


def test_read_repos_lowercases_and_skips_bad_entries():
    plugin = _plugin(
        repos=[
            {"repo": "AstrBotDevs/AstrBot"},
            {"repo": "  foo/BAR/  ", "watch_issues": False, "watch_prs": False},
            {"repo": "no-slash"},
            {"repo": ""},
            "not-a-dict",
        ]
    )
    assert plugin._read_repos() == [
        {"repo": "astrbotdevs/astrbot", "issues": True, "prs": True},
        {"repo": "foo/bar", "issues": False, "prs": False},
    ]


# ---------- local_paths ----------


def test_local_path_matches_repo_configured_in_another_case():
    """回归用例：两边大小写不同也要对上，否则会静默退回静态审查。"""
    plugin = _plugin(
        repos=[{"repo": "AstrBotDevs/AstrBot"}],
        local_paths=[{"repo": "astrbotdevs/ASTRBOT", "path": r"D:\code\AstrBot"}],
    )

    paths = plugin._read_local_paths()
    repo = plugin._read_repos()[0]["repo"]

    assert paths.get(repo) == r"D:\code\AstrBot"


def test_local_paths_normalize_keys_and_skip_incomplete_entries():
    plugin = _plugin(
        local_paths=[
            {"repo": "AstrBotDevs/AstrBot/", "path": r"D:\code\a"},
            {"repo": "foo/bar", "path": "   "},  # 空路径
            {"repo": "", "path": r"D:\code\b"},  # 没有 repo
            {"repo": "no-slash", "path": r"D:\code\c"},
            {"path": r"D:\code\d"},
            "not-a-dict",
        ]
    )
    assert plugin._read_local_paths() == {"astrbotdevs/astrbot": r"D:\code\a"}


def test_local_remotes_normalize_keys_too():
    plugin = _plugin(
        local_paths=[
            {"repo": "AstrBotDevs/AstrBot", "path": r"D:\code\a", "remote": "upstream"},
            {"repo": "foo/bar", "path": r"D:\code\b", "remote": "   "},
        ]
    )
    assert plugin._read_local_remotes() == {"astrbotdevs/astrbot": "upstream"}
