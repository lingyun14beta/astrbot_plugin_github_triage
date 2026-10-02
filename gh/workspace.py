"""为某个 PR 建一个独立 worktree，工具只在这个目录里活动。

设计取舍：

- 用 ``git worktree`` 而不是 clone，省磁盘、快，且共享同一个对象库；
- 把 PR head 取到 **本 PR 专属的私有 ref**（``refs/gh-triage/pr-N``），再检出成 detached HEAD；
  不用 ``FETCH_HEAD`` —— 那是仓库级共享状态，并发下会被另一次 fetch 插队，
  结果可能检出成另一条 PR 的代码且不报错；
- 不新建分支、不改用户当前分支；
- worktree 落在用户仓库**外面**（同级 ``.gh-triage-worktrees/``），不污染工作区；
- 退出时 ``worktree remove --force`` + ``prune``，用完不留痕。

所有写入都发生在临时目录；用户仓库只被读取 git 元数据。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

WORKTREE_DIR_NAME = ".gh-triage-worktrees"
# 本地 clone 是自己的 fork 时，origin 上通常没有 pull/N/head，退回 upstream 再试
FALLBACK_REMOTES = ("origin", "upstream")


class WorkspaceError(Exception):
    """worktree 准备或清理失败。"""


async def _git(*args: str, cwd: str | Path, timeout: float = 180.0) -> tuple[int, str]:
    """跑一条 git 命令，返回 ``(returncode, 输出)``。"""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"git 超时（{timeout:.0f}s）"
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace")


class PullWorkspace:
    """上下文管理器：进入时检出 PR head，退出时删掉 worktree。"""

    def __init__(
        self,
        repo_path: str | Path,
        repo: str,
        number: int,
        *,
        remote: str = "origin",
    ) -> None:
        self.repo_path = Path(str(repo_path)).expanduser()
        self.repo = repo
        self.number = int(number)
        self.remote = remote
        self.root: Path | None = None
        self.resolved_remote: str = ""

    @property
    def ref_name(self) -> str:
        """本 PR 专属的私有 ref：按 PR 号隔离，并发下互不影响。"""
        return f"refs/gh-triage/pr-{self.number}"

    async def _fetch_pr_head(self) -> tuple[int, str]:
        """按 remote 候选依次取 PR head；全失败返回最后一次输出。"""
        candidates = [self.remote] + [
            name for name in FALLBACK_REMOTES if name != self.remote
        ]
        last: tuple[int, str] = (1, "没有可用的 remote")
        for name in candidates:
            code, out = await _git(
                "fetch",
                name,
                f"+refs/pull/{self.number}/head:{self.ref_name}",
                "--no-tags",
                cwd=self.repo_path,
            )
            if code == 0:
                self.resolved_remote = name
                return 0, out
            last = (code, out)
        return last

    async def __aenter__(self) -> PullWorkspace:
        if not (self.repo_path / ".git").exists():
            raise WorkspaceError(f"不是 git 仓库：{self.repo_path}")

        base = self.repo_path.parent / WORKTREE_DIR_NAME
        base.mkdir(exist_ok=True)
        target = base / f"{self.repo.replace('/', '__')}-{self.number}"

        if target.exists():
            await _git("worktree", "remove", "--force", str(target), cwd=self.repo_path)
            shutil.rmtree(target, ignore_errors=True)

        code, out = await self._fetch_pr_head()
        if code != 0:
            raise WorkspaceError(f"git fetch 失败：{out.strip()[:200]}")

        code, out = await _git(
            "worktree",
            "add",
            "--detach",
            str(target),
            self.ref_name,
            cwd=self.repo_path,
        )
        if code != 0:
            raise WorkspaceError(f"git worktree add 失败：{out.strip()[:200]}")

        self.root = target
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self.root is None:
            return
        await _git("worktree", "remove", "--force", str(self.root), cwd=self.repo_path)
        shutil.rmtree(self.root, ignore_errors=True)
        await _git("worktree", "prune", cwd=self.repo_path)
        # 删掉私有 ref；失败也无害（只留一个没人用的 ref）
        await _git("update-ref", "-d", self.ref_name, cwd=self.repo_path)
        self.root = None
