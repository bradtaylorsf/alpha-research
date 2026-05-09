"""Tests for `research_agent.tools.loc` (issue #224)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import loc

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "loc"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    loc.reset_for_tests()
    # Skip the rate-limit sleep so the suite stays fast.
    monkeypatch.setattr(loc.asyncio, "sleep", AsyncMock())
    yield
    loc.reset_for_tests()


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "loc-cache"
    monkeypatch.setattr(loc, "_CACHE_DIR", target)
    return target


def _patch_httpx(monkeypatch, *, responder):
    """Replace ``httpx.AsyncClient`` with a fake driven by ``responder(url, params)``.

    ``responder`` returns ``(status_code, body_text)``; raise from inside
    the responder to simulate transport errors.
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

    monkeypatch.setattr(loc.httpx, "AsyncClient", _client_factory)
    return captured


# ---------------------------------------------------------------------------
# search() — happy path per fixture
# ---------------------------------------------------------------------------


async def test_search_all_collections_happy_path(monkeypatch):
    payload = json.dumps(_load_fixture("search-battle-of-algiers.json"))

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await loc.search("battle of algiers", max_results=5)

    assert len(results) == 2
    # Default (no collection) routes to /search/.
    assert captured["urls"][0] == "https://www.loc.gov/search/"
    assert captured["params"][0]["q"] == "battle of algiers"
    assert captured["params"][0]["sp"] == 1
    assert captured["params"][0]["c"] == 5
    assert captured["params"][0]["fo"] == "json"

    first = results[0]
    assert first.source_kind == "loc"
    assert first.url.startswith("https://www.loc.gov/")
    assert first.title  # non-empty
    assert first.snippet  # non-empty
    assert first.extras["collection"] == ""
    assert isinstance(first.extras["mime_type"], list)


async def test_search_chronam_routes_to_collection_path(monkeypatch):
    payload = json.dumps(_load_fixture("search-chronam-pullman-strike.json"))

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await loc.search(
        "pullman strike", collection="chronicling-america", max_results=3
    )

    assert len(results) == 2
    # collection=chronicling-america routes to /collections/chronicling-america/.
    assert (
        captured["urls"][0]
        == "https://www.loc.gov/collections/chronicling-america/"
    )
    first = results[0]
    assert first.extras["collection"] == "chronicling-america"
    # OCR-derived snippet should be substantive.
    assert len(first.snippet) > 50


async def test_search_prints_routes_to_photos_path(monkeypatch):
    payload = json.dumps(_load_fixture("search-prints-civilwar.json"))

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await loc.search(
        "civil war lincoln", collection="prints", max_results=2
    )

    assert len(results) == 2
    # collection=prints routes to the /photos/ format-portal endpoint.
    assert captured["urls"][0] == "https://www.loc.gov/photos/"

    # First result has full image URLs.
    first = results[0]
    assert first.extras["collection"] == "prints"
    assert first.extras["image_url"]  # non-empty
    # The fragment (#h=...&w=...) should be stripped.
    assert "#" not in first.extras["image_url"]

    # Second result's url is protocol-relative — should be normalized.
    second = results[1]
    assert second.url.startswith("https://")


async def test_search_collection_routing_for_remaining_surfaces(monkeypatch):
    """Each documented `collection` value resolves to the right endpoint."""
    payload = json.dumps({"results": [], "pagination": {}})

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await loc.search("anything", collection="manuscripts")
    await loc.search("anything", collection="recordings")
    await loc.search("anything", collection="maps")
    # An unknown collection falls back to /collections/<slug>/.
    await loc.search("anything", collection="custom-slug")

    assert captured["urls"] == [
        "https://www.loc.gov/manuscripts/",
        "https://www.loc.gov/audio/",
        "https://www.loc.gov/maps/",
        "https://www.loc.gov/collections/custom-slug/",
    ]


# ---------------------------------------------------------------------------
# search() — empty / pagination / errors
# ---------------------------------------------------------------------------


async def test_search_returns_empty_on_no_results(monkeypatch):
    payload = json.dumps({"results": [], "pagination": {"current": 1}})

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    assert await loc.search("nothing matches this") == []


async def test_search_pagination_passes_sp_param(monkeypatch):
    payload = json.dumps({"results": [], "pagination": {"current": 2}})

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await loc.search("anything", page=2)
    assert captured["params"][0]["sp"] == 2

    await loc.search("anything", page=3)
    assert captured["params"][1]["sp"] == 3


async def test_search_returns_empty_on_4xx(monkeypatch):
    def _respond(url, params):
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await loc.search("anything") == []


async def test_search_returns_empty_on_5xx(monkeypatch):
    def _respond(url, params):
        return 503, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await loc.search("anything") == []


async def test_search_returns_empty_on_transport_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(loc.httpx, "AsyncClient", _client_factory)

    assert await loc.search("anything") == []


async def test_search_returns_empty_on_non_json(monkeypatch):
    def _respond(url, params):
        return 200, "<html>not json</html>"

    _patch_httpx(monkeypatch, responder=_respond)

    assert await loc.search("anything") == []


async def test_search_ignores_extra_planner_kwargs(monkeypatch):
    """Orchestrator may thread sub_question / max_results / unknown knobs;
    the connector must accept and ignore connector-irrelevant fields."""
    payload = json.dumps({"results": [], "pagination": {}})

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    # Should not TypeError.
    assert (
        await loc.search(
            "anything",
            collection=None,
            max_results=5,
            sub_question="What was the strike about?",
            something_unrelated="ignored",
        )
        == []
    )


async def test_rate_limit_gate_serializes_concurrent_calls(monkeypatch):
    """Two concurrent search calls must both pass through the rate gate."""
    clock = [100.0]

    def fake_monotonic() -> float:
        return clock[0]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(loc.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(loc.asyncio, "sleep", fake_sleep)

    payload = json.dumps({"results": [], "pagination": {}})

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    await asyncio.gather(loc.search("a"), loc.search("b"))

    assert any(s > 0 for s in sleep_calls), (
        "expected at least one >0 sleep through the 1 RPS gate;"
        f" got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


_ITEM_URL = "https://www.loc.gov/item/2008679147/"
_ITEM_PAYLOAD = {
    "item": {
        "title": "'Diamond horseshoe' Algiers style",
        "dates": ["1943"],
        "description": [
            "Photograph showing American soldiers listening to an orchestra."
        ],
        "subjects": [
            "United States Army Band--Algeria--Algiers--1940-1950",
            "World War, 1939-1945",
        ],
        "image_url": [
            "https://tile.loc.gov/storage-services/service/pnp/ppmsca/18500/18547_150px.jpg#h=119&w=150"
        ],
    },
    "resources": [
        {
            "url": "https://www.loc.gov/resource/ppmsca.18547/",
            "image": "https://tile.loc.gov/storage-services/service/pnp/ppmsca/18500/18547r.jpg",
        }
    ],
}

_CHRONAM_URL = "https://www.loc.gov/resource/sn2001063112/1894-08-24/ed-1/?sp=7"
_CHRONAM_FT_URL = (
    "https://tile.loc.gov/text-services/word-coordinates-service?"
    "segment=/service/ndnp/sdhi/batch_x/data/sn2001063112/00415624645/1894082401/0091.xml"
    "&format=alto_xml&full_text=1"
)
_CHRONAM_RESOURCE_PAYLOAD = {
    "resource": {"url": _CHRONAM_URL},
    "item": {"title": "The Mitchell Capital, August 24, 1894, page 7"},
    "fulltext_service": _CHRONAM_FT_URL,
}
_CHRONAM_FT_PAYLOAD = {
    "/service/ndnp/sdhi/batch_x/data/sn2001063112/00415624645/1894082401/0091.xml": {
        "full_text": (
            "MITCHELL CAPITAL. PAGES 5 TO 8. AUGUST 24 1894. CURRENT COMMENT. "
            "Of course the Populist party has no sympathy with anarchy in spite "
            "of its endorsement of Debs and Altgeld and Waite..."
        )
    }
}


async def test_fetch_item_builds_markdown_and_caches(
    monkeypatch, cache_dir: Path
):
    item_payload = json.dumps(_ITEM_PAYLOAD)

    def _respond(url, params):
        if url.startswith("https://www.loc.gov/item/2008679147/"):
            return 200, item_payload
        return 404, ""

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await loc.fetch(_ITEM_URL)

    assert source is not None
    assert source.source_kind == "loc"
    assert source.url == _ITEM_URL
    assert source.title == "'Diamond horseshoe' Algiers style"
    body = source.cleaned_text
    assert "'Diamond horseshoe' Algiers style" in body
    assert "American soldiers" in body
    assert "World War" in body  # subjects rendered

    # IIIF metadata for an /item/ URL: image_url + manifest synth.
    assert source.metadata.get("image_url")
    assert "#" not in source.metadata["image_url"]  # fragment stripped
    assert source.metadata.get("image_iiif_manifest") == (
        "https://www.loc.gov/item/2008679147/manifest.json"
    )

    # Cache write: a second fetch should not hit the network.
    pre_count = len(captured["urls"])
    source2 = await loc.fetch(_ITEM_URL)
    assert source2 is not None
    assert len(captured["urls"]) == pre_count


async def test_fetch_chronam_resource_puts_ocr_in_cleaned_text(
    monkeypatch, cache_dir: Path
):
    """Per AC: chronam OCR must land in `cleaned_text`, NOT `metadata`."""
    res_payload = json.dumps(_CHRONAM_RESOURCE_PAYLOAD)
    ft_payload = json.dumps(_CHRONAM_FT_PAYLOAD)

    def _respond(url, params):
        if "fulltext_service" in url or "tile.loc.gov/text-services" in url:
            return 200, ft_payload
        if url.startswith("https://www.loc.gov/resource/"):
            return 200, res_payload
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    source = await loc.fetch(_CHRONAM_URL)

    assert source is not None
    assert source.source_kind == "loc"
    # OCR text in cleaned_text — the whole point of the chronam path.
    assert "Populist party" in source.cleaned_text
    assert "MITCHELL CAPITAL" in source.cleaned_text
    # And explicitly NOT in metadata.
    assert "Populist party" not in json.dumps(source.metadata)
    # /resource/ URLs don't synthesize an IIIF manifest (no per-page manifest).
    assert "image_iiif_manifest" not in source.metadata


async def test_fetch_strips_fo_json_from_canonical_url(
    monkeypatch, cache_dir: Path
):
    """If the input URL already carries `?fo=json`, the canonical URL
    on the returned `Source` must be the human-facing URL."""
    item_payload = json.dumps(_ITEM_PAYLOAD)

    def _respond(url, params):
        return 200, item_payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await loc.fetch(_ITEM_URL + "?fo=json")
    assert source is not None
    assert source.url == _ITEM_URL
    # The connector did pass `fo=json` on the actual GET (via params).
    assert any(p and p.get("fo") == "json" for p in captured["params"])


async def test_fetch_rejects_lookalike_host(monkeypatch, cache_dir: Path):
    """Strict host gate — only `www.loc.gov` is accepted."""
    captured = _patch_httpx(monkeypatch, responder=lambda u, p: (200, "{}"))

    assert await loc.fetch("https://www.loc.gov.attacker.example/item/1/") is None
    # Also reject the deprecated chronam host even when the path looks valid.
    assert await loc.fetch(
        "https://chroniclingamerica.loc.gov/lccn/sn83045366/1894-08-24/ed-1/seq-1/"
    ) is None
    # Non-routable paths under the right host return None.
    assert await loc.fetch("https://www.loc.gov/about/") is None

    # No HTTP traffic should have happened for any of these.
    assert captured["urls"] == []


async def test_fetch_returns_none_on_4xx(monkeypatch, cache_dir: Path):
    def _respond(url, params):
        return 503, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await loc.fetch(_ITEM_URL) is None


async def test_fetch_returns_none_on_empty_url():
    assert await loc.fetch("") is None
