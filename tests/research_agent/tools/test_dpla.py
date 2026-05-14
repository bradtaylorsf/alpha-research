"""Tests for ``research_agent.tools.dpla`` (issue #228)."""

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
from research_agent.tools import dpla
from research_agent.tools.models import SearchResult, Source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "dpla"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    config.reset_for_tests()
    dpla.reset_for_tests()
    monkeypatch.setenv("DPLA_API_KEY", "1234567890abcdef1234567890abcdef")
    monkeypatch.setenv("RESEARCH_USER_AGENT", "muckwire tests")
    monkeypatch.setattr(dpla.asyncio, "sleep", AsyncMock())
    yield
    dpla.reset_for_tests()
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

    monkeypatch.setattr(dpla.httpx, "AsyncClient", _client_factory)
    return captured


async def test_search_happy_path_populates_required_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _fixture("search_maya.json")

    def _respond(url, params):
        assert url == "https://api.dp.la/v2/items"
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await dpla.search(
        "Maya land claims",
        max_results=2,
        provider="New York Public Library",
    )

    assert len(results) == 2
    first = results[0]
    assert first.source_kind == "dpla_search"
    assert first.title == "Maya land claims in Chiapas"
    assert first.url == "https://dp.la/item/11112222333344445555666677778888"
    assert first.published_at == datetime(1978, 1, 1, tzinfo=UTC)
    assert first.score == 12.5
    assert "The New York Public Library" in first.snippet
    assert "Maya peoples" in first.snippet
    assert first.extras["dpla_id"] == "11112222333344445555666677778888"
    assert first.extras["provider"] == "New York Public Library"
    assert first.extras["data_provider"] == "The New York Public Library"
    assert first.extras["license"] == "No known copyright restrictions"
    assert first.extras["object_url"].startswith("https://images.nypl.org/")

    second = results[1]
    assert second.extras["provider"] == "HathiTrust"
    assert second.extras["data_provider"] == "University of Michigan"
    assert second.extras["license"] == "Public domain"
    assert second.extras["object_url"].startswith("https://babel.hathitrust.org/")

    headers = captured["headers"][0]
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"] == "muckwire tests"
    params = captured["params"][0]
    assert params == {
        "api_key": "1234567890abcdef1234567890abcdef",
        "page_size": 2,
        "provider": "New York Public Library",
        "q": "Maya land claims",
    }


async def test_search_empty_payload_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(url, params):
        return 200, _fixture("search_empty.json")

    captured = _patch_httpx(monkeypatch, responder=_respond)

    assert await dpla.search("no such object") == []
    assert captured["urls"] == ["https://api.dp.la/v2/items"]


async def test_search_skips_when_dpla_key_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("DPLA_API_KEY", raising=False)

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        raise AssertionError("DPLA HTTP client should not be constructed")
        yield

    monkeypatch.setattr(dpla.httpx, "AsyncClient", _client_factory)

    with caplog.at_level("WARNING", logger="research_agent.tools.dpla"):
        assert await dpla.search("Maya land claims") == []

    assert "would need DPLA_API_KEY" in caplog.text
    assert "curl -X POST https://api.dp.la/v2/api_key/<your-email>" in caplog.text


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
    monkeypatch.setattr(dpla.time, "monotonic", _monotonic)
    monkeypatch.setattr(dpla.asyncio, "sleep", _sleep)

    await asyncio.gather(
        dpla.search("first", max_results=1),
        dpla.search("second", max_results=1),
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

    assert await dpla.search("Maya land claims") == []


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_search_returns_empty_on_5xx(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    def _respond(url, params):
        return status, {"error": "unavailable"}

    _patch_httpx(monkeypatch, responder=_respond)

    assert await dpla.search("Maya land claims") == []


async def test_search_returns_empty_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_args, **_kwargs):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(dpla.httpx, "AsyncClient", _client_factory)

    assert await dpla.search("Maya land claims") == []


async def test_fetch_dpla_item_url_populates_required_source_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(url, params):
        assert url == (
            "https://api.dp.la/v2/items/11112222333344445555666677778888"
        )
        return 200, _fixture("item_maya.json")

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await dpla.fetch(
        "https://dp.la/item/11112222333344445555666677778888"
    )

    assert source is not None
    assert source.source_kind == "dpla_search"
    assert source.url == "https://dp.la/item/11112222333344445555666677778888"
    assert source.title == "Maya land claims in Chiapas"
    assert source.metadata["dpla_id"] == "11112222333344445555666677778888"
    assert source.metadata["provider"] == "New York Public Library"
    assert source.metadata["data_provider"] == "The New York Public Library"
    assert source.metadata["license"] == "No known copyright restrictions"
    assert source.metadata["object_url"].startswith("https://images.nypl.org/")
    assert "## Description" in source.cleaned_text
    assert "Maya peoples" in source.cleaned_text
    assert captured["params"] == [
        {"api_key": "1234567890abcdef1234567890abcdef"}
    ]


async def test_fetch_accepts_api_item_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def _respond(url, params):
        return 200, _fixture("item_maya.json")

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await dpla.fetch(
        "https://api.dp.la/v2/items/11112222333344445555666677778888"
        "?api_key=ignored"
    )

    assert source is not None
    assert source.metadata["dpla_id"] == "11112222333344445555666677778888"
    assert captured["urls"] == [
        "https://api.dp.la/v2/items/11112222333344445555666677778888"
    ]


async def test_fetch_rejects_lookalike_host() -> None:
    assert await dpla.fetch("https://dp.la.evil/item/111122223333") is None


def test_source_kind_accepts_dpla_search() -> None:
    result = SearchResult(
        url="https://dp.la/item/11112222333344445555666677778888",
        title="Maya land claims in Chiapas",
        snippet="DPLA item",
        source_kind="dpla_search",
    )
    assert result.source_kind == "dpla_search"

    source = Source(
        url="https://dp.la/item/11112222333344445555666677778888",
        title="Maya land claims in Chiapas",
        cleaned_text="body",
        fetched_at=datetime.now(UTC),
        source_kind="dpla_search",
    )
    assert source.source_kind == "dpla_search"


def test_module_declares_kind_constant() -> None:
    assert dpla.KIND == "dpla_search"


def test_smoke_registry_includes_dpla_search() -> None:
    from research_agent.tools import TOOL_REGISTRY

    assert "dpla_search" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["dpla_search"])


def test_smoke_wrapper_skips_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from research_agent.tools import TOOL_REGISTRY

    monkeypatch.delenv("DPLA_API_KEY", raising=False)

    out = TOOL_REGISTRY["dpla_search"]("Maya land claims")

    assert "would need DPLA_API_KEY" in out
    assert "curl -X POST https://api.dp.la/v2/api_key/<your-email>" in out
    assert "live test skipped" in out


def test_smoke_wrapper_formats_non_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def _fake_search(query: str, *, max_results: int = 20):
        assert query == "Maya land claims"
        assert max_results == 5
        return [
            SearchResult(
                url="https://dp.la/item/11112222333344445555666677778888",
                title="Maya land claims in Chiapas",
                snippet="Maya land claims metadata.",
                source_kind="dpla_search",
                extras={
                    "dpla_id": "11112222333344445555666677778888",
                    "provider": "New York Public Library",
                    "data_provider": "The New York Public Library",
                    "license": "No known copyright restrictions",
                    "object_url": "https://images.nypl.org/index.php?id=x",
                },
            )
        ]

    monkeypatch.setattr(dpla, "search", _fake_search)

    out = TOOL_REGISTRY["dpla_search"]("Maya land claims")

    assert "dpla_search: returned 1 hits" in out
    assert "Maya land claims in Chiapas" in out
    assert "provider: New York Public Library" in out
    assert "object_url: https://images.nypl.org/index.php?id=x" in out


def test_smoke_wrapper_fails_on_empty_results(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def _fake_search(query: str, *, max_results: int = 20):
        return []

    monkeypatch.setattr(dpla, "search", _fake_search)

    with pytest.raises(SystemExit) as exc:
        TOOL_REGISTRY["dpla_search"]("Maya land claims")

    assert exc.value.code == 1
    assert "returned 0 results" in capsys.readouterr().err


def test_registry_entry_links_skill() -> None:
    from research_agent.tools._registry import get_kind

    entry = get_kind("dpla_search")

    assert entry is not None
    assert entry.skill_name == "dpla"
    assert entry.module_name == "dpla"
    assert entry.host_patterns == ("api.dp.la", "dp.la", "www.dp.la")


def test_doctor_registry_skill_coherence_passes_for_dpla() -> None:
    from research_agent.doctor import check_registry_skill_coherence

    rows = check_registry_skill_coherence()
    dpla_rows = [row for row in rows if row.name == "registry_skill:dpla_search"]

    assert len(dpla_rows) == 1
    assert dpla_rows[0].status == "ok"


def test_dpla_skill_covers_required_operator_topics() -> None:
    from research_agent.skills.loader import load_skill

    body = load_skill("connectors", "dpla")

    for token in (
        "DPLA_API_KEY",
        "curl -X POST https://api.dp.la/v2/api_key/<your-email>",
        "instant",
        "provider",
        "dataProvider",
        "?provider=",
        "New York Public Library",
        "Chronicling America",
        "loc_search",
        "collection=chronicling-america",
    ):
        assert token in body
