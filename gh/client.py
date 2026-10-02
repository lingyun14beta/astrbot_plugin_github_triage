"""GitHub REST 客户端。

只用 ``aiohttp``（AstrBot 本体已依赖），不新增任何依赖，也不依赖 ``gh`` CLI。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlencode

import aiohttp

API_ROOT = "https://api.github.com"
USER_AGENT = "astrbot-plugin-github-triage"

JSON_ACCEPT = "application/vnd.github+json"
DIFF_ACCEPT = "application/vnd.github.v3.diff"
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})  # 碰到这些状态码退避一次再试
MAX_RETRY_DELAY = 10.0


class GitHubError(Exception):
    """GitHub API 调用失败（含限流与鉴权错误）。"""

    def __init__(self, message: str, status: int = 0, hint: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.hint = hint


class GitHubClient:
    """最小的 GitHub REST 封装：拿 JSON、拿 diff、发评论、发 review。"""

    def __init__(self, token: str | None = None, timeout: float = 30.0) -> None:
        self.token = (token or "").strip()
        self.timeout = float(timeout)
        self.last_rate_remaining: str | None = None
        self._session: aiohttp.ClientSession | None = None

    # ---------- 内部 ----------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """复用同一个 session：轮询多个仓库时不必反复建连。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def close(self) -> None:
        """插件卸载时调用，释放连接池。"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        accept: str = JSON_ACCEPT,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        url = path if path.startswith("http") else API_ROOT + path
        if params:
            url = (
                f"{url}?{urlencode({k: v for k, v in params.items() if v is not None})}"
            )

        session = await self._ensure_session()
        retried = False
        while True:
            try:
                async with session.request(
                    method,
                    url,
                    headers=self._headers(accept),
                    json=payload,
                ) as resp:
                    text = await resp.text()
                    self.last_rate_remaining = resp.headers.get("X-RateLimit-Remaining")
                    status = resp.status
                    retry_after = resp.headers.get("Retry-After") or ""
            except aiohttp.ClientError as exc:
                raise GitHubError(f"网络错误：{exc}") from exc

            # 限流 / 网关抖动：退避一次再试（Retry-After 优先，最多等 MAX_RETRY_DELAY）
            if status in RETRY_STATUS and not retried:
                retried = True
                delay = float(retry_after) if str(retry_after).isdigit() else 2.0
                await asyncio.sleep(min(delay, MAX_RETRY_DELAY))
                continue
            break

        if status >= 400:
            raise GitHubError(
                f"GitHub API {status}：{text[:200]}",
                status=status,
                hint=self._error_hint(status, text),
            )
        if accept == DIFF_ACCEPT:
            return status, text
        try:
            return status, json.loads(text) if text else None
        except json.JSONDecodeError:
            return status, text

    @staticmethod
    def _error_hint(status: int, text: str) -> str:
        if status in (401, 403) and "rate limit" in text.lower():
            return "已触发限流：匿名 60 次/小时，配置 github_token 后为 5000 次/小时。"
        if status == 404:
            return "仓库或编号不存在；若为私有仓库，请确认 token 权限。"
        if status == 422:
            return "请求被拒绝，常见原因是 review 参数不合法或该 PR 已关闭。"
        return ""

    async def _get(self, path: str, **kwargs: Any) -> Any:
        _, data = await self._request("GET", path, **kwargs)
        return data

    # ---------- 读 ----------

    async def viewer(self) -> dict[str, Any]:
        """返回 token 对应的账号；未配置 token 时调用会 401。"""
        return await self._get("/user")

    async def list_issues(
        self, repo: str, *, since: str | None = None, per_page: int = 30
    ) -> list[dict[str, Any]]:
        """列出仓库的 open issue 与 PR（GitHub 把 PR 也算 issue）。

        ``since`` 过滤的是 ``updated_at``，所以排序也必须用 ``updated``：
        用 ``sort=created`` 时，刚被更新的老条目排在窗口末尾之外，会被整窗漏掉。

        Args:
            repo: ``owner/repo``。
            since: ISO 8601 时间；只返回该时间之后更新过的条目。
            per_page: 单页条数上限（GitHub 上限 100）。

        Returns:
            条目列表；接口返回非列表时给空列表。
        """
        data = await self._get(
            f"/repos/{repo}/issues",
            params={
                "state": "open",
                "sort": "updated",
                "direction": "desc",
                "since": since,
                "per_page": per_page,
            },
        )
        return data if isinstance(data, list) else []

    async def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        return await self._get(f"/repos/{repo}/issues/{number}")

    async def get_issue_comments(
        self, repo: str, number: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        data = await self._get(
            f"/repos/{repo}/issues/{number}/comments", params={"per_page": limit}
        )
        return data if isinstance(data, list) else []

    async def get_pull(self, repo: str, number: int) -> dict[str, Any]:
        return await self._get(f"/repos/{repo}/pulls/{number}")

    async def get_pull_files(
        self, repo: str, number: int, limit: int = 100
    ) -> list[dict[str, Any]]:
        data = await self._get(
            f"/repos/{repo}/pulls/{number}/files", params={"per_page": limit}
        )
        return data if isinstance(data, list) else []

    async def get_pull_commits(
        self, repo: str, number: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        data = await self._get(
            f"/repos/{repo}/pulls/{number}/commits", params={"per_page": limit}
        )
        return data if isinstance(data, list) else []

    async def get_pull_diff(self, repo: str, number: int) -> str:
        _, text = await self._request(
            "GET", f"/repos/{repo}/pulls/{number}", accept=DIFF_ACCEPT
        )
        return text

    # ---------- 写 ----------

    async def post_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        _, data = await self._request(
            "POST", f"/repos/{repo}/issues/{number}/comments", payload={"body": body}
        )
        return data if isinstance(data, dict) else {}

    async def create_review(
        self,
        repo: str,
        number: int,
        body: str,
        *,
        event: str = "COMMENT",
    ) -> dict[str, Any]:
        """提交 PR review。``event`` 取 ``COMMENT`` / ``APPROVE`` / ``REQUEST_CHANGES``。"""
        _, data = await self._request(
            "POST",
            f"/repos/{repo}/pulls/{number}/reviews",
            payload={"body": body, "event": event},
        )
        return data if isinstance(data, dict) else {}
