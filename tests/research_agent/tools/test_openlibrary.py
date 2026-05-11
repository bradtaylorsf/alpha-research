"""Tests for ``research_agent.tools.openlibrary`` (issue #236)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import openlibrary
from research_agent.tools.models import SearchResult, Source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "openlibrary"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    openlibrary.reset_for_tests()
    monkeypatch.setenv("RESEARCH_USER_AGENT", "operator@example.test")
    monkeypatch.setattr(openlibrary.asyncio, "sleep", AsyncMock())

    async def _identity_enrichment(source: Source) -> Source:
        return source

    monkeypatch.setattr(openlibrary, "_enrich_with_hathitrust", _identity_enrichment)
    yield
    openlibrary.reset_for_tests()


class _FakeResp:
    def __init__(self, status: int, payload) -> None:
        self.status_code = status
        self._payload = payload

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
        "params": [],
    }

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        captured["headers"].append(kwargs.get("headers"))

        class _Client:
            async def get(self, url, *, params=None, **_kwargs):
                captured["urls"].append(url)
                captured["params"].append(params)
                return responder(url, params or {})

        yield _Client()

    monkeypatch.setattr(openlibrary.httpx, "AsyncClient", _client_factory)
    return captured


async def test_search_happy_path_populates_identifiers_and_ia_scan_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _fixture("search_pullman.json")

    def _respond(url, params):
        assert url == "https://openlibrary.org/search.json"
        return _FakeResp(200, payload)

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await openlibrary.search("Pullman Strike 1894", max_results=2)

    assert len(results) == 2
    first = results[0]
    assert first.source_kind == "openlibrary_search"
    assert first.title == "The Pullman Strike of 1894"
    assert first.url == "https://openlibrary.org/works/OL123W"
    assert "Richard Schneirov" in first.snippet
    assert "IA scans: pullmanstrike1894" in first.snippet
    assert first.published_at == datetime(1999, 1, 1, tzinfo=UTC)
    assert first.extras["isbn"] == ["0252067555", "9780252067556"]
    assert first.extras["oclc"] == ["424023"]
    assert first.extras["lccn"] == ["98045231"]
    assert first.extras["ia_scan_id"] == ["pullmanstrike1894"]
    assert first.extras["edition_count"] == 4
    assert first.extras["author_keys"] == ["OL111A"]

    second = results[1]
    assert second.url == "https://openlibrary.org/works/OL456W"

    params = captured["params"][0]
    assert params["q"] == "Pullman Strike 1894"
    assert params["limit"] == 2
    assert params["fields"] == openlibrary.SEARCH_FIELDS
    headers = captured["headers"][0]
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"] == "operator@example.test"


async def test_search_empty_payload_returns_empty(monkeypatch: pytest.MonkeyPatch):
    def _respond(url, params):
        return _FakeResp(200, _fixture("search_empty.json"))

    captured = _patch_httpx(monkeypatch, responder=_respond)

    assert await openlibrary.search("no such book", max_results=5) == []
    assert captured["urls"] == ["https://openlibrary.org/search.json"]


async def test_user_agent_header_includes_configured_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARCH_USER_AGENT", "alpha-research test ua")

    def _respond(url, params):
        return _FakeResp(200, _fixture("search_empty.json"))

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await openlibrary.search("Pullman Strike", max_results=1)

    assert captured["headers"][0]["User-Agent"] == "alpha-research test ua"


async def test_rate_limit_gate_sleeps_to_enforce_three_rps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    sleep_calls: list[float] = []

    def _monotonic() -> float:
        return clock[0]

    async def _sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    def _respond(url, params):
        return _FakeResp(200, _fixture("search_empty.json"))

    _patch_httpx(monkeypatch, responder=_respond)
    monkeypatch.setattr(openlibrary.time, "monotonic", _monotonic)
    monkeypatch.setattr(openlibrary.asyncio, "sleep", _sleep)

    assert await openlibrary.search("first", max_results=1) == []
    clock[0] += 0.10
    assert await openlibrary.search("second", max_results=1) == []

    assert sleep_calls == pytest.approx([1.0 / 3.0 - 0.10])


@pytest.mark.parametrize("status", [400, 404, 429])
async def test_search_returns_empty_on_4xx(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    def _respond(url, params):
        return _FakeResp(status, {"error": "bad request"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await openlibrary.search("Pullman Strike") == []


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_search_returns_empty_on_5xx(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    def _respond(url, params):
        return _FakeResp(status, {"error": "unavailable"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await openlibrary.search("Pullman Strike") == []


async def test_search_returns_empty_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_args, **_kwargs):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(openlibrary.httpx, "AsyncClient", _client_factory)

    assert await openlibrary.search("Pullman Strike") == []


async def test_fields_parameter_is_focused_and_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(url, params):
        return _FakeResp(200, _fixture("search_empty.json"))

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await openlibrary.search("Pullman Strike", max_results=1)

    fields = captured["params"][0]["fields"]
    assert fields == openlibrary.SEARCH_FIELDS
    assert "*" not in fields
    for field in ("isbn", "oclc", "lccn", "ia", "edition_count", "author_key"):
        assert field in fields.split(",")


async def test_fetch_work_url_returns_source_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(url, params):
        assert params["q"] == "key:/works/OL123W"
        assert params["limit"] == 1
        assert params["fields"] == openlibrary.SEARCH_FIELDS
        return _FakeResp(200, _fixture("search_pullman.json"))

    _patch_httpx(monkeypatch, responder=_respond)

    source = await openlibrary.fetch(
        "https://openlibrary.org/works/OL123W/The_Pullman_Strike_of_1894"
    )

    assert source is not None
    assert source.source_kind == "openlibrary_search"
    assert source.url == "https://openlibrary.org/works/OL123W"
    assert source.title == "The Pullman Strike of 1894"
    assert "## Identifiers" in source.cleaned_text
    assert "OCLC: 424023" in source.cleaned_text
    assert "https://archive.org/details/pullmanstrike1894" in source.cleaned_text
    assert source.metadata["isbn"] == ["0252067555", "9780252067556"]
    assert source.metadata["oclc"] == ["424023"]
    assert source.metadata["lccn"] == ["98045231"]
    assert source.metadata["ia_scan_id"] == ["pullmanstrike1894"]
    assert source.metadata["edition_count"] == 4
    assert source.metadata["author_keys"] == ["OL111A"]


async def test_fetch_identifier_url_builds_identifier_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(url, params):
        assert params["q"] == "oclc:424023"
        return _FakeResp(200, _fixture("search_pullman.json"))

    _patch_httpx(monkeypatch, responder=_respond)

    source = await openlibrary.fetch("https://www.openlibrary.org/oclc/424023")

    assert source is not None
    assert source.metadata["oclc"] == ["424023"]


async def test_fetch_returns_none_on_4xx(monkeypatch: pytest.MonkeyPatch):
    def _respond(url, params):
        return _FakeResp(404, {"error": "not found"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await openlibrary.fetch("https://openlibrary.org/works/OL123W") is None


async def test_fetch_returns_none_on_5xx(monkeypatch: pytest.MonkeyPatch):
    def _respond(url, params):
        return _FakeResp(503, {"error": "unavailable"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await openlibrary.fetch("https://openlibrary.org/works/OL123W") is None


async def test_fetch_rejects_non_openlibrary_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(url, params):
        raise AssertionError("foreign host must not make an HTTP request")

    captured = _patch_httpx(monkeypatch, responder=_respond)

    assert await openlibrary.fetch("https://example.com/works/OL123W") is None
    assert (
        await openlibrary.fetch("https://openlibrary.org.attacker.example/works/OL123W")
        is None
    )
    assert captured["urls"] == []


def test_smoke_registry_includes_openlibrary_search() -> None:
    from research_agent.tools import TOOL_REGISTRY

    assert "openlibrary_search" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["openlibrary_search"])


def test_smoke_wrapper_requires_non_empty_title_and_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def fake_search(query: str, *, max_results: int = 20):
        return [
            SearchResult(
                url="https://openlibrary.org/works/OL123W",
                title="The Pullman Strike of 1894",
                snippet="Pullman Strike metadata",
                source_kind="openlibrary_search",
                extras={
                    "isbn": ["0252067555"],
                    "oclc": ["424023"],
                    "lccn": ["98045231"],
                    "ia_scan_id": ["pullmanstrike1894"],
                    "edition_count": 4,
                    "author_keys": ["OL111A"],
                },
            )
        ]

    monkeypatch.setattr(openlibrary, "search", fake_search)

    out = TOOL_REGISTRY["openlibrary_search"]("Pullman Strike 1894")

    assert "openlibrary_search: returned 1 hits" in out
    assert "The Pullman Strike of 1894" in out
    assert "https://openlibrary.org/works/OL123W" in out
    assert "ia_scan_id: pullmanstrike1894" in out


def test_registered_kind_links_openlibrary_skill() -> None:
    from research_agent.tools._registry import get_kind

    entry = get_kind("openlibrary_search")
    assert entry is not None
    assert entry.skill_name == "openlibrary"
    assert entry.fetch_fn is openlibrary.fetch
    assert "max_results" in entry.optional_payload_knobs


def test_openlibrary_skill_covers_required_operator_topics() -> None:
    from research_agent.skills import load_skill

    body = load_skill("connectors", "openlibrary")

    for needle in (
        "openlibrary.org/search.json?q=<query>&fields=<focused-list>",
        "Always set `fields=`",
        "500KB+",
        "OCLC",
        "ISBN",
        "LCCN",
        "HathiTrust enrichment",
        "3 RPS",
        "RESEARCH_USER_AGENT",
        "bulk data dumps",
        "No auth",
    ):
        assert needle in body
