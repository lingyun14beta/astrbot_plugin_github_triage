"""worktree 生命周期用例：不联网，用临时仓库自造 GitHub 那样的 ``refs/pull/N/head``。"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from astrbot_plugin_github_triage.gh.workspace import PullWorkspace, WorkspaceError

IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")
PR_REF_TEMPLATE = "refs/gh-triage/pr-{number}"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *IDENTITY, *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def _make_upstream(tmp_path: Path) -> tuple[Path, Path]:
    """造一个假上游：``main`` + ``refs/pull/1/head`` + ``refs/pull/2/head``（与 GitHub 布局一致）。

    返回 ``(本地克隆, 裸仓库)``。
    """
    bare = tmp_path / "upstream.git"
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    (work / "a.txt").write_text("base", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "base")

    _git(work, "checkout", "-b", "pr-one")
    (work / "a.txt").write_text("pr-one", encoding="utf-8")
    (work / "new.txt").write_text("added by pr one", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "feat: pr one")
    one_sha = _git(work, "rev-parse", "HEAD").strip()

    _git(work, "checkout", "-b", "pr-two", "main")
    (work / "a.txt").write_text("pr-two", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "feat: pr two")
    two_sha = _git(work, "rev-parse", "HEAD").strip()

    _git(tmp_path, "init", "--bare", str(bare))
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(work, "remote", "add", "origin", str(bare))
    # 先把对象推上去（否则下一步写入的 ref 指向不存在的提交）
    _git(work, "push", "origin", "main", "pr-one", "pr-two")
    # GitHub 把 PR head 放在 refs/pull/<n>/head —— 照抄这个布局，并删掉临时分支，
    # 确保被测代码真的从 refs/pull/N/head 取，而不是从同名分支蒙对
    _git(bare, "update-ref", "refs/pull/1/head", one_sha)
    _git(bare, "update-ref", "refs/pull/2/head", two_sha)
    _git(bare, "update-ref", "-d", "refs/heads/pr-one")
    _git(bare, "update-ref", "-d", "refs/heads/pr-two")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    return clone, bare


def test_worktree_checks_out_pr_head_and_cleans_up(tmp_path):
    clone, _ = _make_upstream(tmp_path)

    async def run() -> Path:
        async with PullWorkspace(clone, "fake/repo", 1) as ws:
            assert ws.root is not None and ws.root.is_dir()
            assert (ws.root / "a.txt").read_text(encoding="utf-8") == "pr-one"
            assert (ws.root / "new.txt").is_file()
            return ws.root

    worktree_root = asyncio.run(run())
    assert not worktree_root.exists(), "退出后应删除 worktree"
    assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_checks_out_via_pr_scoped_ref(tmp_path):
    """回归用例：检出走本 PR 专属 ref，不依赖仓库级共享的 FETCH_HEAD。"""
    clone, bare = _make_upstream(tmp_path)
    expected = _git(bare, "rev-parse", "refs/pull/1/head").strip()

    async def run() -> None:
        async with PullWorkspace(clone, "fake/repo", 1) as ws:
            local_ref = PR_REF_TEMPLATE.format(number=1)
            assert _git(clone, "rev-parse", local_ref).strip() == expected
            assert _git(ws.root, "rev-parse", "HEAD").strip() == expected

    asyncio.run(run())


def test_two_prs_stay_isolated(tmp_path):
    """两个 PR 的 worktree 同时存在时，内容不能互相串。"""
    clone, _ = _make_upstream(tmp_path)

    async def run() -> tuple[Path, Path]:
        async with PullWorkspace(clone, "fake/repo", 1) as one:
            async with PullWorkspace(clone, "fake/repo", 2) as two:
                assert one.root != two.root
                assert (one.root / "a.txt").read_text(encoding="utf-8") == "pr-one"
                assert (two.root / "a.txt").read_text(encoding="utf-8") == "pr-two"
                refs = _git(
                    clone, "for-each-ref", "--format=%(refname)", "refs/gh-triage/"
                )
                assert PR_REF_TEMPLATE.format(number=1) in refs
                assert PR_REF_TEMPLATE.format(number=2) in refs
                return one.root, two.root

    first, second = asyncio.run(run())
    assert not first.exists()
    assert not second.exists()
    refs_after = _git(clone, "for-each-ref", "--format=%(refname)", "refs/gh-triage/")
    assert refs_after.strip() == "", f"退出后应清掉私有 ref，实际还剩：{refs_after!r}"


def test_worktree_is_removed_even_when_body_raises(tmp_path):
    clone, _ = _make_upstream(tmp_path)
    captured: dict[str, Path] = {}

    async def run() -> None:
        with pytest.raises(RuntimeError):
            async with PullWorkspace(clone, "fake/repo", 1) as ws:
                captured["root"] = ws.root
                raise RuntimeError("boom")

    asyncio.run(run())
    assert not captured["root"].exists()


def test_rejects_directory_that_is_not_a_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    async def run() -> None:
        with pytest.raises(WorkspaceError):
            async with PullWorkspace(plain, "fake/repo", 1):
                pass

    asyncio.run(run())


def test_failed_fetch_raises_workspace_error(tmp_path):
    clone, _ = _make_upstream(tmp_path)

    async def run() -> None:
        with pytest.raises(WorkspaceError):
            async with PullWorkspace(clone, "fake/repo", 999):
                pass

    asyncio.run(run())


def test_falls_back_to_upstream_when_origin_lacks_pr_ref(tmp_path):
    """本地 clone 是自己的 fork 时，origin 上没有 pull/N/head，应退回 upstream。"""
    clone, _ = _make_upstream(tmp_path)
    _git(clone, "remote", "rename", "origin", "upstream")

    async def run() -> Path:
        async with PullWorkspace(clone, "fake/repo", 1, remote="origin") as ws:
            assert (ws.root / "a.txt").read_text(encoding="utf-8") == "pr-one"
            assert ws.resolved_remote == "upstream"
            return ws.root

    root = asyncio.run(run())
    assert not root.exists()
