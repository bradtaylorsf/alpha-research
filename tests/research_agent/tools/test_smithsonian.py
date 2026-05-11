"""Tests for ``research_agent.tools.smithsonian`` (issue #227)."""

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
from research_agent.tools import smithsonian
from research_agent.tools.models import SearchResult, Source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "smithsonian"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    config.reset_for_tests()
    smithsonian.reset_for_tests()
    monkeypatch.setenv("DATA_GOV_API_KEY", "si-test-key")
    monkeypatch.setenv("RESEARCH_USER_AGENT", "alpha-research tests")
    monkeypatch.setattr(smithsonian.asyncio, "sleep", AsyncMock())
    yield
    smithsonian.reset_for_tests()
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

    monkeypatch.setattr(smithsonian.httpx, "AsyncClient", _client_factory)
    return captured


async def test_search_happy_path_populates_required_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _fixture("search_apollo11.json")

    def _respond(url, params):
        assert url == "https://api.si.edu/openaccess/api/v1.0/search"
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await smithsonian.search("Apollo 11", max_results=2)

    assert len(results) == 2
    first = results[0]
    assert first.source_kind == "si_search"
    assert first.title == "Command Module, Apollo 11"
    assert first.url == "https://www.si.edu/object/nasm_A19700102000"
    assert first.published_at == datetime(1969, 1, 1, tzinfo=UTC)
    assert "National Air and Space Museum" in first.snippet
    assert "Neil Armstrong" in first.snippet
    assert first.extras["unit_code"] == "NASM"
    assert first.extras["object_type"] == "SPACECRAFT-Crewed"
    assert first.extras["image_url"].startswith("https://ids.si.edu/")
    assert first.extras["license"] == "CC0"
    assert first.extras["smithsonian_id"] == "edanmdm:nasm_A19700102000"

    second = results[1]
    assert second.extras["object_type"] == "EQUIPMENT-Photographic"
    assert second.extras["license"] == "Usage conditions apply"

    headers = captured["headers"][0]
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"] == "alpha-research tests"
    params = captured["params"][0]
    assert params == {"api_key": "si-test-key", "q": "Apollo 11", "rows": 2}


async def test_search_empty_payload_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(url, params):
        return 200, _fixture("search_empty.json")

    captured = _patch_httpx(monkeypatch, responder=_respond)

    assert await smithsonian.search("no such object") == []
    assert captured["urls"] == ["https://api.si.edu/openaccess/api/v1.0/search"]


async def test_search_falls_back_to_demo_key_when_data_gov_key_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("DATA_GOV_API_KEY", raising=False)

    def _respond(url, params):
        return 200, _fixture("search_empty.json")

    captured = _patch_httpx(monkeypatch, responder=_respond)

    with caplog.at_level("WARNING", logger="research_agent.tools.smithsonian"):
        assert await smithsonian.search("Apollo 11") == []

    assert captured["params"][0]["api_key"] == "DEMO_KEY"
    assert "falling back to DEMO_KEY" in caplog.text


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
    monkeypatch.setattr(smithsonian.time, "monotonic", _monotonic)
    monkeypatch.setattr(smithsonian.asyncio, "sleep", _sleep)

    await asyncio.gather(
        smithsonian.search("first", max_results=1),
        smithsonian.search("second", max_results=1),
    )

    assert sleep_calls == pytest.approx([1.0])


@pytest.mark.parametrize("status", [400, 403, 404, 429])
async def test_search_returns_empty_on_4xx(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    def _respond(url, params):
        return status, {"error": "bad request"}

    _patch_httpx(monkeypatch, responder=_respond)

    assert await smithsonian.search("Apollo 11") == []


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_search_returns_empty_on_5xx(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    def _respond(url, params):
        return status, {"error": "unavailable"}

    _patch_httpx(monkeypatch, responder=_respond)

    assert await smithsonian.search("Apollo 11") == []


async def test_search_returns_empty_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_args, **_kwargs):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(smithsonian.httpx, "AsyncClient", _client_factory)

    assert await smithsonian.search("Apollo 11") == []


async def test_fetch_object_url_populates_required_source_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(url, params):
        assert url == (
            "https://api.si.edu/openaccess/api/v1.0/content/"
            "edanmdm:nasm_A19700102000"
        )
        return 200, _fixture("content_apollo11.json")

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await smithsonian.fetch("https://www.si.edu/object/nasm_A19700102000")

    assert source is not None
    assert source.source_kind == "si_search"
    assert source.url == "https://www.si.edu/object/nasm_A19700102000"
    assert source.title == "Command Module, Apollo 11"
    assert source.metadata["unit_code"] == "NASM"
    assert source.metadata["object_type"] == "SPACECRAFT-Crewed"
    assert source.metadata["image_url"].startswith("https://ids.si.edu/")
    assert source.metadata["license"] == "CC0"
    assert "## Summary" in source.cleaned_text
    assert "Neil Armstrong" in source.cleaned_text
    assert captured["params"] == [{"api_key": "si-test-key"}]


async def test_fetch_accepts_api_content_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def _respond(url, params):
        return 200, _fixture("content_apollo11.json")

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await smithsonian.fetch(
        "https://api.si.edu/openaccess/api/v1.0/content/"
        "edanmdm:nasm_A19700102000?api_key=ignored"
    )

    assert source is not None
    assert source.metadata["record_id"] == "nasm_A19700102000"
    assert captured["urls"] == [
        "https://api.si.edu/openaccess/api/v1.0/content/"
        "edanmdm:nasm_A19700102000"
    ]


async def test_fetch_rejects_lookalike_host() -> None:
    assert await smithsonian.fetch("https://www.si.edu.evil/object/nasm_A1") is None


def test_source_kind_accepts_si_search() -> None:
    result = SearchResult(
        url="https://www.si.edu/object/nasm_A19700102000",
        title="Command Module",
        snippet="Apollo",
        source_kind="si_search",
    )
    assert result.source_kind == "si_search"

    source = Source(
        url="https://www.si.edu/object/nasm_A19700102000",
        title="Command Module",
        cleaned_text="body",
        fetched_at=datetime.now(UTC),
        source_kind="si_search",
    )
    assert source.source_kind == "si_search"


def test_module_declares_kind_constant() -> None:
    assert smithsonian.KIND == "si_search"


def test_smoke_registry_includes_si_search() -> None:
    from research_agent.tools import TOOL_REGISTRY

    assert "si_search" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["si_search"])


def test_smoke_wrapper_formats_non_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def _fake_search(query: str, *, max_results: int = 20):
        assert query == "Apollo 11"
        assert max_results == 5
        return [
            SearchResult(
                url="https://www.si.edu/object/nasm_A19700102000",
                title="Command Module, Apollo 11",
                snippet="Apollo 11 command module.",
                source_kind="si_search",
                extras={
                    "unit_code": "NASM",
                    "object_type": "SPACECRAFT-Crewed",
                    "license": "CC0",
                    "image_url": "https://ids.si.edu/ids/deliveryService?id=x",
                },
            )
        ]

    monkeypatch.setattr(smithsonian, "search", _fake_search)

    out = TOOL_REGISTRY["si_search"]("Apollo 11")

    assert "si_search: returned 1 hits" in out
    assert "Command Module, Apollo 11" in out
    assert "unit_code: NASM" in out
    assert "license: CC0" in out


def test_smoke_wrapper_fails_on_empty_results(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def _fake_search(query: str, *, max_results: int = 20):
        return []

    monkeypatch.setattr(smithsonian, "search", _fake_search)

    with pytest.raises(SystemExit) as exc:
        TOOL_REGISTRY["si_search"]("Apollo 11")

    assert exc.value.code == 1
    assert "returned 0 results" in capsys.readouterr().err


def test_registry_entry_links_skill() -> None:
    from research_agent.tools._registry import get_kind

    entry = get_kind("si_search")

    assert entry is not None
    assert entry.skill_name == "smithsonian"
    assert entry.module_name == "smithsonian"
    assert entry.host_patterns == ("api.si.edu", "si.edu", "www.si.edu", "3d.si.edu")


def test_doctor_registry_skill_coherence_passes_for_smithsonian() -> None:
    from research_agent.doctor import check_registry_skill_coherence

    rows = check_registry_skill_coherence()
    smithsonian_rows = [row for row in rows if row.name == "registry_skill:si_search"]

    assert len(smithsonian_rows) == 1
    assert smithsonian_rows[0].status == "ok"


def test_smithsonian_skill_covers_required_operator_topics() -> None:
    from research_agent.skills.loader import load_skill

    body = load_skill("connectors", "smithsonian")

    for token in (
        "DATA_GOV_API_KEY",
        "DEMO_KEY",
        "NMAH",
        "NASM",
        "FSG",
        "metadata.license",
        "CC0",
        "CC-BY-NC",
        "Restricted",
        "non-object",
        "research papers",
        "exhibition",
    ):
        assert token in body
