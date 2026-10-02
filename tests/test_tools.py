"""工具层用例：路径边界、读 / 搜 / 列、白名单命令。

工具基类来自 AstrBot core，运行环境里没有 AstrBot 时整组跳过。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import os
import tempfile

import pytest

# AstrBot 会按当前工作目录解析 data/ 等运行时路径；先切到临时目录，
# 免得它往插件目录里拉一堆运行期文件。
os.chdir(tempfile.mkdtemp(prefix="gh-triage-test-"))

pytest.importorskip("astrbot", reason="工具基类来自 AstrBot core，缺运行时时跳过")

from astrbot_plugin_github_triage.gh import tools  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_resolve_inside_rejects_traversal():
    for bad in ["../../etc/passwd", "..", "a/../../../outside.txt"]:
        with pytest.raises(tools.PathOutsideRoot):
            tools.resolve_inside(PLUGIN_ROOT, bad)


def test_resolve_inside_accepts_normal_path():
    assert tools.resolve_inside(PLUGIN_ROOT, "gh/tools.py").is_file()


def test_read_lines_returns_numbered_window():
    text = tools.read_lines(PLUGIN_ROOT, "gh/tools.py", 1, 8)
    assert "gh/tools.py（第 1-8 行" in text
    assert "| " in text


def test_read_lines_clamps_window_size():
    text = tools.read_lines(PLUGIN_ROOT, "gh/tools.py", 1, 99999)
    assert len(text.splitlines()) <= tools.MAX_LINES + 1


def test_read_lines_rejects_outside_path():
    with pytest.raises(tools.PathOutsideRoot):
        tools.read_lines(PLUGIN_ROOT, "../../outside.txt")


def test_search_reports_file_and_line():
    hits = tools.search(PLUGIN_ROOT, r"def resolve_inside", "*.py")
    assert "gh/tools.py:" in hits


def test_search_rejects_bad_regex():
    assert "正则不合法" in tools.search(PLUGIN_ROOT, "([", "*.py")


def test_search_returns_placeholder_when_nothing_matches():
    token = "zzz_" + uuid.uuid4().hex  # 每次都不一样，免得搜到测试文件自身
    assert tools.search(PLUGIN_ROOT, token, "*.py") == "没有命中。"


def test_list_dir_lists_known_files():
    listing = tools.list_dir(PLUGIN_ROOT, "gh")
    assert "tools.py" in listing
    assert "workspace.py" in listing


def test_run_check_rejects_kind_outside_whitelist():
    out = asyncio.run(tools.run_check(PLUGIN_ROOT, "rm", "."))
    assert "不允许的命令" in out


def test_run_check_rejects_target_outside_root():
    out = asyncio.run(tools.run_check(PLUGIN_ROOT, "ruff_check", "../../.."))
    assert "路径越界" in out


def test_tools_declare_json_schema():
    tool = tools.ReadFileTool(root=str(PLUGIN_ROOT))
    assert tool.name == "gh_read_file"
    assert tool.parameters["required"] == ["path"]


def test_run_check_exposes_command_enum():
    tool = tools.RunCheckTool(root=str(PLUGIN_ROOT))
    allowed = tool.parameters["properties"]["kind"]["enum"]
    assert "ruff_check" in allowed
    assert "rm" not in allowed


def test_search_works_when_ancestor_dir_is_named_build(tmp_path):
    """回归用例：根目录上层叫 build / venv 之类时，不能整仓静默跳过。"""
    root = tmp_path / "build" / "venv" / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("def marker(): pass", encoding="utf-8")

    hits = tools.search(root, "def marker", "*.py")

    assert "pkg/a.py:" in hits


def test_search_still_skips_noise_dirs_inside_root(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "node_modules").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("marker = 1", encoding="utf-8")
    (root / "node_modules" / "b.py").write_text("marker = 2", encoding="utf-8")

    hits = tools.search(root, "marker", "*.py")

    assert "pkg/a.py:" in hits
    assert "node_modules" not in hits


def test_build_argv_uses_configured_interpreter():
    argv = tools._build_argv("pytest", ".", "D:/py/python.exe")
    assert argv[0] == "D:/py/python.exe"
    assert "pytest" in argv


def test_build_argv_falls_back_to_current_interpreter():
    argv = tools._build_argv("ruff_check", "pkg")
    assert argv[0] == sys.executable


def test_run_check_rejects_missing_interpreter():
    out = asyncio.run(
        tools.run_check(PLUGIN_ROOT, "pytest", ".", "Z:/no/such/python.exe")
    )
    assert "解释器不存在" in out


def test_run_check_tool_carries_interpreter():
    tool = tools.RunCheckTool(root=str(PLUGIN_ROOT), python="X:/py.exe")
    assert tool.python == "X:/py.exe"
