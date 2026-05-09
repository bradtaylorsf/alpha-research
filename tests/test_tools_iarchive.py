"""Tests for ``research_agent.tools.iarchive`` (issue #225)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import iarchive

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "iarchive"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    iarchive.reset_for_tests()
    monkeypatch.setattr(iarchive.asyncio, "sleep", AsyncMock())
    yield
    iarchive.reset_for_tests()


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "iarchive-cache"
    monkeypatch.setattr(iarchive, "_CACHE_DIR", target)
    return target


@pytest.fixture
def no_archive_save(monkeypatch):
    """Suppress the fire-and-forget Wayback save kicked off by ``fetch()``."""

    async def _noop(_url, *_a, **_kw):
        return None

    monkeypatch.setattr(iarchive.archive, "save", _noop)


def _patch_httpx(monkeypatch, *, responder):
    """Replace ``httpx.AsyncClient`` with a fake driven by ``responder(url, params)``.

    Returns ``(status_code, body_text)``.
    """
    captured: dict[str, list] = {"urls": [], "headers": [], "params": []}

    class _FakeResp:
        def __init__(self, status: int, text: str) -> None:
            self.status_code = status
            self.text = text

        def json(self):
            return json.loads(self.text)

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        captured["headers"].append(kwargs.get("headers"))

        class _Client:
            async def get(self, url, *, params=None, **_kwargs):
                captured["urls"].append(url)
                captured["params"].append(params)
                status, text = responder(url, params)
                return _FakeResp(status, text)

        yield _Client()

    monkeypatch.setattr(iarchive.httpx, "AsyncClient", _client_factory)
    return captured


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_happy_returns_results(monkeypatch):
    payload = (FIXTURES / "search-pullman-strike.json").read_text(encoding="utf-8")

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await iarchive.search("Pullman Strike")

    assert len(results) == 3
    first = results[0]
    assert first.source_kind == "iarchive"
    assert first.url == "https://archive.org/details/pullmanstrikestor00lind"
    assert first.title.startswith("The Pullman strike")
    assert first.extras["identifier"] == "pullmanstrikestor00lind"
    assert first.extras["mediatype"] == "texts"
    assert first.extras["downloads"] == 4523
    assert first.extras["creator"] == "Lindsey, Almont"
    assert first.extras["date"] == "1942"
    # Snippet stitched from creator/date/mediatype/description.
    assert "Lindsey, Almont" in first.snippet
    assert "1942" in first.snippet
    assert "texts" in first.snippet

    # Second hit is audio — confirms the fixture covers both mediatypes.
    second = results[1]
    assert second.extras["mediatype"] == "audio"

    # Headers + endpoint sanity.
    headers = captured["headers"][0]
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"]
    assert captured["urls"][0] == iarchive._BASE_URL


async def test_search_empty_docs(monkeypatch):
    payload = json.dumps({"response": {"docs": []}})

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    assert await iarchive.search("anything") == []


async def test_search_includes_mediatype_filter(monkeypatch):
    payload = (FIXTURES / "search-audio-foia.json").read_text(encoding="utf-8")

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await iarchive.search("FOIA", mediatype="audio")

    # The query passed upstream must AND in mediatype:audio.
    params = captured["params"][0]
    pairs = list(params) if not isinstance(params, dict) else list(params.items())
    q_values = [v for (k, v) in pairs if k == "q"]
    assert len(q_values) == 1
    assert "mediatype:audio" in q_values[0]
    assert "FOIA" in q_values[0]
    # And every returned hit really is audio (per the fixture).
    assert all(r.extras["mediatype"] == "audio" for r in results)


async def test_search_unknown_mediatype_is_dropped(monkeypatch):
    payload = (FIXTURES / "search-pullman-strike.json").read_text(encoding="utf-8")

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await iarchive.search("Pullman Strike", mediatype="bogus")
    # Connector logs a warning and drops the unknown value — no crash.
    assert len(results) == 3
    params = captured["params"][0]
    pairs = list(params) if not isinstance(params, dict) else list(params.items())
    q_values = [v for (k, v) in pairs if k == "q"]
    assert len(q_values) == 1
    assert "mediatype:" not in q_values[0]


async def test_search_returns_empty_on_404(monkeypatch):
    def _respond(url, params):
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await iarchive.search("anything") == []


async def test_search_returns_empty_on_503(monkeypatch):
    def _respond(url, params):
        return 503, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await iarchive.search("anything") == []


async def test_search_returns_empty_on_transport_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(iarchive.httpx, "AsyncClient", _client_factory)

    assert await iarchive.search("anything") == []


async def test_search_returns_empty_on_non_json(monkeypatch):
    def _respond(url, params):
        return 200, "<html>not json</html>"

    _patch_httpx(monkeypatch, responder=_respond)

    assert await iarchive.search("anything") == []


async def test_search_rate_limit_gate_sleeps_within_interval(monkeypatch):
    """Two concurrent search calls must both pass through the rate gate."""
    clock = [100.0]

    def fake_monotonic() -> float:
        return clock[0]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(iarchive.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(iarchive.asyncio, "sleep", fake_sleep)

    payload = json.dumps({"response": {"docs": []}})

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    await asyncio.gather(iarchive.search("a"), iarchive.search("b"))

    assert any(s > 0 for s in sleep_calls), (
        f"expected at least one >0 sleep through the rate gate; got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


_TEXT_ITEM_URL = "https://archive.org/details/pullmanstrikestor00lind"
_TEXT_ITEM_API = "https://archive.org/metadata/pullmanstrikestor00lind"
_TEXT_ITEM_PAYLOAD = {
    "metadata": {
        "identifier": "pullmanstrikestor00lind",
        "title": "The Pullman strike",
        "creator": "Lindsey, Almont",
        "date": "1942",
        "mediatype": "texts",
        "downloads": 4523,
        "description": "Narrative of the 1894 strike.",
        "publicdate": "2008-03-12T01:23:55Z",
        "collection": ["americana", "library_of_congress"],
    },
    "files": [
        {"name": "pullmanstrikestor00lind_djvu.txt", "format": "DjVuTXT"},
        {"name": "pullmanstrikestor00lind.pdf", "format": "Text PDF"},
        {"name": "pullmanstrikestor00lind_jp2.zip", "format": "Single Page Processed JP2 ZIP"},
    ],
}


_AUDIO_ITEM_URL = "https://archive.org/details/foia_oral_history_1974"
_AUDIO_ITEM_API = "https://archive.org/metadata/foia_oral_history_1974"
_AUDIO_ITEM_PAYLOAD = {
    "metadata": {
        "identifier": "foia_oral_history_1974",
        "title": "FOIA oral history: amendments of 1974",
        "creator": "National Security Archive",
        "date": "1995-03-14",
        "mediatype": "audio",
        "downloads": 318,
        "description": "Panel discussion.",
        "publicdate": "2012-04-21T16:00:00Z",
    },
    "files": [
        {"name": "foia_panel_master.flac", "format": "FLAC"},
        {"name": "foia_panel_64kb.mp3", "format": "VBR MP3"},
        {"name": "foia_panel.png", "format": "PNG"},
    ],
}


async def test_fetch_text_mediatype_surfaces_fulltext_url(
    monkeypatch, cache_dir: Path, no_archive_save
):
    body = json.dumps(_TEXT_ITEM_PAYLOAD)

    def _respond(url, params):
        if url == _TEXT_ITEM_API:
            return 200, body
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    source = await iarchive.fetch(_TEXT_ITEM_URL)

    assert source is not None
    assert source.source_kind == "iarchive"
    assert source.url == _TEXT_ITEM_URL
    assert source.title == "The Pullman strike"
    assert source.metadata["identifier"] == "pullmanstrikestor00lind"
    assert source.metadata["mediatype"] == "texts"
    assert source.metadata["downloads"] == 4523
    expected_fulltext = (
        "https://archive.org/download/pullmanstrikestor00lind/"
        "pullmanstrikestor00lind_djvu.txt"
    )
    assert source.metadata["fulltext_url"] == expected_fulltext

    # Cache file written under the per-test cache dir.
    cached = list(cache_dir.glob("item-*.json"))
    assert len(cached) == 1


async def test_fetch_audio_mediatype_surfaces_audio_files(
    monkeypatch, cache_dir: Path, no_archive_save
):
    body = json.dumps(_AUDIO_ITEM_PAYLOAD)

    def _respond(url, params):
        if url == _AUDIO_ITEM_API:
            return 200, body
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    source = await iarchive.fetch(_AUDIO_ITEM_URL)

    assert source is not None
    assert source.metadata["mediatype"] == "audio"
    audio_files = source.metadata["audio_files"]
    assert isinstance(audio_files, list)
    assert len(audio_files) == 2
    # Both canonical audio derivatives must be present, by the IA download URL.
    assert any(u.endswith("foia_panel_master.flac") for u in audio_files)
    assert any(u.endswith("foia_panel_64kb.mp3") for u in audio_files)
    # The PNG file MUST NOT leak through.
    assert all(not u.endswith(".png") for u in audio_files)
    # No fulltext_url for audio mediatype.
    assert "fulltext_url" not in source.metadata


async def test_fetch_caches_and_skips_second_http_call(
    monkeypatch, cache_dir: Path, no_archive_save
):
    body = json.dumps(_TEXT_ITEM_PAYLOAD)

    def _respond(url, params):
        return 200, body

    captured = _patch_httpx(monkeypatch, responder=_respond)

    s1 = await iarchive.fetch(_TEXT_ITEM_URL)
    api_calls_after_first = [u for u in captured["urls"] if u == _TEXT_ITEM_API]
    s2 = await iarchive.fetch(_TEXT_ITEM_URL)
    api_calls_after_second = [u for u in captured["urls"] if u == _TEXT_ITEM_API]

    assert s1 is not None and s2 is not None
    assert len(api_calls_after_first) == 1
    assert len(api_calls_after_second) == 1


async def test_fetch_rejects_lookalike_host(monkeypatch, cache_dir: Path):
    spoof = "https://archive.org.attacker.example/details/pullmanstrikestor00lind"
    assert await iarchive.fetch(spoof) is None


async def test_fetch_returns_none_for_non_detail_path(monkeypatch, cache_dir: Path):
    assert (
        await iarchive.fetch("https://archive.org/about/")
        is None
    )


async def test_fetch_returns_none_on_404(monkeypatch, cache_dir: Path, no_archive_save):
    def _respond(url, params):
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await iarchive.fetch(_TEXT_ITEM_URL) is None


async def test_fetch_returns_none_on_transport_error(
    monkeypatch, cache_dir: Path, no_archive_save
):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(iarchive.httpx, "AsyncClient", _client_factory)

    assert await iarchive.fetch(_TEXT_ITEM_URL) is None


# ---------------------------------------------------------------------------
# Smoke registration
# ---------------------------------------------------------------------------


def test_smoke_registry_includes_iarchive_search():
    from research_agent.tools import TOOL_REGISTRY

    assert "iarchive_search" in TOOL_REGISTRY


def test_module_declares_kind_constant():
    assert iarchive.KIND == "iarchive_search"
