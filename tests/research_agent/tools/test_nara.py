"""Tests for ``research_agent.tools.nara`` (issue #226)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent import config
from research_agent.tools import nara
from research_agent.tools.models import SearchResult, Source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "nara"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    config.reset_for_tests()
    nara.reset_for_tests()
    monkeypatch.setenv("NARA_API_KEY", "nara-test-key")
    monkeypatch.setattr(nara.asyncio, "sleep", AsyncMock())
    yield
    nara.reset_for_tests()
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

    monkeypatch.setattr(nara.httpx, "AsyncClient", _client_factory)
    return captured


async def test_search_happy_path_populates_archival_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _fixture("search-vietnam-declassified.json")

    def _respond(url, params):
        assert url == "https://catalog.archives.gov/api/v2/records/search"
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await nara.search(
        "Vietnam War declassified",
        max_results=2,
        available_online=True,
        type_of_materials="Textual Records",
        record_group=59,
    )

    assert len(results) == 2
    first = results[0]
    assert first.source_kind == "nara_search"
    assert first.title == "Vietnam War declassified diplomatic cable, 1968"
    assert first.url == "https://catalog.archives.gov/id/123456"
    assert first.published_at is not None
    assert first.extras["nara_record_id"] == "123456"
    assert first.extras["record_group"] == (
        "RG 59: General Records of the Department of State"
    )
    assert first.extras["series_title"] == "Central Foreign Policy Files, 1967-1969"
    assert "Declassified Department of State cable" in first.extras["scope_and_content"]
    assert first.extras["access_restriction"] == "Unrestricted"
    assert first.extras["digital_objects"][0]["url"].endswith(
        "state-vietnam-1968.pdf"
    )

    second = results[1]
    assert second.extras["record_group"] == (
        "RG 263: Records of the Central Intelligence Agency"
    )
    assert second.extras["access_restriction"] == "Restricted"
    assert "Access: Restricted" in second.snippet

    headers = captured["headers"][0]
    assert headers["x-api-key"] == "nara-test-key"
    assert headers["Accept"] == "application/json"
    params = captured["params"][0]
    assert params["q"] == "Vietnam War declassified"
    assert params["rows"] == 2
    assert params["availableOnline"] == "true"
    assert params["typeOfMaterials"] == "Textual Records"
    assert params["recordGroupNumber"] == 59
    assert "api_key" not in params


async def test_search_empty_payload_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(url, params):
        return 200, {"body": {"hits": {"hits": []}}}

    _patch_httpx(monkeypatch, responder=_respond)

    assert await nara.search("no hits") == []


async def test_search_missing_key_logs_skip_without_http(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("NARA_API_KEY", raising=False)

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        raise AssertionError("httpx must not be called when NARA_API_KEY is missing")
        yield

    monkeypatch.setattr(nara.httpx, "AsyncClient", _client_factory)

    with caplog.at_level("WARNING", logger="research_agent.tools.nara"):
        results = await nara.search("Vietnam War declassified")

    assert results == []
    assert "would need NARA_API_KEY; skipping" in caplog.text


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

    monkeypatch.setattr(nara.time, "monotonic", _monotonic)
    monkeypatch.setattr(nara.asyncio, "sleep", _sleep)

    def _respond(url, params):
        return 200, {"body": {"hits": {"hits": []}}}

    _patch_httpx(monkeypatch, responder=_respond)

    await asyncio.gather(nara.search("a"), nara.search("b"))

    assert sleep_calls == pytest.approx([2.0])


async def test_search_returns_empty_on_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    def _respond(url, params):
        return 403, {}

    _patch_httpx(monkeypatch, responder=_respond)

    assert await nara.search("anything") == []


async def test_search_returns_empty_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    def _respond(url, params):
        return 429, {}

    _patch_httpx(monkeypatch, responder=_respond)

    assert await nara.search("anything") == []


async def test_search_returns_empty_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    def _respond(url, params):
        return 503, {}

    _patch_httpx(monkeypatch, responder=_respond)

    assert await nara.search("anything") == []


async def test_search_returns_empty_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(nara.httpx, "AsyncClient", _client_factory)

    assert await nara.search("anything") == []


async def test_fetch_detail_url_populates_required_metadata_and_archives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _fixture("search-vietnam-declassified.json")
    archived: list[str] = []

    def _respond(url, params):
        assert params["naIds"] == "123456"
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)
    monkeypatch.setattr(nara, "_spawn_wayback_save", lambda url: archived.append(url))

    source = await nara.fetch("https://catalog.archives.gov/id/123456")

    assert source is not None
    assert source.source_kind == "nara_search"
    assert source.url == "https://catalog.archives.gov/id/123456"
    assert source.metadata["nara_record_id"] == "123456"
    assert source.metadata["record_group"] == (
        "RG 59: General Records of the Department of State"
    )
    assert source.metadata["series_title"] == "Central Foreign Policy Files, 1967-1969"
    assert "Declassified Department of State cable" in source.metadata[
        "scope_and_content"
    ]
    assert "## Scope and Content" in source.cleaned_text
    assert "## Digital Objects" in source.cleaned_text
    assert archived == ["https://catalog.archives.gov/id/123456"]
    assert captured["params"] == [{"naIds": "123456", "rows": 1}]


async def test_fetch_accepts_api_search_url_without_wayback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _fixture("search-vietnam-declassified.json")
    archived: list[str] = []

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)
    monkeypatch.setattr(nara, "_spawn_wayback_save", lambda url: archived.append(url))

    source = await nara.fetch(
        "https://catalog.archives.gov/api/v2/records/search?naIds=123456"
    )

    assert source is not None
    assert source.metadata["nara_record_id"] == "123456"
    assert archived == []


async def test_fetch_missing_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NARA_API_KEY", raising=False)

    assert await nara.fetch("https://catalog.archives.gov/id/123456") is None


async def test_fetch_rejects_lookalike_host() -> None:
    assert await nara.fetch("https://catalog.archives.gov.evil/id/123456") is None


def test_source_kind_accepts_nara_search() -> None:
    result = SearchResult(
        url="https://catalog.archives.gov/id/1",
        title="t",
        snippet="s",
        source_kind="nara_search",
    )
    assert result.source_kind == "nara_search"

    source = Source(
        url="https://catalog.archives.gov/id/1",
        title="t",
        cleaned_text="body",
        fetched_at=nara.datetime.now(nara.UTC),
        source_kind="nara_search",
    )
    assert source.source_kind == "nara_search"


def test_module_declares_kind_constant() -> None:
    assert nara.KIND == "nara_search"


def test_smoke_registry_includes_nara_search() -> None:
    from research_agent.tools import TOOL_REGISTRY

    assert "nara_search" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["nara_search"])


def test_smoke_wrapper_skips_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from research_agent.tools import TOOL_REGISTRY

    monkeypatch.delenv("NARA_API_KEY", raising=False)

    async def _boom(*_a, **_k):
        raise AssertionError("nara.search must not run when key is missing")

    monkeypatch.setattr(nara, "search", _boom)

    out = TOOL_REGISTRY["nara_search"]("Vietnam War declassified")

    assert "would need NARA_API_KEY" in out
    assert "live test skipped" in out


def test_smoke_wrapper_formats_non_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def _fake_search(query: str, *, max_results: int = 20):
        assert query == "Vietnam War declassified"
        assert max_results == 5
        return [
            SearchResult(
                url="https://catalog.archives.gov/id/123456",
                title="Vietnam War declassified diplomatic cable, 1968",
                snippet="Declassified Department of State cable.",
                source_kind="nara_search",
                extras={
                    "nara_record_id": "123456",
                    "record_group": "RG 59",
                    "series_title": "Central Foreign Policy Files",
                    "access_restriction": "Unrestricted",
                },
            )
        ]

    monkeypatch.setattr(nara, "search", _fake_search)

    out = TOOL_REGISTRY["nara_search"]("Vietnam War declassified")

    assert "nara_search: returned 1 hits" in out
    assert "Vietnam War declassified diplomatic cable" in out
    assert "NAID 123456" in out
    assert "RG 59" in out


def test_nara_skill_covers_required_operator_warnings() -> None:
    from research_agent.skills import load_skill

    body = load_skill("connectors", "nara")

    for needle in (
        "NARA_API_KEY",
        "Catalog_API@nara.gov",
        "24h",
        "10,000 queries/month",
        "0.5 RPS",
        "RG 59",
        "RG 263",
        "Restricted",
        "FOIA-only",
        "post-2010",
    ):
        assert needle in body


def test_doctor_registry_skill_coherence_passes_for_nara() -> None:
    from research_agent import doctor

    rows = doctor.check_registry_skill_coherence()
    nara_rows = [row for row in rows if row.name == "registry_skill:nara_search"]
    assert len(nara_rows) == 1
    assert nara_rows[0].status == "ok", nara_rows[0].detail
