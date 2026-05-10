"""Tests for ``research_agent.tools.europeana`` (issue #229)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent import config
from research_agent.tools import europeana
from research_agent.tools.models import SearchResult, Source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "europeana"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    config.reset_for_tests()
    europeana.reset_for_tests()
    monkeypatch.setenv("EUROPEANA_API_KEY", "europeana-test-key")
    monkeypatch.setenv("RESEARCH_USER_AGENT", "alpha-research tests")
    monkeypatch.setattr(europeana.asyncio, "sleep", AsyncMock())
    yield
    europeana.reset_for_tests()
    config.reset_for_tests()


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
                captured["params"].append(params or {})
                status, payload = responder(url, params or {})
                return _FakeResp(status, payload)

        yield _Client()

    monkeypatch.setattr(europeana.httpx, "AsyncClient", _client_factory)
    return captured


async def test_search_happy_path_populates_required_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _fixture("search_algerian_war.json")

    def _respond(url, params):
        assert url == "https://api.europeana.eu/api/v2/search.json"
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await europeana.search("Algerian war 1954", lang="fr", max_results=2)

    assert len(results) == 2
    first = results[0]
    assert first.source_kind == "europeana_search"
    assert first.title == "Guerre d'Algerie, 1954-1962"
    assert first.url == (
        "https://www.europeana.eu/en/item/9200429/"
        "BibliographicResource_3000135723944"
    )
    assert first.published_at == datetime(1958, 1, 1, tzinfo=UTC)
    assert first.score == 12.5
    assert "Bibliotheque nationale de France" in first.snippet
    assert first.extras["europeana_id"] == (
        "/9200429/BibliographicResource_3000135723944"
    )
    assert first.extras["dataProvider"] == "Bibliotheque nationale de France"
    assert first.extras["country"] == "France"
    assert first.extras["language"] == "fr"
    assert first.extras["rights"] == (
        "http://creativecommons.org/publicdomain/mark/1.0/"
    )
    assert first.extras["edmIsShownAt"] == (
        "https://gallica.bnf.fr/ark:/12148/bpt6k1234567"
    )

    second = results[1]
    assert second.extras["country"] == "The Netherlands"
    assert second.extras["language"] == "en"
    assert second.extras["rights"] == "http://rightsstatements.org/vocab/InC/1.0/"

    headers = captured["headers"][0]
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"] == "alpha-research tests"
    params = captured["params"][0]
    assert params == {
        "query": "Algerian war 1954",
        "rows": 2,
        "qf": "LANGUAGE:fr",
        "wskey": "europeana-test-key",
    }
    assert "api_key" not in params


async def test_search_empty_payload_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(url, params):
        return 200, _fixture("search_empty.json")

    captured = _patch_httpx(monkeypatch, responder=_respond)

    assert await europeana.search("no such object") == []
    assert captured["urls"] == ["https://api.europeana.eu/api/v2/search.json"]


async def test_search_skips_when_europeana_key_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("EUROPEANA_API_KEY", raising=False)

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        raise AssertionError("Europeana HTTP client should not be constructed")
        yield

    monkeypatch.setattr(europeana.httpx, "AsyncClient", _client_factory)

    with caplog.at_level("WARNING", logger="research_agent.tools.europeana"):
        assert await europeana.search("Algerian war 1954") == []

    assert "would need EUROPEANA_API_KEY" in caplog.text
    assert "Manage API keys" in caplog.text


async def test_search_rate_limit_gate_sleeps_between_calls(
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
        return 200, _fixture("search_empty.json")

    _patch_httpx(monkeypatch, responder=_respond)
    monkeypatch.setattr(europeana.time, "monotonic", _monotonic)
    monkeypatch.setattr(europeana.asyncio, "sleep", _sleep)

    await asyncio.gather(
        europeana.search("first", max_results=1),
        europeana.search("second", max_results=1),
    )

    assert sleep_calls == pytest.approx([1.0])


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
async def test_search_returns_empty_on_4xx(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    def _respond(url, params):
        return status, {"error": "bad request"}

    _patch_httpx(monkeypatch, responder=_respond)

    assert await europeana.search("Algerian war 1954") == []


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_search_returns_empty_on_5xx(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    def _respond(url, params):
        return status, {"error": "unavailable"}

    _patch_httpx(monkeypatch, responder=_respond)

    assert await europeana.search("Algerian war 1954") == []


async def test_search_returns_empty_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_args, **_kwargs):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(europeana.httpx, "AsyncClient", _client_factory)

    assert await europeana.search("Algerian war 1954") == []


async def test_fetch_europeana_item_url_populates_required_source_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(url, params):
        assert url == (
            "https://api.europeana.eu/record/v2/9200429/"
            "BibliographicResource_3000135723944.json"
        )
        return 200, _fixture("item_algerian_war.json")

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await europeana.fetch(
        "https://www.europeana.eu/en/item/9200429/"
        "BibliographicResource_3000135723944"
    )

    assert source is not None
    assert source.source_kind == "europeana_search"
    assert source.url == (
        "https://www.europeana.eu/en/item/9200429/"
        "BibliographicResource_3000135723944"
    )
    assert source.title == "Guerre d'Algerie, 1954-1962"
    assert source.metadata["europeana_id"] == (
        "/9200429/BibliographicResource_3000135723944"
    )
    assert source.metadata["dataProvider"] == "Bibliotheque nationale de France"
    assert source.metadata["country"] == "France"
    assert source.metadata["language"] == "fr"
    assert source.metadata["rights"] == (
        "http://creativecommons.org/publicdomain/mark/1.0/"
    )
    assert source.metadata["edmIsShownAt"] == (
        "https://gallica.bnf.fr/ark:/12148/bpt6k1234567"
    )
    assert "## Description" in source.cleaned_text
    assert "## Metadata" in source.cleaned_text
    assert "Guerre d'Algerie" in source.cleaned_text
    assert captured["params"] == [{"wskey": "europeana-test-key"}]


async def test_fetch_accepts_api_record_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def _respond(url, params):
        return 200, _fixture("item_algerian_war.json")

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await europeana.fetch(
        "https://api.europeana.eu/record/v2/9200429/"
        "BibliographicResource_3000135723944.json?wskey=ignored"
    )

    assert source is not None
    assert source.metadata["europeana_id"] == (
        "/9200429/BibliographicResource_3000135723944"
    )
    assert captured["urls"] == [
        "https://api.europeana.eu/record/v2/9200429/"
        "BibliographicResource_3000135723944.json"
    ]


async def test_fetch_missing_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EUROPEANA_API_KEY", raising=False)

    assert (
        await europeana.fetch(
            "https://www.europeana.eu/en/item/9200429/"
            "BibliographicResource_3000135723944"
        )
        is None
    )


async def test_fetch_rejects_lookalike_host() -> None:
    assert (
        await europeana.fetch(
            "https://www.europeana.eu.evil/en/item/9200429/"
            "BibliographicResource_3000135723944"
        )
        is None
    )


def test_source_kind_accepts_europeana_search() -> None:
    result = SearchResult(
        url="https://www.europeana.eu/en/item/9200429/x",
        title="Guerre d'Algerie, 1954-1962",
        snippet="Europeana item",
        source_kind="europeana_search",
    )
    assert result.source_kind == "europeana_search"

    source = Source(
        url="https://www.europeana.eu/en/item/9200429/x",
        title="Guerre d'Algerie, 1954-1962",
        cleaned_text="body",
        fetched_at=datetime.now(UTC),
        source_kind="europeana_search",
    )
    assert source.source_kind == "europeana_search"


def test_module_declares_kind_constant() -> None:
    assert europeana.KIND == "europeana_search"


def test_smoke_registry_includes_europeana_search() -> None:
    from research_agent.tools import TOOL_REGISTRY

    assert "europeana_search" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["europeana_search"])


def test_smoke_wrapper_skips_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from research_agent.tools import TOOL_REGISTRY

    monkeypatch.delenv("EUROPEANA_API_KEY", raising=False)

    async def _boom(*_a, **_k):
        raise AssertionError("europeana.search must not run when key is missing")

    monkeypatch.setattr(europeana, "search", _boom)

    out = TOOL_REGISTRY["europeana_search"]("Algerian war 1954")

    assert "would need EUROPEANA_API_KEY" in out
    assert "Manage API keys" in out
    assert "live test skipped" in out


def test_smoke_wrapper_formats_non_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def _fake_search(query: str, *, max_results: int = 20):
        assert query == "Algerian war 1954"
        assert max_results == 5
        return [
            SearchResult(
                url=(
                    "https://www.europeana.eu/en/item/9200429/"
                    "BibliographicResource_3000135723944"
                ),
                title="Guerre d'Algerie, 1954-1962",
                snippet="Europeana archival metadata.",
                source_kind="europeana_search",
                extras={
                    "europeana_id": "/9200429/BibliographicResource_3000135723944",
                    "dataProvider": "Bibliotheque nationale de France",
                    "country": "France",
                    "language": "fr",
                    "rights": "http://creativecommons.org/publicdomain/mark/1.0/",
                    "edmIsShownAt": "https://gallica.bnf.fr/ark:/12148/bpt6k1234567",
                },
            )
        ]

    monkeypatch.setattr(europeana, "search", _fake_search)

    out = TOOL_REGISTRY["europeana_search"]("Algerian war 1954")

    assert "europeana_search: returned 1 hits" in out
    assert "Guerre d'Algerie, 1954-1962" in out
    assert "dataProvider: Bibliotheque nationale de France" in out
    assert "edmIsShownAt: https://gallica.bnf.fr/ark:/12148/bpt6k1234567" in out


def test_smoke_wrapper_fails_on_empty_results(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def _fake_search(query: str, *, max_results: int = 20):
        return []

    monkeypatch.setattr(europeana, "search", _fake_search)

    with pytest.raises(SystemExit) as exc:
        TOOL_REGISTRY["europeana_search"]("Algerian war 1954")

    assert exc.value.code == 1
    assert "returned 0 results" in capsys.readouterr().err


def test_registry_entry_links_skill() -> None:
    from research_agent.tools._registry import get_kind

    entry = get_kind("europeana_search")

    assert entry is not None
    assert entry.skill_name == "europeana"
    assert entry.module_name == "europeana"
    assert entry.host_patterns == (
        "api.europeana.eu",
        "europeana.eu",
        "www.europeana.eu",
    )


def test_doctor_registry_skill_coherence_passes_for_europeana() -> None:
    from research_agent.doctor import check_registry_skill_coherence

    rows = check_registry_skill_coherence()
    europeana_rows = [
        row for row in rows if row.name == "registry_skill:europeana_search"
    ]

    assert len(europeana_rows) == 1
    assert europeana_rows[0].status == "ok"


def test_europeana_skill_covers_required_operator_topics() -> None:
    from research_agent.skills.loader import load_skill

    body = load_skill("connectors", "europeana")

    for token in (
        "EUROPEANA_API_KEY",
        "2025-05-28",
        "Manage API keys",
        "qf=LANGUAGE:fr",
        "qf=COUNTRY:France",
        "multilingual-source-handling",
        'Source.metadata["rights"]',
        "English keywords",
        "native-language keywords",
        "?wskey=<key>",
        "api_key=",
    ):
        assert token in body
