"""Tests for `research_agent.tools.archive` (issue #15)."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from research_agent.tools import archive


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        url: str = "",
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url


def _patch_client(monkeypatch, on_get):
    """Replace ``httpx.AsyncClient`` with a fake whose ``get`` is ``on_get``.

    ``on_get`` receives ``(url, headers)`` and returns a :class:`_FakeResponse`.
    Headers are captured from the ``AsyncClient(headers=...)`` init kwargs
    because that's where archive.py sets them.
    """
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        captured["init_kwargs"] = kwargs
        init_headers = kwargs.get("headers", {})

        class _Client:
            async def get(self, url, *args, **kwargs):
                captured["last_url"] = url
                merged = dict(init_headers)
                merged.update(kwargs.get("headers", {}))
                return on_get(url, merged)

        yield _Client()

    monkeypatch.setattr(archive.httpx, "AsyncClient", _client_factory)
    return captured


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    monkeypatch.delenv("RESEARCH_USER_AGENT", raising=False)
    yield


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_save_returns_archive_url_from_content_location(monkeypatch):
    captured = _patch_client(
        monkeypatch,
        lambda url, _h: _FakeResponse(
            headers={"Content-Location": "/web/20260502/https://x.example/y"},
        ),
    )

    result = await archive.save("https://x.example/y")
    assert result == "https://web.archive.org/web/20260502/https://x.example/y"
    assert captured["last_url"] == "https://web.archive.org/save/https://x.example/y"


async def test_save_returns_archive_url_from_location_header(monkeypatch):
    _patch_client(
        monkeypatch,
        lambda url, _h: _FakeResponse(
            headers={"Location": "/web/20260502/https://x.example/y"},
        ),
    )
    assert (
        await archive.save("https://x.example/y")
        == "https://web.archive.org/web/20260502/https://x.example/y"
    )


async def test_save_returns_absolute_archive_url_passthrough(monkeypatch):
    _patch_client(
        monkeypatch,
        lambda url, _h: _FakeResponse(
            headers={"Content-Location": "https://web.archive.org/web/2026/x.example"},
        ),
    )
    assert await archive.save("https://x.example/") == "https://web.archive.org/web/2026/x.example"


async def test_save_uses_response_url_when_no_headers(monkeypatch):
    final = "https://web.archive.org/web/20260502/https://x.example/y"
    _patch_client(
        monkeypatch,
        lambda url, _h: _FakeResponse(headers={}, url=final),
    )
    assert await archive.save("https://x.example/y") == final


async def test_save_sends_user_agent_header(monkeypatch):
    monkeypatch.setenv("RESEARCH_USER_AGENT", "custom-bot/3.0")
    seen_headers: dict[str, str] = {}

    def _on_get(url, headers):
        seen_headers.update(headers)
        return _FakeResponse(headers={"Content-Location": "/web/x/https://x.example"})

    _patch_client(monkeypatch, _on_get)
    await archive.save("https://x.example")
    assert seen_headers.get("User-Agent") == "custom-bot/3.0"


async def test_save_uses_default_user_agent_when_unset(monkeypatch):
    monkeypatch.setattr(archive.config, "get", lambda name: None)
    seen_headers: dict[str, str] = {}

    def _on_get(url, headers):
        seen_headers.update(headers)
        return _FakeResponse(headers={"Content-Location": "/web/x/https://x.example"})

    _patch_client(monkeypatch, _on_get)
    await archive.save("https://x.example")
    assert seen_headers["User-Agent"] == "research-agent/0.1"


# ---------------------------------------------------------------------------
# Failure modes — every one of these must return None, never raise.
# ---------------------------------------------------------------------------


async def test_save_returns_none_on_4xx(monkeypatch):
    _patch_client(monkeypatch, lambda url, _h: _FakeResponse(status_code=429))
    assert await archive.save("https://x.example/y") is None


async def test_save_returns_none_on_5xx(monkeypatch):
    _patch_client(monkeypatch, lambda url, _h: _FakeResponse(status_code=500))
    assert await archive.save("https://x.example/y") is None


async def test_save_returns_none_on_timeout(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, url, *args, **kwargs):
                raise archive.httpx.TimeoutException("slow")

        yield _Client()

    monkeypatch.setattr(archive.httpx, "AsyncClient", _client_factory)
    assert await archive.save("https://x.example/y") is None


async def test_save_returns_none_on_connection_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, url, *args, **kwargs):
                raise archive.httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(archive.httpx, "AsyncClient", _client_factory)
    assert await archive.save("https://x.example/y") is None


async def test_save_returns_none_for_empty_url(monkeypatch):
    # No client should be invoked for an empty URL.
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        raise AssertionError("client should not be created for empty URL")
        yield  # pragma: no cover

    monkeypatch.setattr(archive.httpx, "AsyncClient", _client_factory)
    assert await archive.save("") is None


async def test_save_returns_none_when_no_archive_pointer(monkeypatch):
    """200 OK with no Location/Content-Location and a non-archive URL."""
    _patch_client(
        monkeypatch,
        lambda url, _h: _FakeResponse(
            headers={},
            url="https://web.archive.org/save/https://x.example/y",
        ),
    )
    assert await archive.save("https://x.example/y") is None
