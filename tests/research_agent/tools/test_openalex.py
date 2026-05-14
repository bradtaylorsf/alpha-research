"""Tests for ``research_agent.tools.openalex`` (issue #241)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import openalex
from research_agent.tools.models import SearchResult

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "openalex"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    openalex.reset_for_tests()
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.setenv("RESEARCH_USER_AGENT", "research-agent test@example.org")
    monkeypatch.setattr(openalex.asyncio, "sleep", AsyncMock())
    yield
    openalex.reset_for_tests()


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


def _first_work() -> dict:
    return _fixture("search-project2025.json")["results"][0]


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
                captured["params"].append(params or {})
                return responder(url, params or {})

        yield _Client()

    monkeypatch.setattr(openalex.httpx, "AsyncClient", _client_factory)
    return captured


async def test_search_happy_path_populates_academic_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _fixture("search-project2025.json")

    def _respond(url, params):
        assert url == "https://api.openalex.org/works"
        return _FakeResp(200, payload)

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await openalex.search(
        "Project 2025 unitary executive theory",
        max_results=2,
        filter="publication_year:>2020,is_oa:true",
        sort="relevance_score:desc",
    )

    assert len(results) == 1
    first = results[0]
    assert first.source_kind == "openalex_search"
    assert first.title == "Project 2025 and the unitary executive theory"
    assert first.url == "https://openalex.org/W4401234567"
    assert first.published_at == datetime(2025, 3, 14, tzinfo=UTC)
    assert "Project 2025 applies unitary executive theory" in first.snippet
    assert first.extras["doi"] == "10.5555/project2025.2025.001"
    assert first.extras["openalex_id"] == "W4401234567"
    assert first.extras["pub_year"] == 2025
    assert first.extras["authors"] == ["Jane Doe", "Alex Smith"]
    assert first.extras["host_venue"] == "Journal of Constitutional Studies"
    assert first.extras["abstract"] == (
        "Project 2025 applies unitary executive theory to administrative governance."
    )
    assert first.extras["citation_count"] == 7
    assert (
        first.extras["open_access_url"]
        == "https://example.org/articles/project-2025-unitary-executive.pdf"
    )

    params = captured["params"][0]
    assert params["search"] == "Project 2025 unitary executive theory"
    assert params["per_page"] == 2
    assert params["filter"] == "publication_year:>2020,is_oa:true"
    assert params["sort"] == "relevance_score:desc"
    assert params["mailto"] == "test@example.org"
    assert "api_key" not in params
    headers = captured["headers"][0]
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"] == "research-agent test@example.org"


async def test_search_caps_per_page_at_200(monkeypatch: pytest.MonkeyPatch) -> None:
    def _respond(url, params):
        return _FakeResp(200, {"results": []})

    captured = _patch_httpx(monkeypatch, responder=_respond)

    assert await openalex.search("unitary executive", max_results=500) == []
    assert captured["params"][0]["per_page"] == 200


async def test_search_empty_payload_returns_empty(monkeypatch: pytest.MonkeyPatch):
    def _respond(url, params):
        return _FakeResp(200, {"meta": {"count": 0}, "results": []})

    captured = _patch_httpx(monkeypatch, responder=_respond)

    assert await openalex.search("no such article", max_results=5) == []
    assert captured["urls"] == ["https://api.openalex.org/works"]


async def test_abstract_reconstruction_lands_in_source_cleaned_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(url, params):
        assert url == "https://api.openalex.org/works/W4401234567"
        return _FakeResp(200, _first_work())

    _patch_httpx(monkeypatch, responder=_respond)

    source = await openalex.fetch("https://openalex.org/W4401234567")

    assert source is not None
    assert source.source_kind == "openalex_search"
    assert source.url == "https://openalex.org/W4401234567"
    assert source.metadata["abstract"] == (
        "Project 2025 applies unitary executive theory to administrative governance."
    )
    assert "## Abstract" in source.cleaned_text
    assert "Project 2025 applies unitary executive theory" in source.cleaned_text
    assert source.metadata["doi"] == "10.5555/project2025.2025.001"
    assert source.metadata["openalex_id"] == "W4401234567"
    assert source.metadata["host_venue"] == "Journal of Constitutional Studies"
    assert source.metadata["citation_count"] == 7
    assert (
        source.metadata["open_access_url"]
        == "https://example.org/articles/project-2025-unitary-executive.pdf"
    )


async def test_fetch_accepts_api_and_doi_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_urls: list[str] = []

    def _respond(url, params):
        seen_urls.append(url)
        return _FakeResp(200, _first_work())

    _patch_httpx(monkeypatch, responder=_respond)

    api_source = await openalex.fetch("https://api.openalex.org/works/W4401234567")
    doi_source = await openalex.fetch(
        "https://doi.org/10.5555/project2025.2025.001"
    )

    assert api_source is not None
    assert doi_source is not None
    assert seen_urls == [
        "https://api.openalex.org/works/W4401234567",
        "https://api.openalex.org/works/https://doi.org/10.5555/project2025.2025.001",
    ]


async def test_polite_pool_mailto_and_optional_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARCH_USER_AGENT", "muckwire (mailto:ops@example.org)")
    monkeypatch.setenv("OPENALEX_API_KEY", "oa_test_key")

    def _respond(url, params):
        return _FakeResp(200, {"results": []})

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await openalex.search("Project 2025", max_results=1)

    params = captured["params"][0]
    assert params["mailto"] == "ops@example.org"
    assert params["api_key"] == "oa_test_key"
    assert captured["headers"][0]["User-Agent"] == (
        "muckwire (mailto:ops@example.org)"
    )


async def test_anonymous_requests_omit_mailto_and_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARCH_USER_AGENT", "muckwire no contact")

    def _respond(url, params):
        return _FakeResp(200, {"results": []})

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await openalex.search("Project 2025", max_results=1)

    assert "mailto" not in captured["params"][0]
    assert "api_key" not in captured["params"][0]


async def test_rate_limit_gate_uses_5_rps_when_identified(
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
        return _FakeResp(200, {"results": []})

    _patch_httpx(monkeypatch, responder=_respond)
    monkeypatch.setattr(openalex.time, "monotonic", _monotonic)
    monkeypatch.setattr(openalex.asyncio, "sleep", _sleep)

    assert await openalex.search("first", max_results=1) == []
    clock[0] += 0.05
    assert await openalex.search("second", max_results=1) == []

    assert sleep_calls == pytest.approx([0.20 - 0.05])


async def test_rate_limit_gate_uses_1_rps_without_identification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARCH_USER_AGENT", "muckwire no contact")
    clock = [100.0]
    sleep_calls: list[float] = []

    def _monotonic() -> float:
        return clock[0]

    async def _sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    def _respond(url, params):
        return _FakeResp(200, {"results": []})

    _patch_httpx(monkeypatch, responder=_respond)
    monkeypatch.setattr(openalex.time, "monotonic", _monotonic)
    monkeypatch.setattr(openalex.asyncio, "sleep", _sleep)

    assert await openalex.search("first", max_results=1) == []
    clock[0] += 0.25
    assert await openalex.search("second", max_results=1) == []

    assert sleep_calls == pytest.approx([1.0 - 0.25])


@pytest.mark.parametrize("status", [400, 403, 404, 429])
async def test_search_returns_empty_on_4xx(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    def _respond(url, params):
        return _FakeResp(status, {"error": "bad request"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await openalex.search("Project 2025") == []


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_search_returns_empty_on_5xx(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    def _respond(url, params):
        return _FakeResp(status, {"error": "unavailable"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await openalex.search("Project 2025") == []


async def test_fetch_returns_none_on_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    def _respond(url, params):
        return _FakeResp(404, {"error": "not found"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await openalex.fetch("https://openalex.org/W4401234567") is None


async def test_fetch_returns_none_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    def _respond(url, params):
        return _FakeResp(503, {"error": "unavailable"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await openalex.fetch("https://openalex.org/W4401234567") is None


async def test_search_returns_empty_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_args, **_kwargs):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(openalex.httpx, "AsyncClient", _client_factory)

    assert await openalex.search("Project 2025") == []


async def test_fetch_rejects_foreign_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    def _respond(url, params):
        raise AssertionError("foreign host must not make an HTTP request")

    captured = _patch_httpx(monkeypatch, responder=_respond)

    assert await openalex.fetch("https://example.com/W4401234567") is None
    assert await openalex.fetch("https://openalex.org.attacker.example/W1") is None
    assert captured["urls"] == []


def test_smoke_registry_includes_openalex_search() -> None:
    from research_agent.tools import TOOL_REGISTRY

    assert "openalex_search" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["openalex_search"])


def test_smoke_wrapper_requires_non_empty_title_and_doi_or_openalex_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def fake_search(query: str, *, max_results: int = 20):
        return [
            SearchResult(
                url="https://openalex.org/W4401234567",
                title="Project 2025 and the unitary executive theory",
                snippet="Project 2025 applies unitary executive theory",
                source_kind="openalex_search",
                extras={
                    "doi": "10.5555/project2025.2025.001",
                    "openalex_id": "W4401234567",
                    "pub_year": 2025,
                    "authors": ["Jane Doe"],
                    "host_venue": "Journal of Constitutional Studies",
                    "abstract": "Project 2025 applies unitary executive theory.",
                    "citation_count": 7,
                    "open_access_url": "https://example.org/paper.pdf",
                },
            )
        ]

    monkeypatch.setattr(openalex, "search", fake_search)

    out = TOOL_REGISTRY["openalex_search"]("Project 2025 unitary executive theory")

    assert "openalex_search: returned 1 hits" in out
    assert "Project 2025 and the unitary executive theory" in out
    assert "https://openalex.org/W4401234567" in out
    assert "doi: 10.5555/project2025.2025.001" in out


def test_registered_kind_links_openalex_skill() -> None:
    from research_agent.tools._registry import get_kind

    entry = get_kind("openalex_search")
    assert entry is not None
    assert entry.skill_name == "openalex"
    assert entry.fetch_fn is openalex.fetch
    assert "filter" in entry.optional_payload_knobs
    assert "sort" in entry.optional_payload_knobs
    assert "doi.org" in entry.host_patterns


def test_openalex_skill_covers_required_operator_topics() -> None:
    from research_agent.skills import load_skill

    body = load_skill("connectors", "openalex")

    for needle in (
        "No key required today",
        "OPENALEX_API_KEY",
        "RESEARCH_USER_AGENT",
        "mailto=",
        "5 RPS",
        "1 RPS",
        "inverted indexes",
        "Source.cleaned_text",
        "250M+ scholarly works",
        "JSTOR Constellate",
        "2025-07-01",
        "publication_year:>2020,is_oa:true",
        'metadata["open_access_url"]',
        "web_fetch",
    ):
        assert needle in body
