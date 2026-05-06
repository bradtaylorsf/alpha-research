"""Tests for `research_agent.tools.gdelt` (issue #105)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import gdelt

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    gdelt.reset_for_tests()
    monkeypatch.setattr(gdelt.asyncio, "sleep", AsyncMock())
    yield
    gdelt.reset_for_tests()


# ---------------------------------------------------------------------------
# Test payloads
# ---------------------------------------------------------------------------


_ARTLIST_PAYLOAD = {
    "articles": [
        {
            "url": "https://example.com/anysphere-cursor-launch",
            "url_mobile": "",
            "title": "Anysphere ships Cursor 1.0",
            "seendate": "20260301T120000Z",
            "socialimage": "https://example.com/img.png",
            "domain": "example.com",
            "language": "English",
            "sourcecountry": "United States",
        },
        {
            "url": "https://news.example.org/cursor-coverage",
            "title": "Cursor adoption climbs",
            "seendate": "20260228T090000Z",
            "domain": "news.example.org",
            "language": "English",
            "sourcecountry": "United Kingdom",
        },
    ]
}


_TIMELINE_PAYLOAD = {
    "timeline": [
        {
            "data": [
                {"date": "20260301T000000Z", "value": -1.5},
                {"date": "20260301T010000Z", "value": -0.7},
                {"date": "20260301T020000Z", "value": 2.1},
            ]
        }
    ]
}


# ---------------------------------------------------------------------------
# httpx mock plumbing
# ---------------------------------------------------------------------------


def _patch_httpx(monkeypatch, *, get_responder):
    captured: dict[str, list] = {
        "urls": [],
        "params": [],
        "headers": [],
    }

    class _FakeResp:
        def __init__(self, status: int, text: str) -> None:
            self.status_code = status
            self.text = text

        def json(self):
            return json.loads(self.text)

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        captured["headers"].append(kwargs.get("headers"))

        class _Client:
            async def get(self, url, *, params=None, **_kwargs):
                captured["urls"].append(url)
                captured["params"].append(params)
                status, text = get_responder(url, params)
                return _FakeResp(status, text)

        yield _Client()

    monkeypatch.setattr(gdelt.httpx, "AsyncClient", _client_factory)
    return captured


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_parses_artlist(monkeypatch):
    payload = json.dumps(_ARTLIST_PAYLOAD)

    def _get(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, get_responder=_get)

    results = await gdelt.search("Anysphere Cursor", max_results=10)

    assert len(captured["urls"]) == 1
    assert captured["urls"][0] == gdelt._BASE_URL
    params = captured["params"][0]
    assert params["mode"] == "ArtList"
    assert params["format"] == "json"
    assert params["maxrecords"] == 10
    assert params["sort"] == "datedesc"
    assert "sourcelang:eng" in params["query"]
    assert "Anysphere Cursor" in params["query"]
    # No since => no timespan param.
    assert "timespan" not in params

    assert len(results) == 2
    hit = results[0]
    assert hit.source_kind == "gdelt"
    assert hit.url == "https://example.com/anysphere-cursor-launch"
    assert hit.title == "Anysphere ships Cursor 1.0"
    assert hit.extras["domain"] == "example.com"
    assert hit.extras["language"] == "English"
    assert hit.extras["sourcecountry"] == "United States"
    assert hit.extras["socialimage"] == "https://example.com/img.png"
    # Published_at parsed from YYYYMMDDTHHMMSSZ.
    assert hit.published_at == datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


async def test_search_handles_non_json(monkeypatch):
    """HTML error pages must not crash — they surface as []."""

    def _get(url, params):
        return 200, "<html>not json</html>"

    _patch_httpx(monkeypatch, get_responder=_get)

    assert await gdelt.search("anything") == []


async def test_search_passes_timespan(monkeypatch):
    payload = json.dumps({"articles": []})

    def _get(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, get_responder=_get)

    since = datetime.now(UTC) - timedelta(days=2)
    await gdelt.search("anything", since=since)

    params = captured["params"][0]
    assert params["timespan"] == "2d"


async def test_search_http_error_returns_empty(monkeypatch):
    def _get(url, params):
        return 500, ""

    _patch_httpx(monkeypatch, get_responder=_get)

    assert await gdelt.search("anything") == []


async def test_search_transport_error_returns_empty(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(gdelt.httpx, "AsyncClient", _client_factory)

    assert await gdelt.search("anything") == []


async def test_search_unknown_language_passes_through(monkeypatch):
    """An unmapped language value should pass through verbatim as the iso3 token."""
    payload = json.dumps({"articles": []})

    def _get(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, get_responder=_get)

    await gdelt.search("anything", language="kor")
    assert "sourcelang:kor" in captured["params"][0]["query"]


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def test_fetch_delegates_to_web_fetch(monkeypatch):
    """``gdelt.fetch`` must be a no-op pass-through to ``web_fetch.fetch``."""
    from research_agent.tools import web_fetch

    sentinel_source = object()
    mock_fetch = AsyncMock(return_value=sentinel_source)
    monkeypatch.setattr(web_fetch, "fetch", mock_fetch)

    url = "https://example.com/article"
    result = await gdelt.fetch(url)

    assert result is sentinel_source
    mock_fetch.assert_awaited_once_with(url)


async def test_fetch_empty_url_returns_none():
    assert await gdelt.fetch("") is None


# ---------------------------------------------------------------------------
# tone_timeline()
# ---------------------------------------------------------------------------


async def test_tone_timeline_parses_payload(monkeypatch):
    payload = json.dumps(_TIMELINE_PAYLOAD)

    def _get(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, get_responder=_get)

    series = await gdelt.tone_timeline("Anysphere Cursor")

    params = captured["params"][0]
    assert params["mode"] == "TimelineTone"
    assert params["format"] == "json"

    assert len(series) == 3
    assert series[0]["datetime"] == datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)
    assert series[0]["value"] == -1.5
    assert series[2]["value"] == pytest.approx(2.1)


async def test_tone_timeline_handles_empty_shape(monkeypatch):
    """Missing ``timeline`` key (or unexpected shape) returns []."""
    payload = json.dumps({"unexpected": "shape"})

    def _get(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, get_responder=_get)

    assert await gdelt.tone_timeline("anything") == []


async def test_tone_timeline_handles_non_json(monkeypatch):
    def _get(url, params):
        return 200, "<html>blocked</html>"

    _patch_httpx(monkeypatch, get_responder=_get)

    assert await gdelt.tone_timeline("anything") == []


async def test_tone_timeline_http_error_returns_empty(monkeypatch):
    def _get(url, params):
        return 503, ""

    _patch_httpx(monkeypatch, get_responder=_get)

    assert await gdelt.tone_timeline("anything") == []


# ---------------------------------------------------------------------------
# Rate limit gate
# ---------------------------------------------------------------------------


async def test_rate_limit_gate_serializes_calls(monkeypatch):
    """Two concurrent _get calls must space out by ~_RATE_LIMIT_INTERVAL."""
    clock = [100.0]

    def fake_monotonic() -> float:
        return clock[0]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(gdelt.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(gdelt.asyncio, "sleep", fake_sleep)

    payload = json.dumps({"articles": []})

    def _get(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, get_responder=_get)

    await asyncio.gather(
        gdelt.search("a"),
        gdelt.search("b"),
    )

    assert any(abs(s - gdelt._RATE_LIMIT_INTERVAL) < 1e-6 for s in sleep_calls), (
        f"expected a ~{gdelt._RATE_LIMIT_INTERVAL}s sleep through the rate gate; "
        f"got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# Smoke registration
# ---------------------------------------------------------------------------


def test_smoke_registry_includes_gdelt():
    from research_agent.tools import TOOL_REGISTRY

    assert "gdelt" in TOOL_REGISTRY
