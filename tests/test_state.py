"""状态层用例：已处理表（含容量回收）、草稿、轮询水位。用一个假 KV 宿主，不需要 AstrBot。"""

from __future__ import annotations

import asyncio

from astrbot_plugin_github_triage.gh.state import (
    PUBLISHED_LIMIT,
    SEEN_LIMIT,
    StateStore,
)


class FakeKvHost:
    """最小 KV 宿主：行为对齐 AstrBot 的 get_kv_data / put_kv_data。"""

    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    async def get_kv_data(self, key: str, default=None):
        return self.store.get(key, default)

    async def put_kv_data(self, key: str, value) -> None:
        self.store[key] = value


def test_marks_and_reads_seen_items():
    async def run():
        state = StateStore(FakeKvHost())
        assert await state.is_seen("o/r", 1) is False
        await state.mark_seen("o/r", 1)
        assert await state.is_seen("o/r", 1) is True
        assert await state.is_seen("o/r", 2) is False

    asyncio.run(run())


def test_seen_table_is_capped():
    async def run():
        state = StateStore(FakeKvHost())
        for number in range(SEEN_LIMIT + 20):
            await state.mark_seen("o/r", number)
        seen = await state.seen()
        assert len(seen) == SEEN_LIMIT
        assert await state.is_seen("o/r", SEEN_LIMIT + 19) is True

    asyncio.run(run())


def test_draft_roundtrip():
    async def run():
        state = StateStore(FakeKvHost())
        await state.save_draft("o/r", 5, kind="pr", title="t", body="body")
        draft = await state.get_draft("o/r", 5)
        assert draft and draft["body"] == "body" and draft["kind"] == "pr"
        assert len(await state.list_drafts()) == 1
        popped = await state.pop_draft("o/r", 5)
        assert popped and popped["title"] == "t"
        assert await state.get_draft("o/r", 5) is None

    asyncio.run(run())


def test_poll_watermark_defaults_and_updates():
    async def run():
        state = StateStore(FakeKvHost())
        assert await state.last_poll_at() == 0.0
        await state.set_last_poll_at(1735689600.0)
        assert await state.last_poll_at() == 1735689600.0

    asyncio.run(run())


def test_records_published_comment():
    async def run():
        state = StateStore(FakeKvHost())
        await state.record_published(
            "o/r", 9, url="https://example.invalid/9", kind="pr", mode="评论"
        )
        data = await state.published()
        assert data["o/r#9"]["url"] == "https://example.invalid/9"
        assert data["o/r#9"]["mode"] == "评论"

    asyncio.run(run())


def test_published_table_is_capped():
    async def run():
        state = StateStore(FakeKvHost())
        for number in range(PUBLISHED_LIMIT + 5):
            await state.record_published(
                "o/r", number, url="u", kind="issue", mode="评论"
            )
        assert len(await state.published()) == PUBLISHED_LIMIT

    asyncio.run(run())
