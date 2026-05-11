"""Tests for ``research_agent.tools.hathitrust`` (issue #235)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import hathitrust
from research_agent.tools.models import Source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "hathitrust"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    hathitrust.reset_for_tests()
    monkeypatch.setenv("RESEARCH_USER_AGENT", "operator@example.test")
    monkeypatch.setattr(hathitrust.asyncio, "sleep", AsyncMock())
    yield
    hathitrust.reset_for_tests()


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
    }

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        captured["headers"].append(kwargs.get("headers"))

        class _Client:
            async def get(self, url, **_kwargs):
                captured["urls"].append(url)
                return responder(url)

        yield _Client()

    monkeypatch.setattr(hathitrust.httpx, "AsyncClient", _client_factory)
    return captured


def test_connector_is_fetch_only_not_planner_kind() -> None:
    assert not hasattr(hathitrust, "KIND")


@pytest.mark.parametrize(
    ("kwargs", "expected_suffix"),
    [
        ({"isbn": "0030110408"}, "/isbn/0030110408.json"),
        ({"oclc": "424023"}, "/oclc/424023.json"),
        ({"lccn": "62/9520"}, "/lccn/62%2F9520.json"),
        ({"htid": "mdp.39015025315527"}, "/htid/mdp.39015025315527.json"),
    ],
)
async def test_fetch_by_identifier_happy_path_by_id_type(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, str],
    expected_suffix: str,
) -> None:
    payload = _fixture("lookup-oclc-424023.json")

    def _respond(_url):
        return _FakeResp(200, payload)

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await hathitrust.fetch_by_identifier(**kwargs)

    assert source is not None
    assert captured["urls"] == [f"{hathitrust._BASE_URL}{expected_suffix}"]
    assert captured["headers"][0]["Accept"] == "application/json"
    assert captured["headers"][0]["User-Agent"] == "operator@example.test"

    assert source.source_kind == "hathitrust"
    assert source.url == "https://catalog.hathitrust.org/Record/000578050"
    assert source.title == "Infinite series."
    assert "Full text available: yes" in source.cleaned_text
    assert "mdp.39015025315527 - pd - Full View" in source.cleaned_text
    assert source.metadata["hathi_record_id"] == "000578050"
    assert source.metadata["rights"] == "pd"
    assert source.metadata["full_text_available"] is True
    assert source.metadata["hathi_permalink"] == (
        "https://hdl.handle.net/2027/mdp.39015025315527"
    )
    assert source.metadata["identifiers"]["oclcs"] == ["00424023"]
    assert len(source.metadata["volumes"]) == 2
    assert source.metadata["volumes"][0]["rights"] == "ic"
    assert source.metadata["volumes"][1]["full_text_available"] is True


async def test_fetch_by_identifier_returns_none_without_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(_url):
        raise AssertionError("no identifier must not make an HTTP request")

    captured = _patch_httpx(monkeypatch, responder=_respond)

    assert await hathitrust.fetch_by_identifier() is None
    assert captured["urls"] == []


async def test_fetch_by_identifier_returns_none_for_record_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(_url):
        return _FakeResp(200, {"records": {}, "items": []})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await hathitrust.fetch_by_identifier(oclc="missing") is None


@pytest.mark.parametrize("status", [404, 429, 503])
async def test_fetch_by_identifier_returns_none_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    def _respond(_url):
        return _FakeResp(status, {"error": "nope"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await hathitrust.fetch_by_identifier(oclc="424023") is None


async def test_fetch_by_identifier_returns_none_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_args, **_kwargs):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(hathitrust.httpx, "AsyncClient", _client_factory)

    assert await hathitrust.fetch_by_identifier(oclc="424023") is None


async def test_fetch_by_identifier_returns_none_on_non_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(_url):
        return _FakeResp(200, ValueError("not json"))

    _patch_httpx(monkeypatch, responder=_respond)

    assert await hathitrust.fetch_by_identifier(oclc="424023") is None


async def test_rate_limit_gate_sleeps_within_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _fixture("lookup-oclc-424023.json")
    clock = [100.0]
    sleep_calls: list[float] = []

    def _monotonic() -> float:
        return clock[0]

    async def _sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    def _respond(_url):
        return _FakeResp(200, payload)

    _patch_httpx(monkeypatch, responder=_respond)
    monkeypatch.setattr(hathitrust.time, "monotonic", _monotonic)
    monkeypatch.setattr(hathitrust.asyncio, "sleep", _sleep)

    assert await hathitrust.fetch_by_identifier(oclc="1") is not None
    clock[0] += 0.25
    assert await hathitrust.fetch_by_identifier(oclc="2") is not None

    assert sleep_calls == pytest.approx([0.75])


@pytest.mark.parametrize(
    ("url", "expected_suffix"),
    [
        (
            "https://catalog.hathitrust.org/Record/000578050",
            "/recordnumber/000578050.json",
        ),
        (
            "https://catalog.hathitrust.org/api/volumes/brief/oclc/424023.json",
            "/oclc/424023.json",
        ),
        (
            "https://catalog.hathitrust.org/cgi/pt?id=mdp.39015025315527",
            "/htid/mdp.39015025315527.json",
        ),
        (
            "https://hdl.handle.net/2027/mdp.39015025315527",
            "/htid/mdp.39015025315527.json",
        ),
    ],
)
async def test_fetch_classifies_hathitrust_urls(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    expected_suffix: str,
) -> None:
    payload = _fixture("lookup-oclc-424023.json")

    def _respond(_url):
        return _FakeResp(200, payload)

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await hathitrust.fetch(url)

    assert source is not None
    assert captured["urls"] == [f"{hathitrust._BASE_URL}{expected_suffix}"]


async def test_fetch_rejects_non_hathitrust_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _respond(_url):
        raise AssertionError("foreign host must not make an HTTP request")

    captured = _patch_httpx(monkeypatch, responder=_respond)

    assert await hathitrust.fetch("https://example.com/Record/000578050") is None
    assert captured["urls"] == []


async def test_enrich_source_from_openlibrary_identifier_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture("openlibrary-fetch-oclc-424023.json")
    source = Source(
        url=fixture["url"],
        title=fixture["title"],
        cleaned_text=fixture["cleaned_text"],
        fetched_at=datetime.now(UTC),
        source_kind="web",
        metadata=fixture["metadata"],
    )
    hathi_source = Source(
        url="https://catalog.hathitrust.org/Record/000578050",
        title="Infinite series.",
        cleaned_text="HathiTrust enrichment.",
        fetched_at=datetime.now(UTC),
        source_kind="hathitrust",
        metadata={
            "hathi_record_id": "000578050",
            "rights": "pd",
            "full_text_available": True,
            "volumes": [{"htid": "mdp.39015025315527", "rights": "pd"}],
            "hathi_permalink": "https://hdl.handle.net/2027/mdp.39015025315527",
            "identifiers": {"oclcs": ["00424023"]},
            "fetched_via": "hathitrust",
        },
    )
    captured: list[dict[str, str | None]] = []

    async def _fake_fetch_by_identifier(
        *,
        isbn: str | None = None,
        oclc: str | None = None,
        lccn: str | None = None,
        htid: str | None = None,
    ) -> Source | None:
        captured.append({"isbn": isbn, "oclc": oclc, "lccn": lccn, "htid": htid})
        return hathi_source

    monkeypatch.setattr(
        hathitrust,
        "fetch_by_identifier",
        _fake_fetch_by_identifier,
    )

    enriched = await hathitrust.enrich_source_from_identifiers(source)

    assert enriched is source
    assert captured == [{"isbn": None, "oclc": "424023", "lccn": None, "htid": None}]
    assert enriched.url == "https://openlibrary.org/works/OL123W/Infinite_series"
    assert enriched.source_kind == "web"
    assert enriched.metadata["hathi_record_id"] == "000578050"
    assert enriched.metadata["rights"] == "pd"
    assert enriched.metadata["full_text_available"] is True
    assert enriched.metadata["volumes"][0]["htid"] == "mdp.39015025315527"
    assert enriched.metadata["hathi_source_url"] == (
        "https://catalog.hathitrust.org/Record/000578050"
    )
