"""Tests for `research_agent.tools.archive` (issues #15 and #16)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from research_agent.tools import archive


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        url: str = "",
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self.text = text


def _patch_client(monkeypatch, on_get):
    """Replace ``httpx.AsyncClient`` with a fake whose ``get`` is ``on_get``.

    ``on_get`` receives ``(url, headers)`` and returns a :class:`_FakeResponse`.
    Headers are captured from the ``AsyncClient(headers=...)`` init kwargs
    because that's where archive.py sets them.
    """
    captured: dict[str, object] = {"call_count": 0}

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        captured["init_kwargs"] = kwargs
        init_headers = kwargs.get("headers", {})

        class _Client:
            async def get(self, url, *args, **kwargs):
                captured["call_count"] = int(captured["call_count"]) + 1
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


@pytest.fixture(autouse=True)
def _reset_archive_state(monkeypatch):
    """Reset per-process rate-limit state and silence backoff sleeps.

    Tests that need to inspect or override ``asyncio.sleep`` re-patch it
    explicitly; this default keeps the suite fast even when tenacity is
    iterating its retry loop.
    """
    archive.reset_for_tests()
    monkeypatch.setattr(archive.asyncio, "sleep", AsyncMock())
    yield
    archive.reset_for_tests()


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
    _patch_client(monkeypatch, lambda url, _h: _FakeResponse(status_code=404))
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


# ---------------------------------------------------------------------------
# Issue #16 — retry/backoff and rate-limit behaviour
# ---------------------------------------------------------------------------


async def test_save_logs_warning_on_failure(monkeypatch, caplog):
    _patch_client(monkeypatch, lambda url, _h: _FakeResponse(status_code=500))
    with caplog.at_level(logging.WARNING, logger=archive.logger.name):
        result = await archive.save("https://x.example/y")
    assert result is None
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


async def test_save_retries_on_transient_error(monkeypatch):
    """503 twice, then a 200 with Content-Location succeeds on the 3rd try."""
    responses = iter(
        [
            _FakeResponse(status_code=503),
            _FakeResponse(status_code=503),
            _FakeResponse(headers={"Content-Location": "/web/2026/https://x.example/y"}),
        ]
    )
    captured = _patch_client(monkeypatch, lambda url, _h: next(responses))

    result = await archive.save("https://x.example/y")
    assert result == "https://web.archive.org/web/2026/https://x.example/y"
    assert captured["call_count"] == 3


async def test_save_retries_on_timeout_then_succeeds(monkeypatch):
    """First call raises TimeoutException; second returns 200."""
    calls = {"n": 0}

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, url, *args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise archive.httpx.TimeoutException("slow")
                return _FakeResponse(
                    headers={"Content-Location": "/web/2026/https://x.example/y"},
                )

        yield _Client()

    monkeypatch.setattr(archive.httpx, "AsyncClient", _client_factory)

    result = await archive.save("https://x.example/y")
    assert result == "https://web.archive.org/web/2026/https://x.example/y"
    assert calls["n"] == 2


async def test_save_gives_up_after_max_attempts(monkeypatch, caplog):
    captured = _patch_client(monkeypatch, lambda url, _h: _FakeResponse(status_code=503))
    with caplog.at_level(logging.WARNING, logger=archive.logger.name):
        result = await archive.save("https://x.example/y")
    assert result is None
    assert captured["call_count"] == 3
    assert any(
        "wayback save failed after retries" in rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    )


async def test_save_rate_limits_concurrent_calls(monkeypatch):
    """Two concurrent ``save()`` calls must be serialised: the second one
    sleeps at least the rate-limit interval before its submission."""
    clock = [100.0]

    def fake_monotonic() -> float:
        return clock[0]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        # Advance the fake clock so subsequent monotonic() reads reflect
        # that time has "passed."
        clock[0] += seconds

    monkeypatch.setattr(archive.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(archive.asyncio, "sleep", fake_sleep)

    _patch_client(
        monkeypatch,
        lambda url, _h: _FakeResponse(
            headers={"Content-Location": "/web/2026/" + url},
        ),
    )

    results = await asyncio.gather(
        archive.save("https://a.example"),
        archive.save("https://b.example"),
    )

    assert all(r is not None for r in results)
    # The second call to enter the gate must have slept at least the full
    # rate-limit interval (no real time passed between the two acquires).
    assert any(s >= archive._RATE_LIMIT_INTERVAL for s in sleep_calls), (
        f"expected a sleep ≥ {archive._RATE_LIMIT_INTERVAL}s, got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# Issue #106 — archive.today fallback
# ---------------------------------------------------------------------------


def _patch_client_post(monkeypatch, on_post):
    """Replace ``httpx.AsyncClient`` with a fake whose ``post`` is ``on_post``.

    ``on_post(url, data, headers)`` returns a :class:`_FakeResponse`.
    Mirrors :func:`_patch_client` but for archive.today's POST submission.
    """
    captured: dict[str, object] = {"call_count": 0, "posts": []}

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        captured["init_kwargs"] = kwargs
        init_headers = kwargs.get("headers", {})

        class _Client:
            async def post(self, url, *args, data=None, **kwargs):
                captured["call_count"] = int(captured["call_count"]) + 1
                captured["last_url"] = url
                captured["last_data"] = data
                merged = dict(init_headers)
                merged.update(kwargs.get("headers", {}))
                captured["last_headers"] = merged
                posts_list = captured.setdefault("posts", [])
                assert isinstance(posts_list, list)
                posts_list.append({"url": url, "data": data})
                return on_post(url, data, merged)

        yield _Client()

    monkeypatch.setattr(archive.httpx, "AsyncClient", _client_factory)
    return captured


async def test_archive_today_save_returns_url_from_refresh_header(monkeypatch):
    captured = _patch_client_post(
        monkeypatch,
        lambda url, data, _h: _FakeResponse(
            headers={"Refresh": "0; url=https://archive.today/abc12"},
        ),
    )

    result = await archive.archive_today_save("https://x.example/y")
    assert result == "https://archive.today/abc12"
    assert captured["last_url"] == "https://archive.today/submit/"
    # Must be form-encoded body (passed via ``data``), NOT a query string.
    assert captured["last_data"] == {"url": "https://x.example/y"}


async def test_archive_today_save_returns_url_from_location_header(monkeypatch):
    _patch_client_post(
        monkeypatch,
        lambda url, data, _h: _FakeResponse(
            headers={"Location": "https://archive.today/abc12"},
        ),
    )
    assert (
        await archive.archive_today_save("https://x.example/y")
        == "https://archive.today/abc12"
    )


async def test_archive_today_save_normalises_archive_ph_to_today(monkeypatch):
    _patch_client_post(
        monkeypatch,
        lambda url, data, _h: _FakeResponse(
            headers={"Location": "https://archive.ph/abc12"},
        ),
    )
    # archive.ph and archive.today are aliases; only archive.today is canonical.
    assert (
        await archive.archive_today_save("https://x.example/y")
        == "https://archive.today/abc12"
    )


async def test_archive_today_save_uses_response_url_when_no_headers(monkeypatch):
    final = "https://archive.today/abc12"
    _patch_client_post(
        monkeypatch,
        lambda url, data, _h: _FakeResponse(headers={}, url=final),
    )
    assert await archive.archive_today_save("https://x.example/y") == final


async def test_archive_today_save_sends_user_agent_header(monkeypatch):
    monkeypatch.setenv("RESEARCH_USER_AGENT", "custom-bot/3.0")
    seen: dict[str, str] = {}

    def _on_post(url, data, headers):
        seen.update(headers)
        return _FakeResponse(headers={"Location": "https://archive.today/abc"})

    _patch_client_post(monkeypatch, _on_post)
    await archive.archive_today_save("https://x.example")
    assert seen.get("User-Agent") == "custom-bot/3.0"


async def test_archive_today_save_returns_none_on_4xx(monkeypatch):
    _patch_client_post(monkeypatch, lambda url, data, _h: _FakeResponse(status_code=404))
    assert await archive.archive_today_save("https://x.example/y") is None


async def test_archive_today_save_returns_none_on_5xx_after_retries(monkeypatch, caplog):
    captured = _patch_client_post(
        monkeypatch, lambda url, data, _h: _FakeResponse(status_code=503)
    )
    with caplog.at_level(logging.WARNING, logger=archive.logger.name):
        result = await archive.archive_today_save("https://x.example/y")
    assert result is None
    assert captured["call_count"] == 3
    assert any(
        "archive.today save failed after retries" in rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    )


async def test_archive_today_save_returns_none_on_timeout(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def post(self, url, *args, **kwargs):
                raise archive.httpx.TimeoutException("slow")

        yield _Client()

    monkeypatch.setattr(archive.httpx, "AsyncClient", _client_factory)
    assert await archive.archive_today_save("https://x.example/y") is None


async def test_archive_today_save_returns_none_on_connect_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def post(self, url, *args, **kwargs):
                raise archive.httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(archive.httpx, "AsyncClient", _client_factory)
    assert await archive.archive_today_save("https://x.example/y") is None


async def test_archive_today_save_returns_none_for_empty_url(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        raise AssertionError("client should not be created for empty URL")
        yield  # pragma: no cover

    monkeypatch.setattr(archive.httpx, "AsyncClient", _client_factory)
    assert await archive.archive_today_save("") is None


async def test_archive_today_save_skips_on_captcha_body(monkeypatch, caplog):
    """Captcha body in a 429 response must be detected and short-circuit retries.

    archive.today serves its hcaptcha challenge with HTTP 429 + an HTML body
    containing the word ``captcha``. Without sniffing the body we'd loop
    through three retries on a challenge no automation can solve.
    """
    captcha_html = (
        "<html><body><h1>One more step</h1>"
        "<p>Please complete the security check (hcaptcha) to continue.</p>"
        "</body></html>"
    )
    captured = _patch_client_post(
        monkeypatch,
        lambda url, data, _h: _FakeResponse(
            status_code=429, text=captcha_html, headers={}
        ),
    )

    with caplog.at_level(logging.WARNING, logger=archive.logger.name):
        result = await archive.archive_today_save("https://x.example/y")

    assert result is None
    # Captcha must skip retries — exactly one POST, not three.
    assert captured["call_count"] == 1
    assert any(
        "archive.today captcha" in rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    )


async def test_archive_today_save_ignores_submit_url_as_archive(monkeypatch):
    """If the final response.url is the bare /submit/ form, that's not an
    archive landing page — return None rather than a fake archive link."""
    _patch_client_post(
        monkeypatch,
        lambda url, data, _h: _FakeResponse(
            headers={}, url="https://archive.today/submit/"
        ),
    )
    assert await archive.archive_today_save("https://x.example/y") is None


async def test_archive_today_save_rate_limits_concurrent_calls(monkeypatch):
    """Two concurrent archive.today calls must serialise, with the second
    sleeping at least ``_ARCHIVE_TODAY_RATE_INTERVAL`` before its submission."""
    clock = [100.0]

    def fake_monotonic() -> float:
        return clock[0]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(archive.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(archive.asyncio, "sleep", fake_sleep)

    _patch_client_post(
        monkeypatch,
        lambda url, data, _h: _FakeResponse(
            headers={"Location": "https://archive.today/" + str(data["url"])[-3:]},
        ),
    )

    results = await asyncio.gather(
        archive.archive_today_save("https://a.example"),
        archive.archive_today_save("https://b.example"),
    )

    assert all(r is not None for r in results)
    assert any(
        s >= archive._ARCHIVE_TODAY_RATE_INTERVAL for s in sleep_calls
    ), (
        f"expected a sleep ≥ {archive._ARCHIVE_TODAY_RATE_INTERVAL}s, "
        f"got {sleep_calls!r}"
    )
