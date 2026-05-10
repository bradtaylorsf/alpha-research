"""Tests for ``research_agent.tools.wikidata`` (issue #232)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import wikidata

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "wikidata"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    wikidata.reset_for_tests()
    monkeypatch.setenv("RESEARCH_USER_AGENT", "operator@example.test")
    monkeypatch.setattr(wikidata.asyncio, "sleep", AsyncMock())
    yield
    wikidata.reset_for_tests()


class _FakeResp:
    def __init__(
        self,
        status: int,
        payload,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, *, responder):
    captured: dict[str, list] = {
        "urls": [],
        "headers": [],
        "data": [],
        "methods": [],
    }

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        captured["headers"].append(kwargs.get("headers"))

        class _Client:
            async def post(self, url, *, data=None, **_kwargs):
                captured["methods"].append("post")
                captured["urls"].append(url)
                captured["data"].append(data)
                return responder("post", url, data)

            async def get(self, url, **_kwargs):
                captured["methods"].append("get")
                captured["urls"].append(url)
                captured["data"].append(None)
                return responder("get", url, None)

        yield _Client()

    monkeypatch.setattr(wikidata.httpx, "AsyncClient", _client_factory)
    return captured


async def test_search_happy_path_raw_sparql(monkeypatch: pytest.MonkeyPatch):
    payload = _fixture("search_paris_humans.json")
    query = (
        "SELECT ?item ?itemLabel WHERE { "
        "?item wdt:P31 wd:Q5; wdt:P19 wd:Q90 } LIMIT 3"
    )

    def _respond(method, url, data):
        return _FakeResp(200, payload)

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await wikidata.search(query, max_results=1)

    assert len(results) == 1
    first = results[0]
    assert first.source_kind == "wikidata_search"
    assert first.title == "Victor Hugo"
    assert first.url == "https://www.wikidata.org/wiki/Q535"
    assert "French poet" in first.snippet
    assert first.extras["entity_id"] == "Q535"
    assert first.extras["bindings"]["item"]["entity_id"] == "Q535"
    assert first.extras["bindings"]["birth"]["value"] == "1802-02-26T00:00:00Z"

    assert captured["methods"] == ["post"]
    assert captured["urls"] == ["https://query.wikidata.org/sparql"]
    assert captured["data"] == [{"query": query}]
    headers = captured["headers"][0]
    assert headers["Accept"] == "application/sparql-results+json"
    assert headers["User-Agent"] == (
        "research-agent/0.1 "
        "(+https://github.com/bradtaylorsf/alpha-research; "
        "contact: operator@example.test)"
    )


async def test_search_empty_payload_returns_empty(monkeypatch: pytest.MonkeyPatch):
    payload = _fixture("search_empty.json")

    def _respond(method, url, data):
        return _FakeResp(200, payload)

    _patch_httpx(monkeypatch, responder=_respond)

    assert await wikidata.search("SELECT ?item WHERE {} LIMIT 1") == []


async def test_search_honors_429_retry_after(monkeypatch: pytest.MonkeyPatch):
    payload = _fixture("search_paris_humans.json")
    calls = {"count": 0}
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(wikidata.asyncio, "sleep", fake_sleep)

    def _respond(method, url, data):
        calls["count"] += 1
        if calls["count"] == 1:
            return _FakeResp(429, {}, headers={"Retry-After": "2"})
        return _FakeResp(200, payload)

    _patch_httpx(monkeypatch, responder=_respond)

    results = await wikidata.search("SELECT ?item WHERE {} LIMIT 1")

    assert len(results) == 2
    assert calls["count"] == 2
    assert sleep_calls == [2.0]


async def test_cpu_budget_backoff_when_rolling_duration_exceeds_50s(
    monkeypatch: pytest.MonkeyPatch,
):
    payload = _fixture("search_empty.json")
    clock = [100.0]
    sleep_calls: list[float] = []

    def fake_monotonic() -> float:
        return clock[0]

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(wikidata.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(wikidata.asyncio, "sleep", fake_sleep)
    await wikidata._record_query_duration(51.0)

    def _respond(method, url, data):
        return _FakeResp(200, payload)

    _patch_httpx(monkeypatch, responder=_respond)

    await wikidata.search("SELECT ?item WHERE {} LIMIT 1")

    assert sleep_calls == [60.0]


async def test_search_schema_fail_returns_empty(monkeypatch: pytest.MonkeyPatch):
    def _respond(method, url, data):
        return _FakeResp(200, {"head": {"vars": ["item"]}, "results": {"bindings": {}}})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await wikidata.search("SELECT ?item WHERE {} LIMIT 1") == []


async def test_search_returns_empty_on_transport_error(monkeypatch: pytest.MonkeyPatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def post(self, *_args, **_kwargs):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(wikidata.httpx, "AsyncClient", _client_factory)

    assert await wikidata.search("SELECT ?item WHERE {} LIMIT 1") == []


async def test_fetch_entity_populates_metadata_claims_and_sitelinks(
    monkeypatch: pytest.MonkeyPatch,
):
    payload = _fixture("entity_q42.json")

    def _respond(method, url, data):
        return _FakeResp(200, payload)

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await wikidata.fetch("https://www.wikidata.org/wiki/Q42")

    assert source is not None
    assert source.source_kind == "wikidata_search"
    assert source.url == "https://www.wikidata.org/wiki/Q42"
    assert source.title == "Douglas Adams"
    assert "English writer and humorist" in source.cleaned_text
    assert "P31: Q5" in source.cleaned_text
    assert source.metadata["entity_id"] == "Q42"
    assert source.metadata["label"] == "Douglas Adams"
    assert source.metadata["description"] == "English writer and humorist"
    assert source.metadata["claims"]["P31"] == ["Q5"]
    assert source.metadata["claims"]["P569"] == ["+1952-03-11T00:00:00Z"]
    assert source.metadata["claims"]["P106"] == ["Q36180", "Q6625963"]
    assert source.metadata["sitelinks"]["enwiki"]["title"] == "Douglas Adams"
    assert source.metadata["sitelinks"]["enwiki"]["url"] == (
        "https://en.wikipedia.org/wiki/Douglas_Adams"
    )
    assert captured["methods"] == ["get"]
    assert captured["urls"] == ["https://www.wikidata.org/wiki/Special:EntityData/Q42.json"]
    assert captured["headers"][0]["Accept"] == "application/json"


async def test_fetch_rejects_non_wikidata_q_url() -> None:
    assert await wikidata.fetch("https://example.com/wiki/Q42") is None
    assert await wikidata.fetch("https://www.wikidata.org/wiki/NotQ") is None


def test_smoke_registry_includes_wikidata_search() -> None:
    from research_agent.tools import TOOL_REGISTRY

    assert "wikidata_search" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["wikidata_search"])


def test_registered_kind_links_wikidata_skill() -> None:
    from research_agent.tools._registry import get_kind

    entry = get_kind("wikidata_search")
    assert entry is not None
    assert entry.skill_name == "wikidata"
    assert entry.fetch_fn is wikidata.fetch
