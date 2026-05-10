"""Tests for ``research_agent.tools.ukna`` (issue #231)."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent import config
from research_agent.tools import ukna
from research_agent.tools.models import SearchResult, Source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ukna"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    config.reset_for_tests()
    ukna.reset_for_tests()
    monkeypatch.setattr(ukna.asyncio, "sleep", AsyncMock())
    yield
    ukna.reset_for_tests()
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


def _camelize_key(key: str) -> str:
    if key == "URLParameters":
        return "urlParameters"
    return key[:1].lower() + key[1:]


def _camelize_payload(payload: dict) -> dict:
    out = {_camelize_key(key): value for key, value in payload.items()}
    out["records"] = [
        {_camelize_key(key): value for key, value in record.items()}
        for record in payload.get("Records", [])
    ]
    out.pop("Records", None)
    return out


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

    monkeypatch.setattr(ukna.httpx, "AsyncClient", _client_factory)
    return captured


async def test_search_happy_path_populates_catalogue_metadata(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _fixture("search_mau_mau.json")

    def _respond(url, params):
        assert url == "https://discovery.nationalarchives.gov.uk/API/search/v1/records"
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    with caplog.at_level(logging.WARNING, logger="research_agent.tools.ukna"):
        results = await ukna.search("Mau Mau Kenya", max_results=2, page=3)

    assert "schema drift" not in caplog.text
    assert len(results) == 2
    first = results[0]
    assert first.source_kind == "ukna_search"
    assert first.title == "Kenya: Mau Mau emergency"
    assert first.url == "https://discovery.nationalarchives.gov.uk/details/r/C1234567"
    assert first.score == 14.25
    assert first.extras["catalogue_reference"] == "CO 822/1234"
    assert first.extras["covering_dates"] == "1952-1959"
    assert first.extras["held_by"] == "The National Archives, Kew"
    assert "detention camps" in first.extras["scope_content"]
    assert first.extras["department"] == "CO"
    assert first.extras["catalogue_level"] == 6
    assert "Ref: CO 822/1234" in first.snippet

    headers = captured["headers"][0]
    assert headers["Accept"] == "application/json"
    assert "Authorization" not in headers
    params = captured["params"][0]
    assert params["sps.searchQuery"] == "Mau Mau Kenya"
    assert params["sps.resultsPageSize"] == 2
    assert params["sps.page"] == 3
    assert "api_key" not in params


async def test_search_accepts_current_lower_camel_api_shape(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = {
        "records": [
            {
                "altName": "",
                "places": ["Kenya"],
                "corpBodies": ["Colonial Office"],
                "taxonomies": ["Empire and Commonwealth"],
                "formerReferenceDep": "",
                "formerReferencePro": "",
                "heldBy": ["The National Archives, Kew"],
                "context": "Colonial Office and successors: East Africa Department",
                "content": "Mau Mau emergency files and detention camp reports.",
                "urlParameters": "C1234567",
                "department": "CO",
                "note": "",
                "adminHistory": "",
                "arrangement": "",
                "mapDesignation": "",
                "mapScale": "",
                "physicalCondition": "",
                "catalogueLevel": 6,
                "openingDate": "01 January 1987",
                "closureStatus": "Open Document, Open Description",
                "closureType": "",
                "closureCode": "",
                "documentType": "Textual record",
                "coveringDates": "1952-1959",
                "description": "Kenya: Mau Mau emergency and rehabilitation camps",
                "endDate": "1959",
                "numEndDate": 1959,
                "numStartDate": 1952,
                "startDate": "1952",
                "id": "1234567",
                "reference": "CO 822/1234",
                "score": 14.25,
                "source": "CAT",
                "title": "Kenya: Mau Mau emergency",
            }
        ],
        "taxonomySubjects": [],
        "timePeriods": [],
        "departments": [],
        "catalogueLevels": [],
        "closureStatuses": [],
        "sources": [],
        "repositories": [],
        "heldByReps": [],
        "referenceFirstLetters": [],
        "titleFirstLetters": [],
        "count": 1,
        "nextBatchMark": None,
    }

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    with caplog.at_level(logging.WARNING, logger="research_agent.tools.ukna"):
        results = await ukna.search("Mau Mau Kenya", max_results=5)

    assert "schema drift" not in caplog.text
    assert len(results) == 1
    first = results[0]
    assert first.url == "https://discovery.nationalarchives.gov.uk/details/r/C1234567"
    assert first.title == "Kenya: Mau Mau emergency"
    assert first.extras["catalogue_reference"] == "CO 822/1234"
    assert first.extras["covering_dates"] == "1952-1959"
    assert first.extras["held_by"] == "The National Archives, Kew"
    assert "detention camp reports" in first.extras["scope_content"]


async def test_search_empty_payload_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(url, params):
        return 200, _fixture("search_empty.json")

    _patch_httpx(monkeypatch, responder=_respond)

    assert await ukna.search("no hits") == []


async def test_search_accepts_live_camelcase_payload(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _camelize_payload(_fixture("search_mau_mau.json"))

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    with caplog.at_level(logging.WARNING, logger="research_agent.tools.ukna"):
        results = await ukna.search("Mau Mau Kenya", max_results=1)

    assert "schema drift" not in caplog.text
    assert len(results) == 1
    assert results[0].title == "Kenya: Mau Mau emergency"
    assert results[0].url == "https://discovery.nationalarchives.gov.uk/details/r/C1234567"
    assert results[0].extras["catalogue_reference"] == "CO 822/1234"
    assert results[0].extras["covering_dates"] == "1952-1959"
    assert results[0].extras["held_by"] == "The National Archives, Kew"


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

    monkeypatch.setattr(ukna.time, "monotonic", _monotonic)
    monkeypatch.setattr(ukna.asyncio, "sleep", _sleep)

    def _respond(url, params):
        return 200, _fixture("search_empty.json")

    _patch_httpx(monkeypatch, responder=_respond)

    await asyncio.gather(ukna.search("a"), ukna.search("b"))

    assert sleep_calls == pytest.approx([1.0])


async def test_search_returns_empty_on_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    def _respond(url, params):
        return 404, {}

    _patch_httpx(monkeypatch, responder=_respond)

    assert await ukna.search("anything") == []


async def test_search_returns_empty_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    def _respond(url, params):
        return 503, {}

    _patch_httpx(monkeypatch, responder=_respond)

    assert await ukna.search("anything") == []


async def test_search_returns_empty_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(ukna.httpx, "AsyncClient", _client_factory)

    assert await ukna.search("anything") == []


async def test_search_schema_drift_warning_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _respond(url, params):
        return 200, {"Items": [{"Name": "unexpected"}], "Total": 1}

    _patch_httpx(monkeypatch, responder=_respond)

    with caplog.at_level(logging.WARNING, logger="research_agent.tools.ukna"):
        results = await ukna.search("Mau Mau Kenya")

    assert results == []
    assert "ukna_search: beta API schema drift" in caplog.text
    assert "Records" in caplog.text


async def test_fetch_detail_url_populates_required_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _fixture("search_mau_mau.json")

    def _respond(url, params):
        assert params == {"sps.searchQuery": "C1234567", "sps.resultsPageSize": 10}
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await ukna.fetch(
        "https://discovery.nationalarchives.gov.uk/details/r/C1234567"
    )

    assert source is not None
    assert source.source_kind == "ukna_search"
    assert source.url == "https://discovery.nationalarchives.gov.uk/details/r/C1234567"
    assert source.metadata["catalogue_reference"] == "CO 822/1234"
    assert source.metadata["covering_dates"] == "1952-1959"
    assert source.metadata["held_by"] == "The National Archives, Kew"
    assert "detention camps" in source.metadata["scope_content"]
    assert "## Scope and Content" in source.cleaned_text
    assert "Catalogue reference: CO 822/1234" in source.cleaned_text
    assert captured["urls"] == [
        "https://discovery.nationalarchives.gov.uk/API/search/v1/records"
    ]


async def test_fetch_accepts_api_search_url(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _fixture("search_mau_mau.json")

    def _respond(url, params):
        assert params == {
            "sps.searchQuery": "CO 822/1234",
            "sps.resultsPageSize": 10,
        }
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    source = await ukna.fetch(
        "https://discovery.nationalarchives.gov.uk/API/search/v1/records"
        "?sps.searchQuery=CO%20822%2F1234"
    )

    assert source is not None
    assert source.metadata["catalogue_reference"] == "CO 822/1234"


async def test_fetch_rejects_lookalike_host() -> None:
    assert (
        await ukna.fetch("https://discovery.nationalarchives.gov.uk.evil/details/r/C1")
        is None
    )


def test_source_kind_accepts_ukna_search() -> None:
    result = SearchResult(
        url="https://discovery.nationalarchives.gov.uk/details/r/C1",
        title="t",
        snippet="s",
        source_kind="ukna_search",
    )
    assert result.source_kind == "ukna_search"

    source = Source(
        url="https://discovery.nationalarchives.gov.uk/details/r/C1",
        title="t",
        cleaned_text="body",
        fetched_at=ukna.datetime.now(ukna.UTC),
        source_kind="ukna_search",
    )
    assert source.source_kind == "ukna_search"


def test_module_declares_kind_constant() -> None:
    assert ukna.KIND == "ukna_search"


def test_smoke_registry_includes_ukna_search() -> None:
    from research_agent.tools import TOOL_REGISTRY

    assert "ukna_search" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["ukna_search"])


def test_smoke_wrapper_formats_non_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def _fake_search(query: str, *, max_results: int = 20):
        assert query == "Mau Mau Kenya"
        assert max_results == 5
        return [
            SearchResult(
                url="https://discovery.nationalarchives.gov.uk/details/r/C1234567",
                title="Kenya: Mau Mau emergency",
                snippet="Ref: CO 822/1234 | Dates: 1952-1959",
                source_kind="ukna_search",
                extras={
                    "catalogue_reference": "CO 822/1234",
                    "covering_dates": "1952-1959",
                    "held_by": "The National Archives, Kew",
                },
            )
        ]

    monkeypatch.setattr(ukna, "search", _fake_search)

    out = TOOL_REGISTRY["ukna_search"]("Mau Mau Kenya")

    assert "ukna_search: returned 1 hits" in out
    assert "Kenya: Mau Mau emergency" in out
    assert "CO 822/1234" in out
    assert "The National Archives, Kew" in out


def test_smoke_wrapper_fails_on_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def _fake_search(query: str, *, max_results: int = 20):
        return []

    monkeypatch.setattr(ukna, "search", _fake_search)

    with pytest.raises(SystemExit):
        TOOL_REGISTRY["ukna_search"]("Mau Mau Kenya")


def test_ukna_skill_covers_required_operator_warnings() -> None:
    from research_agent.skills import load_skill

    body = load_skill("connectors", "ukna")

    for needle in (
        "Beta API",
        "schema may drift",
        "CO 537",
        "CO",
        "WO",
        "FO",
        "covering_dates",
        "free-text",
        "NOT contents",
        "scanned-image PDFs",
        "No auth",
    ):
        assert needle in body


def test_doctor_registry_skill_coherence_passes_for_ukna() -> None:
    from research_agent import doctor

    rows = doctor.check_registry_skill_coherence()
    ukna_rows = [row for row in rows if row.name == "registry_skill:ukna_search"]
    assert len(ukna_rows) == 1
    assert ukna_rows[0].status == "ok", ukna_rows[0].detail
