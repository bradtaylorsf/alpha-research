"""Tests for ``research_agent.tools.wikisource`` (issue #234)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import wikisource
from research_agent.tools.models import SearchResult, Source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "wikisource"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    wikisource.reset_for_tests()
    monkeypatch.setenv("RESEARCH_USER_AGENT", "operator@example.test")
    monkeypatch.setattr(wikisource._mediawiki.asyncio, "sleep", AsyncMock())
    yield
    wikisource.reset_for_tests()


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

    monkeypatch.setattr(wikisource._mediawiki.httpx, "AsyncClient", _client_factory)
    return captured


async def test_search_happy_path_uses_mediawiki_search(
    monkeypatch: pytest.MonkeyPatch,
):
    search_payload = _fixture("search_treaty.json")

    def _respond(url, params):
        assert url == "https://en.wikisource.org/w/api.php"
        return _FakeResp(200, search_payload)

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await wikisource.search("Treaty of Versailles", max_results=2)

    assert len(results) == 2
    first = results[0]
    assert first.source_kind == "wikisource_search"
    assert first.title == "Treaty of Versailles"
    assert first.url == "https://en.wikisource.org/wiki/Treaty_of_Versailles"
    assert "Treaty of Versailles" in first.snippet
    assert first.published_at == datetime(2023, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert first.extras["wikisource_lang"] == "en"
    assert first.extras["page_title"] == "Treaty of Versailles"
    assert first.extras["page_id"] == 12345
    assert first.extras["word_count"] == 31200

    params = captured["params"][0]
    assert params["action"] == "query"
    assert params["list"] == "search"
    assert params["srsearch"] == "Treaty of Versailles"
    assert params["format"] == "json"
    assert params["formatversion"] == "2"
    headers = captured["headers"][0]
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"] == (
        "research-agent/0.1 "
        "(+https://github.com/bradtaylorsf/muckwire; "
        "contact: operator@example.test)"
    )


async def test_search_empty_payload_returns_empty(monkeypatch: pytest.MonkeyPatch):
    def _respond(url, params):
        return _FakeResp(200, _fixture("search_empty.json"))

    captured = _patch_httpx(monkeypatch, responder=_respond)

    assert await wikisource.search("no such source text", max_results=5) == []
    assert captured["urls"] == ["https://en.wikisource.org/w/api.php"]


async def test_search_routes_to_requested_language_host(
    monkeypatch: pytest.MonkeyPatch,
):
    def _respond(url, params):
        assert url == "https://fr.wikisource.org/w/api.php"
        return _FakeResp(200, _fixture("search_fr.json"))

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await wikisource.search("La Marseillaise", lang="fr", max_results=1)

    assert len(results) == 1
    assert results[0].url == "https://fr.wikisource.org/wiki/La_Marseillaise"
    assert results[0].extras["wikisource_lang"] == "fr"
    assert captured["params"][0]["srsearch"] == "La Marseillaise"


async def test_search_unsupported_language_returns_empty_without_request(
    monkeypatch: pytest.MonkeyPatch,
):
    def _respond(url, params):
        raise AssertionError("unsupported language must not make an HTTP request")

    captured = _patch_httpx(monkeypatch, responder=_respond)

    assert await wikisource.search("Treaty of Versailles", lang="xx") == []
    assert captured["urls"] == []


async def test_fetch_page_puts_full_transcribed_body_in_cleaned_text(
    monkeypatch: pytest.MonkeyPatch,
):
    def _respond(url, params):
        assert url == "https://en.wikisource.org/w/api.php"
        assert params["action"] == "parse"
        assert params["page"] == "Treaty of Versailles"
        assert params["prop"] == "text|revid|displaytitle"
        return _FakeResp(200, _fixture("parse_treaty.json"))

    _patch_httpx(monkeypatch, responder=_respond)

    source = await wikisource.fetch("https://en.wikisource.org/wiki/Treaty_of_Versailles")

    assert source is not None
    assert source.source_kind == "wikisource_search"
    assert source.url == "https://en.wikisource.org/wiki/Treaty_of_Versailles"
    assert source.title == "Treaty of Versailles"
    assert source.cleaned_text.startswith("# Treaty of Versailles")
    assert "This full body appears only in the fetched page" in source.cleaned_text
    assert "The High Contracting Parties agree" in source.cleaned_text
    assert "Done at Versailles" in source.cleaned_text
    assert source.metadata["wikisource_lang"] == "en"
    assert source.metadata["page_title"] == "Treaty of Versailles"
    assert source.metadata["revision_id"] == 987654321


async def test_fetch_index_php_title_url_and_fr_host(
    monkeypatch: pytest.MonkeyPatch,
):
    def _respond(url, params):
        assert url == "https://fr.wikisource.org/w/api.php"
        assert params["page"] == "La Marseillaise"
        return _FakeResp(200, _fixture("parse_fr.json"))

    _patch_httpx(monkeypatch, responder=_respond)

    source = await wikisource.fetch("https://fr.wikisource.org/w/index.php?title=La_Marseillaise")

    assert source is not None
    assert source.metadata["wikisource_lang"] == "fr"
    assert source.metadata["page_title"] == "La Marseillaise"
    assert "Allons enfants de la Patrie" in source.cleaned_text


async def test_search_returns_empty_on_transport_error(monkeypatch: pytest.MonkeyPatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_args, **_kwargs):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(wikisource._mediawiki.httpx, "AsyncClient", _client_factory)

    assert await wikisource.search("Treaty of Versailles") == []


async def test_search_returns_empty_on_4xx(monkeypatch: pytest.MonkeyPatch):
    def _respond(url, params):
        return _FakeResp(404, {"error": "not found"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await wikisource.search("Treaty of Versailles") == []


async def test_search_returns_empty_on_5xx(monkeypatch: pytest.MonkeyPatch):
    def _respond(url, params):
        return _FakeResp(503, {"error": "unavailable"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await wikisource.search("Treaty of Versailles") == []


async def test_fetch_returns_none_on_4xx(monkeypatch: pytest.MonkeyPatch):
    def _respond(url, params):
        return _FakeResp(404, {"error": "not found"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await wikisource.fetch("https://en.wikisource.org/wiki/Treaty_of_Versailles") is None


async def test_fetch_returns_none_on_5xx(monkeypatch: pytest.MonkeyPatch):
    def _respond(url, params):
        return _FakeResp(503, {"error": "unavailable"})

    _patch_httpx(monkeypatch, responder=_respond)

    assert await wikisource.fetch("https://en.wikisource.org/wiki/Treaty_of_Versailles") is None


async def test_fetch_rejects_non_wikisource_url() -> None:
    assert await wikisource.fetch("https://example.com/wiki/Treaty_of_Versailles") is None
    assert (
        await wikisource.fetch(
            "https://en.wikisource.org.attacker.example/wiki/Treaty_of_Versailles"
        )
        is None
    )


async def test_wikisource_rate_limit_is_shared_across_language_hosts(
    monkeypatch: pytest.MonkeyPatch,
):
    clock = [100.0]
    sleep_calls: list[float] = []

    def fake_monotonic() -> float:
        return clock[0]

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(wikisource._mediawiki.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(wikisource._mediawiki.asyncio, "sleep", fake_sleep)

    await wikisource._mediawiki.rate_limit("https://en.wikisource.org/w/api.php")
    await wikisource._mediawiki.rate_limit("https://fr.wikisource.org/w/api.php")

    assert sleep_calls == [1.0]


def test_smoke_registry_includes_wikisource_search() -> None:
    from research_agent.tools import TOOL_REGISTRY

    assert "wikisource_search" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["wikisource_search"])


def test_smoke_wrapper_requires_fetched_cleaned_text(monkeypatch: pytest.MonkeyPatch):
    from research_agent.tools import TOOL_REGISTRY

    async def fake_search(query: str, *, max_results: int = 20):
        return [
            SearchResult(
                url="https://en.wikisource.org/wiki/Treaty_of_Versailles",
                title="Treaty of Versailles",
                snippet="Treaty snippet",
                source_kind="wikisource_search",
                extras={"wikisource_lang": "en"},
            )
        ]

    async def fake_fetch(url: str):
        return Source(
            url=url,
            title="Treaty of Versailles",
            cleaned_text="Full Treaty of Versailles body",
            fetched_at=datetime.now(UTC),
            source_kind="wikisource_search",
            metadata={
                "wikisource_lang": "en",
                "page_title": "Treaty of Versailles",
                "revision_id": 987,
            },
        )

    monkeypatch.setattr(wikisource, "search", fake_search)
    monkeypatch.setattr(wikisource, "fetch", fake_fetch)

    out = TOOL_REGISTRY["wikisource_search"]("Treaty of Versailles")

    assert "wikisource_search: returned 1 hits" in out
    assert "Treaty of Versailles" in out
    assert "chars=" in out
    assert "Full Treaty of Versailles body" in out


def test_registered_kind_links_wikisource_skill() -> None:
    from research_agent.tools._registry import get_kind

    entry = get_kind("wikisource_search")
    assert entry is not None
    assert entry.skill_name == "wikisource"
    assert entry.fetch_fn is wikisource.fetch
    assert "lang" in entry.optional_payload_knobs


def test_wikisource_skill_covers_required_operator_topics() -> None:
    from research_agent.skills import load_skill

    body = load_skill("connectors", "wikisource")

    for needle in (
        "<lang>.wikisource.org/w/api.php",
        "default is `lang=en`",
        "Source.cleaned_text",
        "Treaty of Versailles",
        "Federalist No. 10",
        "multilingual-source-handling",
        "living-author",
        "public-domain",
    ):
        assert needle in body
