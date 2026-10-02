"""轮询用例：抓失败或窗口取满时不推进水位（免得新条目下一轮凭空消失）。"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

os.chdir(tempfile.mkdtemp(prefix="gh-triage-test-"))
pytest.importorskip("astrbot", reason="指令装饰器来自 AstrBot core，缺运行时时跳过")

from astrbot_plugin_github_triage.main import POLL_PAGE_SIZE, GithubTriagePlugin  # noqa: E402
from astrbot_plugin_github_triage.gh.client import GitHubError  # noqa: E402


class FakeState:
    def __init__(self) -> None:
        self.watermark = 0.0
        self.seen: set[tuple[str, int]] = set()

    async def last_poll_at(self) -> float:
        return self.watermark

    async def set_last_poll_at(self, timestamp: float) -> None:
        self.watermark = timestamp

    async def is_seen(self, repo: str, number: int) -> bool:
        return (repo, number) in self.seen

    async def mark_seen(self, repo: str, number: int) -> None:
        self.seen.add((repo, number))


class FakeClient:
    def __init__(self, *, fail: bool, total: int = 1) -> None:
        self.fail = fail
        self.total = total
        self.calls = 0
        self.per_page = 0

    async def list_issues(
        self, repo: str, *, since: str | None = None, per_page: int = 30
    ):
        self.calls += 1
        self.per_page = per_page
        if self.fail:
            raise GitHubError("boom", status=403, hint="已触发限流")
        return [
            {"number": number, "title": f"条目 {number}", "user": {"login": "someone"}}
            for number in range(self.total, 0, -1)
        ][:per_page]


class FakePlugin:
    """只带 poll_once 需要的字段，避免构造整个插件。"""

    def __init__(self, *, fail: bool, total: int = 1) -> None:
        self.client = FakeClient(fail=fail, total=total)
        self.state = FakeState()
        self.repos = [{"repo": "o/r", "issues": True, "prs": True}]
        self.config = {"max_items_per_poll": 5}
        self._polling = False
        self.notified: list[list[dict]] = []

    async def _notify_new_items(self, items) -> None:
        self.notified.append(list(items))


def _poll(fake: FakePlugin):
    return asyncio.run(GithubTriagePlugin.poll_once(fake, reason="test"))


def test_failed_repo_does_not_advance_watermark():
    fake = FakePlugin(fail=True)

    found = _poll(fake)

    assert found == []
    assert fake.state.watermark == 0.0, "抓失败时水位不能推进，否则新条目下轮不再出现"
    assert fake.notified == []


def test_success_advances_watermark_and_notifies():
    fake = FakePlugin(fail=False)

    found = _poll(fake)

    assert [item["number"] for item in found] == [1]
    assert fake.state.watermark > 0
    assert fake.notified and fake.notified[0][0]["repo"] == "o/r"
    assert ("o/r", 1) in fake.state.seen


def test_seen_items_are_not_reported_twice():
    fake = FakePlugin(fail=False)
    fake.state.seen.add(("o/r", 1))

    assert _poll(fake) == []


def test_full_window_does_not_advance_watermark():
    """窗口取满说明还有条目排在水位之后，推水位会把它们永久丢掉。"""
    fake = FakePlugin(fail=False, total=POLL_PAGE_SIZE + 10)

    found = _poll(fake)

    assert len(found) == 5, "本轮仍按 max_items_per_poll 截断"
    assert fake.client.per_page == POLL_PAGE_SIZE + 1, "多要一条用来判断窗口是否取满"
    assert fake.state.watermark == 0.0, "窗口取满时不能推进水位"


def test_partial_window_advances_watermark():
    fake = FakePlugin(fail=False, total=3)

    _poll(fake)

    assert fake.state.watermark > 0


def test_items_beyond_the_page_survive_the_next_poll():
    """回归用例：窗口取满时水位不动，水位之后的条目才有机会被下一轮取到。"""
    fake = FakePlugin(fail=False, total=POLL_PAGE_SIZE + 10)

    _poll(fake)  # 第一轮：窗口取满，水位不动
    _poll(fake)

    assert fake.state.watermark == 0.0, "窗口一直取满，水位就不该动"
    marked = {number for repo, number in fake.state.seen if repo == "o/r"}
    assert marked != {60, 59, 58, 57, 56}, "第二轮应继续消化同一窗口里还没处理的条目"


def test_watermark_advances_once_window_is_no_longer_full():
    fake = FakePlugin(fail=False, total=POLL_PAGE_SIZE + 10)
    _poll(fake)
    assert fake.state.watermark == 0.0

    fake.client.total = 3  # 新条目把老条目挤出窗口，窗口不再取满

    _poll(fake)

    assert fake.state.watermark > 0
