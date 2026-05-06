"""Tests for `research_agent.tools.reddit` (JSON endpoint version)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from research_agent.tools import reddit


# ---------------------------------------------------------------------------
# httpx mocking helpers
# ---------------------------------------------------------------------------


class _StubResponse:
    """Minimal httpx.Response stand-in for status + json decoding."""

    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        # ``text`` is read on non-200 paths for the WARN log.
        self.text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _StubAsyncClient:
    """Drop-in for httpx.AsyncClient used by reddit.search/fetch.

    Captures the URL + headers each call sees and returns the configured
    ``response`` (or raises ``raise_with`` to simulate transport errors).
    """

    last_url: str | None = None
    last_headers: dict[str, str] | None = None

    def __init__(
        self,
        *,
        response: _StubResponse | None = None,
        raise_with: Exception | None = None,
    ) -> None:
        self._response = response
        self._raise = raise_with

    async def __aenter__(self) -> "_StubAsyncClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def get(self, url: str, headers: dict[str, str] | None = None, **_: Any) -> _StubResponse:
        type(self).last_url = url
        type(self).last_headers = dict(headers or {})
        if self._raise is not None:
            raise self._raise
        assert self._response is not None
        return self._response


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> type[_StubAsyncClient]:
    """Install a fresh stub class on httpx.AsyncClient. Returns the class."""
    stub_cls = type("_Stub", (_StubAsyncClient,), {})
    stub_cls.last_url = None
    stub_cls.last_headers = None
    instance = stub_cls(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: instance)
    return stub_cls


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


def _fake_listing(*posts: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"children": [{"data": p} for p in posts]}}


@pytest.mark.asyncio
async def test_search_empty_query_returns_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _patch_httpx(monkeypatch, response=_StubResponse(200, _fake_listing()))
    results = await reddit.search("   ")
    assert results == []
    assert stub.last_url is None  # never made the HTTP call


@pytest.mark.asyncio
async def test_search_global_url(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _patch_httpx(
        monkeypatch,
        response=_StubResponse(
            200,
            _fake_listing(
                {
                    "title": "Cursor pricing complaints thread",
                    "permalink": "/r/cursor/comments/abc/cursor_pricing/",
                    "selftext": "I am unhappy with the new model.",
                    "subreddit": "cursor",
                    "score": 42,
                    "num_comments": 8,
                    "created_utc": 1700000000,
                }
            ),
        ),
    )
    results = await reddit.search("cursor pricing", limit=5)
    assert stub.last_url is not None
    assert "search.json" in stub.last_url
    assert "limit=5" in stub.last_url
    assert "User-Agent" in stub.last_headers
    assert len(results) == 1
    r = results[0]
    assert r.url == "https://www.reddit.com/r/cursor/comments/abc/cursor_pricing/"
    assert r.title == "Cursor pricing complaints thread"
    assert r.score == 42.0
    assert r.extras["subreddit"] == "cursor"
    assert r.extras["num_comments"] == 8


@pytest.mark.asyncio
async def test_search_subreddit_url_uses_restrict_sr(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _patch_httpx(monkeypatch, response=_StubResponse(200, _fake_listing()))
    await reddit.search("billing", subreddit="cursor")
    assert "/r/cursor/search.json" in stub.last_url
    assert "restrict_sr=on" in stub.last_url


@pytest.mark.asyncio
async def test_search_clamps_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _patch_httpx(monkeypatch, response=_StubResponse(200, _fake_listing()))
    await reddit.search("anything", limit=999)
    # Reddit caps at 100; our build_search_url clamps to 100.
    assert "limit=100" in stub.last_url


@pytest.mark.asyncio
async def test_search_http_error_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(monkeypatch, raise_with=httpx.ConnectError("boom"))
    assert await reddit.search("anything") == []


@pytest.mark.asyncio
async def test_search_non_200_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(monkeypatch, response=_StubResponse(429, {"error": "rate-limited"}))
    assert await reddit.search("anything") == []


@pytest.mark.asyncio
async def test_search_skips_posts_missing_title_or_permalink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _fake_listing(
        {"title": "", "permalink": "/r/x/comments/1/y"},
        {"title": "ok", "permalink": ""},
        {
            "title": "good",
            "permalink": "/r/x/comments/3/z/",
            "subreddit": "x",
            "score": 1,
        },
    )
    _patch_httpx(monkeypatch, response=_StubResponse(200, payload))
    results = await reddit.search("anything")
    assert len(results) == 1
    assert results[0].title == "good"


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def _fake_post_with_comments(
    *, title: str, body: str, comments: list[str], permalink: str = "/r/x/comments/1/y/"
) -> list[dict[str, Any]]:
    post_listing = {
        "data": {
            "children": [
                {
                    "data": {
                        "title": title,
                        "selftext": body,
                        "permalink": permalink,
                        "subreddit": "x",
                        "score": 99,
                        "num_comments": len(comments),
                    }
                }
            ]
        }
    }
    comments_listing = {
        "data": {
            "children": [{"data": {"body": c}} for c in comments],
        }
    }
    return [post_listing, comments_listing]


@pytest.mark.asyncio
async def test_fetch_returns_source_with_body_and_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _fake_post_with_comments(
        title="Cursor pricing thread",
        body="I have complaints",
        comments=["agreed", "switched to copilot", "[deleted]"],
        permalink="/r/cursor/comments/abc/post/",
    )
    _patch_httpx(monkeypatch, response=_StubResponse(200, payload))
    src = await reddit.fetch("https://www.reddit.com/r/cursor/comments/abc/post/")
    assert src is not None
    assert src.title == "Cursor pricing thread"
    assert "Cursor pricing thread" in src.cleaned_text
    assert "I have complaints" in src.cleaned_text
    assert "agreed" in src.cleaned_text
    assert "switched to copilot" in src.cleaned_text
    # [deleted] / [removed] comments are filtered out.
    assert "[deleted]" not in src.cleaned_text
    assert src.source_kind == "reddit"
    assert src.metadata["subreddit"] == "x"


@pytest.mark.asyncio
async def test_fetch_rejects_non_post_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(monkeypatch, response=_StubResponse(200, []))
    # Subreddit listing — not a post permalink.
    assert await reddit.fetch("https://www.reddit.com/r/cursor/") is None
    # User page.
    assert await reddit.fetch("https://www.reddit.com/u/someone/") is None
    # Off-site URL.
    assert await reddit.fetch("https://example.com/whatever") is None


@pytest.mark.asyncio
async def test_fetch_normalizes_old_reddit_to_www(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _fake_post_with_comments(
        title="t",
        body="b",
        comments=[],
        permalink="/r/cursor/comments/abc/post/",
    )
    stub = _patch_httpx(monkeypatch, response=_StubResponse(200, payload))
    src = await reddit.fetch("https://old.reddit.com/r/cursor/comments/abc/post/")
    assert src is not None
    # Outbound URL was normalized to www.reddit.com + .json.
    assert stub.last_url.startswith("https://www.reddit.com/")
    assert stub.last_url.endswith(".json")


@pytest.mark.asyncio
async def test_fetch_empty_body_and_comments_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _fake_post_with_comments(
        title="",  # blank title
        body="",
        comments=[],
        permalink="/r/x/comments/1/y/",
    )
    _patch_httpx(monkeypatch, response=_StubResponse(200, payload))
    src = await reddit.fetch("https://www.reddit.com/r/x/comments/1/y/")
    assert src is None


@pytest.mark.asyncio
async def test_fetch_http_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(monkeypatch, raise_with=httpx.ConnectError("nope"))
    assert await reddit.fetch("https://www.reddit.com/r/x/comments/1/y/") is None
