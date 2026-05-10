"""Tests for ``research_agent.tools.gallica`` (issue #238)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import gallica
from research_agent.tools.models import SearchResult, Source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "gallica"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    gallica.reset_for_tests()
    monkeypatch.setenv("RESEARCH_USER_AGENT", "operator@example.test")
    monkeypatch.setattr(gallica.asyncio, "sleep", AsyncMock())
    yield
    gallica.reset_for_tests()


class _FakeResp:
    def __init__(self, status: int, text: str) -> None:
        self.status_code = status
        self.text = text


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, *, responder):
    captured: dict[str, list] = {"urls": [], "headers": [], "params": []}

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        captured["headers"].append(kwargs.get("headers"))

        class _Client:
            async def get(self, url, *, params=None, **_kwargs):
                captured["urls"].append(url)
                captured["params"].append(params)
                return responder(url, params or {})

        yield _Client()

    monkeypatch.setattr(gallica.httpx, "AsyncClient", _client_factory)
    return captured


async def test_search_happy_path_parses_sru_dublin_core_xml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _fixture("search-guerre-algerie.xml")

    def _respond(url, params):
        assert url == gallica._SRU_URL
        return _FakeResp(200, payload)

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await gallica.search("guerre d'Algerie", max_results=75)

    assert len(results) == 2
    first = results[0]
    assert first.source_kind == "gallica_search"
    assert first.title == "La guerre d'Algerie dans la presse francaise"
    assert first.url == "https://gallica.bnf.fr/ark:/12148/bpt6k1234567"
    assert first.published_at == datetime(1956, 1, 1, tzinfo=UTC)
    assert "Date: 1956" in first.snippet
    assert "Bibliotheque nationale de France" in first.snippet
    assert first.extras["ark"] == "ark:/12148/bpt6k1234567"
    assert first.extras["dc:type"] == "texte"
    assert first.extras["dc:date"] == "1956"
    assert first.extras["dc:language"] == "fre"
    assert first.extras["dc:source"] == "Bibliotheque nationale de France"

    second = results[1]
    assert second.url == "https://gallica.bnf.fr/ark:/12148/bpt6k7654321"
    assert second.published_at == datetime(1961, 4, 23, tzinfo=UTC)

    params = captured["params"][0]
    assert params["operation"] == "searchRetrieve"
    assert params["version"] == "1.2"
    assert params["query"] == 'gallica all "guerre d\'Algerie"'
    assert params["maximumRecords"] == 50
    assert params["suggest"] == 0
    assert captured["headers"][0]["Accept"].startswith("application/xml")
    assert captured["headers"][0]["User-Agent"] == "operator@example.test"


async def test_search_empty_sru_response_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_xml = """<?xml version="1.0"?>
    <srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">
      <srw:numberOfRecords>0</srw:numberOfRecords>
      <srw:records />
    </srw:searchRetrieveResponse>
    """

    def _respond(url, params):
        return _FakeResp(200, empty_xml)

    _patch_httpx(monkeypatch, responder=_respond)

    assert await gallica.search("no hits") == []


def test_build_cql_query_escapes_quotes_and_backslashes() -> None:
    cql = gallica.build_cql_query('  guerre  "memoire" \\ archives  ')

    assert cql == 'gallica all "guerre \\"memoire\\" \\\\ archives"'


async def test_xml_namespace_handling_accepts_default_sru_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = """<?xml version="1.0"?>
    <searchRetrieveResponse
      xmlns="http://www.loc.gov/zing/srw/"
      xmlns:dc="http://purl.org/dc/elements/1.1/">
      <records>
        <record>
          <recordData>
            <dc:title>Journal de la Republique francaise</dc:title>
            <dc:identifier>http://gallica.bnf.fr/ark:/12148/bpt6k9999999</dc:identifier>
            <dc:type>periodique</dc:type>
            <dc:date>1958</dc:date>
            <dc:language>fre</dc:language>
            <dc:source>Gallica</dc:source>
          </recordData>
        </record>
      </records>
    </searchRetrieveResponse>
    """

    def _respond(url, params):
        return _FakeResp(200, payload)

    _patch_httpx(monkeypatch, responder=_respond)

    results = await gallica.search("Republique francaise", max_results=1)

    assert len(results) == 1
    assert results[0].title == "Journal de la Republique francaise"
    assert results[0].extras["ark"] == "ark:/12148/bpt6k9999999"
    assert results[0].extras["dc:type"] == "periodique"


async def test_malformed_live_xml_recovers_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live Gallica can return HTTP 200 with mismatched tags in metadata."""
    payload = """<?xml version="1.0" encoding="UTF-8"?>
    <srw:searchRetrieveResponse
      xmlns:srw="http://www.loc.gov/zing/srw/"
      xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
      xmlns:dc="http://purl.org/dc/elements/1.1/">
      <srw:records>
        <srw:record>
          <srw:recordData>
            <oai_dc:dc>
              <dc:title>La <em>guerre</em> d'Algerie</dc:title>
              <dc:description>Malformed <i>embedded</b> metadata</dc:description>
              <dc:identifier>https://gallica.bnf.fr/ark:/12148/bpt6k1111111</dc:identifier>
              <dc:type>texte</dc:type>
              <dc:date>1957</dc:date>
              <dc:language>fre</dc:language>
              <dc:source>Gallica</dc:source>
            </oai_dc:dc>
          </srw:recordData>
        </srw:record>
      </srw:records>
    </srw:searchRetrieveResponse>
    """

    def _respond(url, params):
        return _FakeResp(200, payload)

    _patch_httpx(monkeypatch, responder=_respond)

    results = await gallica.search("guerre d'Algerie", max_results=5)

    assert len(results) == 1
    assert results[0].title == "La guerre d'Algerie"
    assert results[0].url == "https://gallica.bnf.fr/ark:/12148/bpt6k1111111"
    assert results[0].extras["ark"] == "ark:/12148/bpt6k1111111"
    assert "Malformed embedded metadata" in results[0].snippet


async def test_rate_limit_gate_sleeps_to_enforce_one_rps(
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
        return _FakeResp(200, _fixture("search-guerre-algerie.xml"))

    _patch_httpx(monkeypatch, responder=_respond)
    monkeypatch.setattr(gallica.time, "monotonic", _monotonic)
    monkeypatch.setattr(gallica.asyncio, "sleep", _sleep)

    assert await gallica.search("first", max_results=1)
    clock[0] += 0.25
    assert await gallica.search("second", max_results=1)

    assert sleep_calls == pytest.approx([0.75])


@pytest.mark.parametrize("status", [400, 404, 429])
async def test_search_returns_empty_on_4xx(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    def _respond(url, params):
        return _FakeResp(status, "<error />")

    _patch_httpx(monkeypatch, responder=_respond)

    assert await gallica.search("anything") == []


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_search_returns_empty_on_5xx(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    def _respond(url, params):
        return _FakeResp(status, "<error />")

    _patch_httpx(monkeypatch, responder=_respond)

    assert await gallica.search("anything") == []


async def test_search_returns_empty_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_args, **_kwargs):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(gallica.httpx, "AsyncClient", _client_factory)

    assert await gallica.search("anything") == []


async def test_fetch_ark_url_returns_source_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(url, params):
        assert params["query"] == 'dc.identifier all "ark:/12148/bpt6k1234567"'
        assert params["maximumRecords"] == 1
        assert params["suggest"] == 0
        return _FakeResp(200, _fixture("search-guerre-algerie.xml"))

    _patch_httpx(monkeypatch, responder=_respond)

    source = await gallica.fetch(
        "https://gallica.bnf.fr/ark:/12148/bpt6k1234567/f1.item"
    )

    assert source is not None
    assert source.source_kind == "gallica_search"
    assert source.url == "https://gallica.bnf.fr/ark:/12148/bpt6k1234567"
    assert source.title == "La guerre d'Algerie dans la presse francaise"
    assert source.metadata["ark"] == "ark:/12148/bpt6k1234567"
    assert source.metadata["dc:type"] == "texte"
    assert source.metadata["dc:date"] == "1956"
    assert source.metadata["dc:language"] == "fre"
    assert source.metadata["dc:source"] == "Bibliotheque nationale de France"
    assert "## Identifiers" in source.cleaned_text
    assert "ark:/12148/bpt6k1234567" in source.cleaned_text


async def test_fetch_rejects_foreign_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    def _respond(url, params):
        raise AssertionError("foreign host must not make an HTTP request")

    captured = _patch_httpx(monkeypatch, responder=_respond)

    assert await gallica.fetch("https://example.com/ark:/12148/bpt6k1234567") is None
    assert (
        await gallica.fetch("https://gallica.bnf.fr.attacker.example/ark:/12148/x")
        is None
    )
    assert captured["urls"] == []


def test_source_kind_accepts_gallica_search() -> None:
    result = SearchResult(
        url="https://gallica.bnf.fr/ark:/12148/bpt6k1234567",
        title="t",
        snippet="s",
        source_kind="gallica_search",
    )
    assert result.source_kind == "gallica_search"
    source = Source(
        url="https://gallica.bnf.fr/ark:/12148/bpt6k1234567",
        title="t",
        cleaned_text="metadata",
        fetched_at=datetime.now(UTC),
        source_kind="gallica_search",
    )
    assert source.source_kind == "gallica_search"


def test_smoke_registry_includes_gallica_search() -> None:
    from research_agent.tools import TOOL_REGISTRY

    assert "gallica_search" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["gallica_search"])


def test_smoke_wrapper_requires_non_empty_gallica_url_and_ark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def fake_search(query: str, *, max_results: int = 20):
        assert query == "guerre d'Algerie"
        assert max_results == 5
        return [
            SearchResult(
                url="https://gallica.bnf.fr/ark:/12148/bpt6k1234567",
                title="La guerre d'Algerie dans la presse francaise",
                snippet="Date: 1956 | Language: fre",
                source_kind="gallica_search",
                extras={
                    "ark": "ark:/12148/bpt6k1234567",
                    "dc:date": "1956",
                    "dc:language": "fre",
                },
            )
        ]

    monkeypatch.setattr(gallica, "search", fake_search)

    out = TOOL_REGISTRY["gallica_search"]("guerre d'Algerie")

    assert "gallica_search: returned 1 hits" in out
    assert "La guerre d'Algerie" in out
    assert "https://gallica.bnf.fr/ark:/12148/bpt6k1234567" in out
    assert "ark:/12148/bpt6k1234567" in out
    assert "language: fre" in out


def test_registered_kind_links_gallica_skill() -> None:
    from research_agent.tools._registry import get_kind

    entry = get_kind("gallica_search")
    assert entry is not None
    assert entry.skill_name == "gallica"
    assert entry.fetch_fn is gallica.fetch
    assert "maximumRecords capped at 50" in entry.optional_payload_knobs


def test_doctor_registry_skill_coherence_passes_for_gallica() -> None:
    from research_agent import doctor

    rows = doctor.check_registry_skill_coherence()
    gallica_rows = [row for row in rows if row.name == "registry_skill:gallica_search"]

    assert len(gallica_rows) == 1
    assert gallica_rows[0].status == "ok"


def test_gallica_skill_covers_required_operator_topics() -> None:
    from research_agent.skills import load_skill

    body = load_skill("connectors", "gallica")

    for needle in (
        "SRU returns XML, not JSON",
        "only XML-response connector",
        "xml.etree.ElementTree",
        'gallica all "<keywords>"',
        'dc.creator any "<author>"',
        'dc.date >= "1956"',
        "gallica.bnf.fr/services/engine/search/sru",
        "not `gallica.bnf.fr/SRU",
        "maximumRecords",
        "capped at 50",
        'metadata["ark"]',
        "multilingual-source-handling",
        "No auth",
    ):
        assert needle in body
