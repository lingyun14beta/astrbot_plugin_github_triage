"""客户端用例：session 复用、退避重试、限流提示。不需要联网。"""

from __future__ import annotations

import asyncio

import pytest

from astrbot_plugin_github_triage.gh.client import (
    MAX_RETRY_DELAY,
    RETRY_STATUS,
    GitHubClient,
    GitHubError,
)


class FakeResp:
    def __init__(
        self, status: int, text: str = "{}", headers: dict | None = None
    ) -> None:
        self.status = status
        self._text = text
        self.headers = headers or {}

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> FakeResp:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeSession:
    """按顺序吐响应；调用次数会被记下来。"""

    def __init__(self, responses: list[FakeResp]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def request(self, *args, **kwargs) -> FakeResp:
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp


def _stub_session(client: GitHubClient, session: FakeSession) -> None:
    async def _ensure() -> FakeSession:
        return session

    client._ensure_session = _ensure  # type: ignore[method-assign]


def test_session_is_reused_then_closed():
    async def run() -> None:
        client = GitHubClient("")
        first = await client._ensure_session()
        second = await client._ensure_session()
        assert first is second
        await client.close()
        assert client._session is None

    asyncio.run(run())


def test_headers_carry_token_only_when_configured():
    assert (
        GitHubClient("tok")._headers("application/json")["Authorization"]
        == "Bearer tok"
    )
    assert "Authorization" not in GitHubClient("")._headers("application/json")


def test_retries_once_on_gateway_error():
    async def run() -> None:
        client = GitHubClient("")
        session = FakeSession([FakeResp(503, "boom"), FakeResp(200, '{"ok": true}')])
        _stub_session(client, session)

        status, data = await client._request("GET", "/repos/o/r")

        assert (status, data) == (200, {"ok": True})
        assert session.calls == 2, "网关错误应退避重试一次"

    asyncio.run(run())


def test_retry_after_header_is_honoured_without_waiting():
    async def run() -> None:
        client = GitHubClient("")
        session = FakeSession(
            [
                FakeResp(429, "slow down", {"Retry-After": "0"}),
                FakeResp(200, '{"ok": 1}'),
            ]
        )
        _stub_session(client, session)

        status, _ = await client._request("GET", "/x")

        assert status == 200
        assert session.calls == 2

    asyncio.run(run())


def test_retries_at_most_once_then_raises():
    async def run() -> None:
        client = GitHubClient("")
        session = FakeSession([FakeResp(503, "boom"), FakeResp(503, "boom")])
        _stub_session(client, session)

        with pytest.raises(GitHubError):
            await client._request("GET", "/x")
        assert session.calls == 2, "只退避一次，不做无限重试"

    asyncio.run(run())


def test_rate_limit_error_carries_hint():
    async def run() -> None:
        client = GitHubClient("")
        session = FakeSession([FakeResp(403, "API rate limit exceeded")])
        _stub_session(client, session)

        with pytest.raises(GitHubError) as info:
            await client._request("GET", "/x")

        assert "限流" in info.value.hint

    asyncio.run(run())


def test_remaining_quota_is_captured():
    async def run() -> None:
        client = GitHubClient("")
        session = FakeSession([FakeResp(200, "{}", {"X-RateLimit-Remaining": "4999"})])
        _stub_session(client, session)

        await client._request("GET", "/x")

        assert client.last_rate_remaining == "4999"

    asyncio.run(run())


def test_retry_constants_are_sane():
    assert {429, 500, 502, 503, 504} == set(RETRY_STATUS)
    assert MAX_RETRY_DELAY == 10.0
