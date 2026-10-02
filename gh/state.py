"""插件状态：已处理条目、待发草稿、上次轮询时间（都走插件 KV 存储）。"""

from __future__ import annotations

import time
from typing import Any

SEEN_KEY = "seen_items"
DRAFTS_KEY = "drafts"
LAST_POLL_KEY = "last_poll_at"
SEEN_LIMIT = 500
PUBLISHED_KEY = "published"
PUBLISHED_LIMIT = 200


def item_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


class StateStore:
    """薄封装插件 KV，避免各处直接拼 key。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    async def _get(self, key: str, default: Any) -> Any:
        value = await self.plugin.get_kv_data(key, default)
        return default if value is None else value

    # ---------- 已处理 ----------

    async def seen(self) -> dict[str, float]:
        data = await self._get(SEEN_KEY, {})
        return data if isinstance(data, dict) else {}

    async def is_seen(self, repo: str, number: int) -> bool:
        return item_key(repo, number) in await self.seen()

    async def mark_seen(self, repo: str, number: int) -> None:
        """标记已处理；只保留最近 ``SEEN_LIMIT`` 条，避免无限增长。"""
        data = await self.seen()
        data[item_key(repo, number)] = time.time()
        if len(data) > SEEN_LIMIT:
            keep = sorted(data.items(), key=lambda kv: kv[1], reverse=True)[:SEEN_LIMIT]
            data = dict(keep)
        await self.plugin.put_kv_data(SEEN_KEY, data)

    # ---------- 草稿 ----------

    async def drafts(self) -> dict[str, dict[str, Any]]:
        data = await self._get(DRAFTS_KEY, {})
        return data if isinstance(data, dict) else {}

    async def save_draft(
        self, repo: str, number: int, *, kind: str, title: str, body: str
    ) -> None:
        data = await self.drafts()
        data[item_key(repo, number)] = {
            "repo": repo,
            "number": number,
            "kind": kind,
            "title": title,
            "body": body,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        await self.plugin.put_kv_data(DRAFTS_KEY, data)

    async def get_draft(self, repo: str, number: int) -> dict[str, Any] | None:
        return (await self.drafts()).get(item_key(repo, number))

    async def pop_draft(self, repo: str, number: int) -> dict[str, Any] | None:
        data = await self.drafts()
        draft = data.pop(item_key(repo, number), None)
        await self.plugin.put_kv_data(DRAFTS_KEY, data)
        return draft if isinstance(draft, dict) else None

    async def list_drafts(self) -> list[dict[str, Any]]:
        return list((await self.drafts()).values())

    # ---------- 已发布 ----------

    async def published(self) -> dict[str, dict[str, Any]]:
        data = await self._get(PUBLISHED_KEY, {})
        return data if isinstance(data, dict) else {}

    async def record_published(
        self, repo: str, number: int, *, url: str, kind: str, mode: str
    ) -> None:
        """记一条已发评论：只留最近 PUBLISHED_LIMIT 条。"""
        data = await self.published()
        data[item_key(repo, number)] = {
            "repo": repo,
            "number": number,
            "kind": kind,
            "mode": mode,
            "url": url,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if len(data) > PUBLISHED_LIMIT:
            keep = sorted(
                data.items(), key=lambda kv: str(kv[1].get("at") or ""), reverse=True
            )[:PUBLISHED_LIMIT]
            data = dict(keep)
        await self.plugin.put_kv_data(PUBLISHED_KEY, data)

    # ---------- 轮询水位 ----------

    async def last_poll_at(self) -> float:
        value = await self._get(LAST_POLL_KEY, 0.0)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    async def set_last_poll_at(self, timestamp: float) -> None:
        await self.plugin.put_kv_data(LAST_POLL_KEY, float(timestamp))
