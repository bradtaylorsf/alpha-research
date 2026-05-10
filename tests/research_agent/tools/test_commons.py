"""Tests for ``research_agent.tools.commons`` (issue #233)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import commons
from research_agent.tools.models import SearchResult, Source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "commons"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    commons.reset_for_tests()
    monkeypatch.setenv("RESEARCH_USER_AGENT", "operator@example.test")
    monkeypatch.setattr(commons._mediawiki.asyncio, "sleep", AsyncMock())
    yield
    commons.reset_for_tests()


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

    monkeypatch.setattr(commons._mediawiki.httpx, "AsyncClient", _client_factory)
    return captured


async def test_search_happy_path_enriches_license_metadata(
    monkeypatch: pytest.MonkeyPatch,
):
    search_payload = _fixture("search_algerian_war.json")
    imageinfo_payload = _fixture("imageinfo_algerian_war.json")

    def _respond(url, params):
        if params.get("list") == "search":
            return _FakeResp(200, search_payload)
        return _FakeResp(200, imageinfo_payload)

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await commons.search("Algerian war photographs", max_results=2)

    assert len(results) == 2
    first = results[0]
    assert first.source_kind == "commons_search"
    assert first.title == "File:Algerian war photograph 1957.jpg"
    assert first.url == (
        "https://commons.wikimedia.org/wiki/File:Algerian_war_photograph_1957.jpg"
    )
    assert "Algerian war" in first.snippet
    assert first.published_at == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert first.extras["mime_type"] == "image/jpeg"
    assert first.extras["original_url"].endswith("Algerian_war_photograph_1957.jpg")
    assert first.extras["thumb_url"].startswith("https://upload.wikimedia.org/")
    assert first.extras["author"] == "Archive photographer"
    assert first.extras["license"] == "cc-by-sa-4.0"
    assert first.extras["license_short"] == "CC BY-SA 4.0"
    assert first.extras["metadata"]["license"] == "cc-by-sa-4.0"

    assert captured["urls"] == [
        "https://commons.wikimedia.org/w/api.php",
        "https://commons.wikimedia.org/w/api.php",
    ]
    search_params = captured["params"][0]
    assert search_params["action"] == "query"
    assert search_params["list"] == "search"
    assert search_params["srsearch"] == "Algerian war photographs"
    assert search_params["srnamespace"] == "6"
    assert search_params["format"] == "json"
    imageinfo_params = captured["params"][1]
    assert imageinfo_params["prop"] == "imageinfo"
    assert imageinfo_params["iiprop"] == "url|mime|mediatype|extmetadata"
    assert "File:Algerian war photograph 1957.jpg" in imageinfo_params["titles"]
    headers = captured["headers"][0]
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"] == (
        "research-agent/0.1 "
        "(+https://github.com/bradtaylorsf/alpha-research; "
        "contact: operator@example.test)"
    )


async def test_search_populates_license_for_every_result(
    monkeypatch: pytest.MonkeyPatch,
):
    search_payload = _fixture("search_algerian_war.json")
    imageinfo_payload = _fixture("imageinfo_algerian_war.json")

    def _respond(url, params):
        return _FakeResp(
            200,
            search_payload if params.get("list") == "search" else imageinfo_payload,
        )

    _patch_httpx(monkeypatch, responder=_respond)

    results = await commons.search("Algerian war photographs", max_results=10)

    assert results
    assert all(hit.extras["metadata"]["license"] for hit in results)
    assert all(hit.extras["license"] for hit in results)


async def test_search_empty_payload_returns_empty(monkeypatch: pytest.MonkeyPatch):
    payload = _fixture("search_empty.json")

    def _respond(url, params):
        return _FakeResp(200, payload)

    captured = _patch_httpx(monkeypatch, responder=_respond)

    assert await commons.search("no such media", max_results=5) == []
    assert len(captured["params"]) == 1


async def test_search_returns_empty_on_transport_error(monkeypatch: pytest.MonkeyPatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_args, **_kwargs):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(commons._mediawiki.httpx, "AsyncClient", _client_factory)

    assert await commons.search("Algerian war photographs") == []


async def test_search_returns_empty_on_4xx(monkeypatch: pytest.MonkeyPatch):
    def _respond(url, params):
        return _FakeResp(404, {"error": "not found"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await commons.search("Algerian war photographs") == []


async def test_search_returns_empty_on_5xx(monkeypatch: pytest.MonkeyPatch):
    def _respond(url, params):
        return _FakeResp(503, {"error": "unavailable"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await commons.search("Algerian war photographs") == []


async def test_fetch_returns_none_on_4xx(monkeypatch: pytest.MonkeyPatch):
    def _respond(url, params):
        return _FakeResp(404, {"error": "not found"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await commons.fetch(
        "https://commons.wikimedia.org/wiki/File:Algerian_war_photograph_1957.jpg"
    ) is None


async def test_fetch_returns_none_on_5xx(monkeypatch: pytest.MonkeyPatch):
    def _respond(url, params):
        return _FakeResp(503, {"error": "unavailable"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await commons.fetch(
        "https://commons.wikimedia.org/wiki/File:Algerian_war_photograph_1957.jpg"
    ) is None


async def test_search_returns_empty_on_malformed_json(monkeypatch: pytest.MonkeyPatch):
    def _respond(url, params):
        return _FakeResp(200, ValueError("bad json"))

    _patch_httpx(monkeypatch, responder=_respond)

    assert await commons.search("Algerian war photographs") == []


async def test_fetch_file_page_populates_source_metadata(
    monkeypatch: pytest.MonkeyPatch,
):
    imageinfo_payload = _fixture("imageinfo_algerian_war.json")

    def _respond(url, params):
        assert params["titles"] == "File:Algerian war photograph 1957.jpg"
        return _FakeResp(200, imageinfo_payload)

    _patch_httpx(monkeypatch, responder=_respond)

    source = await commons.fetch(
        "https://commons.wikimedia.org/wiki/File:Algerian_war_photograph_1957.jpg"
    )

    assert source is not None
    assert source.source_kind == "commons_search"
    assert source.url == (
        "https://commons.wikimedia.org/wiki/File:Algerian_war_photograph_1957.jpg"
    )
    assert source.title == "File:Algerian war photograph 1957.jpg"
    assert "## Rights and reuse" in source.cleaned_text
    assert "cc-by-sa-4.0" in source.cleaned_text
    assert source.metadata["license"] == "cc-by-sa-4.0"
    assert source.metadata["license_short"] == "CC BY-SA 4.0"
    assert source.metadata["mime_type"] == "image/jpeg"
    assert source.metadata["original_url"].endswith("Algerian_war_photograph_1957.jpg")
    assert source.metadata["thumb_url"].startswith("https://upload.wikimedia.org/")
    assert source.metadata["author"] == "Archive photographer"


async def test_fetch_upload_thumbnail_url_normalizes_to_file_title(
    monkeypatch: pytest.MonkeyPatch,
):
    imageinfo_payload = _fixture("imageinfo_algerian_war.json")

    def _respond(url, params):
        assert params["titles"] == "File:Algerian war photograph 1957.jpg"
        return _FakeResp(200, imageinfo_payload)

    _patch_httpx(monkeypatch, responder=_respond)

    source = await commons.fetch(
        "https://upload.wikimedia.org/wikipedia/commons/thumb/a/aa/"
        "Algerian_war_photograph_1957.jpg/640px-Algerian_war_photograph_1957.jpg"
    )

    assert source is not None
    assert source.metadata["license"] == "cc-by-sa-4.0"


async def test_shared_wikimedia_rate_limit_waits_between_requests(
    monkeypatch: pytest.MonkeyPatch,
):
    search_payload = _fixture("search_algerian_war.json")
    imageinfo_payload = _fixture("imageinfo_algerian_war.json")
    clock = [100.0]
    sleep_calls: list[float] = []

    def fake_monotonic() -> float:
        return clock[0]

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(commons._mediawiki.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(commons._mediawiki.asyncio, "sleep", fake_sleep)

    def _respond(url, params):
        if params.get("list") == "search":
            return _FakeResp(200, search_payload)
        return _FakeResp(200, imageinfo_payload)

    _patch_httpx(monkeypatch, responder=_respond)

    await commons.search("Algerian war photographs", max_results=2)

    assert sleep_calls == [1.0]


async def test_rate_limit_is_shared_across_wikimedia_subdomains(
    monkeypatch: pytest.MonkeyPatch,
):
    clock = [200.0]
    sleep_calls: list[float] = []

    def fake_monotonic() -> float:
        return clock[0]

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(commons._mediawiki.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(commons._mediawiki.asyncio, "sleep", fake_sleep)

    await commons._mediawiki.rate_limit("https://commons.wikimedia.org/w/api.php")
    await commons._mediawiki.rate_limit(
        "https://upload.wikimedia.org/wikipedia/commons/a/aa/file.jpg"
    )

    assert sleep_calls == [1.0]


def test_smoke_registry_includes_commons_search() -> None:
    from research_agent.tools import TOOL_REGISTRY

    assert "commons_search" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["commons_search"])


def test_smoke_wrapper_requires_license_metadata(monkeypatch: pytest.MonkeyPatch):
    from research_agent.tools import TOOL_REGISTRY

    async def fake_search(query: str, *, max_results: int = 20):
        return [
            SearchResult(
                url="https://commons.wikimedia.org/wiki/File:Example.jpg",
                title="File:Example.jpg",
                snippet="Example",
                source_kind="commons_search",
                extras={
                    "license": "cc0",
                    "license_short": "CC0",
                    "mime_type": "image/jpeg",
                },
            )
        ]

    async def fake_fetch(url: str):
        return Source(
            url=url,
            title="File:Example.jpg",
            cleaned_text="body",
            fetched_at=datetime.now(UTC),
            source_kind="commons_search",
            metadata={"license": "cc0"},
        )

    monkeypatch.setattr(commons, "search", fake_search)
    monkeypatch.setattr(commons, "fetch", fake_fetch)

    out = TOOL_REGISTRY["commons_search"]("Algerian war photographs")

    assert "commons_search: returned 1 license-bearing hits" in out
    assert "fetched metadata.license: cc0" in out


def test_registered_kind_links_commons_skill() -> None:
    from research_agent.tools._registry import get_kind

    entry = get_kind("commons_search")
    assert entry is not None
    assert entry.skill_name == "commons"
    assert entry.fetch_fn is commons.fetch
