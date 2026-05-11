"""Tests for `research_agent.tools.trove` (issue #230)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import trove
from research_agent.tools._errors import MissingCredentialError
from research_agent.tools.models import SearchResult, Source

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "trove"


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("TROVE_API_KEY", "trove-test-key-1234567890abcdef")
    yield


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    trove.reset_for_tests()
    monkeypatch.setattr(trove.asyncio, "sleep", AsyncMock())
    yield
    trove.reset_for_tests()


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _patch_httpx(monkeypatch, *, responder):
    captured: dict[str, list] = {"urls": [], "headers": [], "params": []}

    class _FakeResp:
        def __init__(self, status: int, payload) -> None:
            self.status_code = status
            self._payload = payload

        def json(self):
            if isinstance(self._payload, BaseException):
                raise self._payload
            return self._payload

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        captured["headers"].append(kwargs.get("headers"))

        class _Client:
            async def get(self, url, *, params=None, **_kwargs):
                captured["urls"].append(url)
                captured["params"].append(params)
                status, payload = responder(url, params)
                return _FakeResp(status, payload)

        yield _Client()

    monkeypatch.setattr(trove.httpx, "AsyncClient", _client_factory)
    return captured


async def test_search_happy_path_metadata_only(monkeypatch):
    payload = _fixture("search_white_australia.json")

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await trove.search("White Australia Policy", max_results=2)

    assert len(results) == 2
    first = results[0]
    assert first.source_kind == "trove_search"
    assert first.title == "White Australia Policy"
    assert first.url == "https://trove.nla.gov.au/newspaper/article/18342701"
    assert first.extras["trove_id"] == "18342701"
    assert first.extras["zone"] == "newspaper"
    assert first.extras["pub_date"] == "1901-12-20"
    assert first.extras["fulltext_url"] == "https://nla.gov.au/nla.news-article18342701"
    assert first.extras["metadata_only"] is True

    second = results[1]
    assert second.extras["holding_libraries"] == ["National Library of Australia (ANL)"]
    assert second.extras["fulltext_url"] == (
        "https://example.org/fulltext/white-australia-policy"
    )

    params = captured["params"][0]
    assert params["q"] == "White Australia Policy"
    assert params["category"] == "book,newspaper,image,magazine"
    assert params["encoding"] == "json"
    assert "include" not in params
    assert "key" not in params


async def test_search_uses_x_api_key_header_not_query_param(monkeypatch):
    def _respond(url, params):
        return 200, {"category": []}

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await trove.search("anything")

    headers = captured["headers"][0]
    assert headers["X-API-KEY"] == "trove-test-key-1234567890abcdef"
    assert headers["Accept"] == "application/json"
    assert "key" not in captured["params"][0]
    assert captured["urls"] == ["https://api.trove.nla.gov.au/v3/result"]


async def test_search_accepts_legacy_zone_picture_as_image_category(monkeypatch):
    def _respond(url, params):
        return 200, {"category": []}

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await trove.search("portrait", zone="picture,sound")

    assert captured["params"][0]["category"] == "image,music"


async def test_search_empty_payload_returns_empty(monkeypatch):
    def _respond(url, params):
        return 200, {"category": [{"code": "newspaper", "records": {"article": []}}]}

    _patch_httpx(monkeypatch, responder=_respond)

    assert await trove.search("no hits") == []


async def test_search_missing_key_raises(monkeypatch):
    monkeypatch.delenv("TROVE_API_KEY", raising=False)

    with pytest.raises(MissingCredentialError, match="TROVE_API_KEY"):
        await trove.search("anything")


async def test_search_returns_empty_on_4xx(monkeypatch):
    def _respond(url, params):
        return 403, {}

    _patch_httpx(monkeypatch, responder=_respond)

    assert await trove.search("anything") == []


async def test_search_returns_empty_on_5xx(monkeypatch):
    def _respond(url, params):
        return 500, {}

    _patch_httpx(monkeypatch, responder=_respond)

    assert await trove.search("anything") == []


async def test_search_returns_empty_on_transport_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(trove.httpx, "AsyncClient", _client_factory)

    assert await trove.search("anything") == []


async def test_rate_limit_gate_sleeps_between_calls(monkeypatch):
    clock = [100.0]

    def fake_monotonic() -> float:
        return clock[0]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(trove.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(trove.asyncio, "sleep", fake_sleep)

    def _respond(url, params):
        return 200, {"category": []}

    _patch_httpx(monkeypatch, responder=_respond)

    await asyncio.gather(trove.search("a"), trove.search("b"))

    assert any(s > 0 for s in sleep_calls)


async def test_fetch_work_metadata_populates_required_fields(monkeypatch):
    payload = _fixture("fetch_work.json")

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await trove.fetch("https://trove.nla.gov.au/work/37757621")

    assert source is not None
    assert source.source_kind == "trove_search"
    assert source.title == "The White Australia policy"
    assert source.metadata["trove_id"] == "37757621"
    assert source.metadata["zone"] == "book"
    assert source.metadata["pub_date"] == "1975"
    assert source.metadata["holding_libraries"] == [
        "National Library of Australia (ANL)",
        "State Library Victoria (VSL)",
    ]
    assert source.metadata["fulltext_url"] == (
        "https://example.org/fulltext/white-australia-policy"
    )
    assert "Full-text bodies are intentionally not fetched" in source.cleaned_text
    assert captured["urls"] == ["https://api.trove.nla.gov.au/v3/work/37757621"]
    assert captured["params"] == [{"encoding": "json"}]
    assert "include" not in captured["params"][0]


async def test_fetch_newspaper_ignores_article_text_by_default(monkeypatch):
    payload = _fixture("fetch_newspaper.json")

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await trove.fetch("https://nla.gov.au/nla.news-article18342701")

    assert source is not None
    assert source.metadata["trove_id"] == "18342701"
    assert source.metadata["zone"] == "newspaper"
    assert source.metadata["pub_date"] == "1901-12-20"
    assert source.metadata["fulltext_url"] == "https://nla.gov.au/nla.news-article18342701"
    assert "THIS FULL TEXT MUST NOT BE RENDERED" not in source.cleaned_text
    assert captured["urls"] == ["https://api.trove.nla.gov.au/v3/newspaper/18342701"]
    assert captured["params"] == [{"encoding": "json"}]


async def test_connector_does_not_request_full_text_by_default(monkeypatch):
    search_payload = _fixture("search_white_australia.json")
    fetch_payload = _fixture("fetch_newspaper.json")

    def _respond(url, params):
        if url.endswith("/result"):
            return 200, search_payload
        if url.endswith("/newspaper/18342701"):
            return 200, fetch_payload
        return 404, {}

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await trove.search("White Australia Policy", include="articletext", reclevel="full")
    await trove.fetch("https://trove.nla.gov.au/newspaper/article/18342701")

    assert len(captured["urls"]) == 2
    assert all("articletext" not in str(params).lower() for params in captured["params"])
    assert all("include" not in params for params in captured["params"])
    assert all(
        "/newspaper/18342701" not in url or "articletext" not in url
        for url in captured["urls"]
    )


async def test_fetch_returns_none_for_unrecognised_url(monkeypatch):
    assert await trove.fetch("https://example.com/not-trove") is None


async def test_fetch_returns_none_on_4xx_and_5xx(monkeypatch):
    def _respond_404(url, params):
        return 404, {}

    _patch_httpx(monkeypatch, responder=_respond_404)
    assert await trove.fetch("https://trove.nla.gov.au/work/37757621") is None

    def _respond_500(url, params):
        return 500, {}

    _patch_httpx(monkeypatch, responder=_respond_500)
    assert await trove.fetch("https://trove.nla.gov.au/work/37757621") is None


def test_source_kind_accepts_trove_search() -> None:
    result = SearchResult(
        url="https://trove.nla.gov.au/work/1",
        title="t",
        snippet="s",
        source_kind="trove_search",
    )
    assert result.source_kind == "trove_search"
    source = Source(
        url="https://trove.nla.gov.au/work/1",
        title="t",
        cleaned_text="metadata",
        fetched_at=trove.datetime.now(trove.UTC),
        source_kind="trove_search",
    )
    assert source.source_kind == "trove_search"


def test_smoke_registry_includes_trove_search() -> None:
    from research_agent.tools import TOOL_REGISTRY

    assert "trove_search" in TOOL_REGISTRY


def test_smoke_wrapper_skips_when_key_missing(monkeypatch):
    from research_agent.tools import TOOL_REGISTRY

    monkeypatch.delenv("TROVE_API_KEY", raising=False)

    async def _boom(*_a, **_k):
        raise AssertionError("trove.search must not run when key is missing")

    monkeypatch.setattr(trove, "search", _boom)

    out = TOOL_REGISTRY["trove_search"]("White Australia Policy")

    assert "TROVE_API_KEY" in out
    assert "skipped" in out


def test_smoke_wrapper_formats_non_empty_metadata(monkeypatch):
    from research_agent.tools import TOOL_REGISTRY

    async def _fake_search(query: str, *, max_results: int = 20):
        assert query == "White Australia Policy"
        assert max_results == 5
        return [
            SearchResult(
                url="https://trove.nla.gov.au/newspaper/article/18342701",
                title="White Australia Policy",
                snippet="metadata only",
                source_kind="trove_search",
                extras={
                    "trove_id": "18342701",
                    "zone": "newspaper",
                    "pub_date": "1901-12-20",
                    "fulltext_url": "https://nla.gov.au/nla.news-article18342701",
                },
            )
        ]

    monkeypatch.setattr(trove, "search", _fake_search)

    out = TOOL_REGISTRY["trove_search"]("White Australia Policy")

    assert "White Australia Policy" in out
    assert "metadata-only" in out
    assert "18342701" in out


def test_trove_skill_covers_required_operator_warnings() -> None:
    from research_agent.skills import load_skill

    body = load_skill("connectors", "trove")

    for needle in (
        "TROVE_API_KEY",
        "X-API-KEY",
        "NOT URL parameter",
        "12 months",
        "WITHOUT WARNING",
        "metadata-only",
        "DO NOT auto-fetch full-text bodies",
        "zone=newspaper|picture|book|sound",
        "1803",
        "full-text retrieval",
    ):
        assert needle in body
