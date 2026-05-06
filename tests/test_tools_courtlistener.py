"""Tests for `research_agent.tools.courtlistener` (issue #93)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import courtlistener

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_token(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "tok-test-1234567890abcdef")
    yield


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    courtlistener.reset_for_tests()
    monkeypatch.setattr(courtlistener.asyncio, "sleep", AsyncMock())
    yield
    courtlistener.reset_for_tests()


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "courtlistener-cache"
    monkeypatch.setattr(courtlistener, "_CACHE_DIR", target)
    return target


_SEARCH_PAYLOAD = {
    "count": 2,
    "results": [
        {
            "absolute_url": "/opinion/4341372/lozman-v-city-of-riviera-beach/",
            "caseName": "Lozman v. City of Riviera Beach",
            "case_name_short": "Lozman",
            "court": "Supreme Court of the United States",
            "court_id": "scotus",
            "dateFiled": "2018-06-18",
            "docketNumber": "17-21",
            "snippet": "First Amendment <em>retaliation</em> arrest claim",
            "citation": ["585 U.S. 87"],
            "lexisCite": "138 S. Ct. 1945",
            "neutralCite": "",
        },
        {
            "absolute_url": "/opinion/12345/doe-v-roe/",
            "caseName": "Doe v. Roe",
            "court": "9th Cir.",
            "court_id": "ca9",
            "dateFiled": "2021-03-04",
            "docketNumber": "20-1234",
            "snippet": "",
            "text": (
                "This is a long opinion text used to derive a snippet "
                "when no highlight snippet is provided by the API."
            ),
            "citation": [],
            "lexisCite": "999 F.3d 1",
            "neutralCite": "",
        },
    ],
}


_OPINION_PAYLOAD = {
    "id": 4341372,
    "case_name": "Lozman v. City of Riviera Beach",
    "court": "scotus",
    "court_id": "scotus",
    "docket_number": "17-21",
    "citation": "585 U.S. 87",
    "date_filed": "2018-06-18",
    "plain_text": (
        "Held: The existence of probable cause does not bar Lozman's"
        " First Amendment retaliation claim."
    ),
    "html_with_citations": "<p>fallback html</p>",
    "html": "",
    "html_lawbox": "",
}


_DOCKET_PAYLOAD = {
    "id": 555,
    "case_name": "United States v. Acme",
    "court": "nysd",
    "court_id": "nysd",
    "docket_number": "1:23-cv-12345",
    "date_filed": "2023-01-15",
}


_DOCKET_ENTRIES_PAYLOAD = {
    "count": 2,
    "results": [
        {
            "entry_number": 1,
            "date_filed": "2023-01-15",
            "description": "COMPLAINT against Acme Corp.",
            "recap_documents": [
                {"id": 1, "is_available": True, "filepath_local": "x.pdf"}
            ],
        },
        {
            "entry_number": 2,
            "date_filed": "2023-02-01",
            "description": "ORDER setting initial conference.",
            "recap_documents": [],
        },
    ],
}


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

    monkeypatch.setattr(courtlistener.httpx, "AsyncClient", _client_factory)
    return captured


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_builds_correct_query_and_auth_header(monkeypatch):
    payload = json.dumps(_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await courtlistener.search(
        "first amendment retaliation", kind="opinions"
    )

    assert len(results) == 2
    # Auth header carries the token.
    headers = captured["headers"][0]
    assert headers["Authorization"] == "Token tok-test-1234567890abcdef"
    assert headers["Accept"] == "application/json"
    # type=o for opinions.
    assert captured["params"][0] == {
        "q": "first amendment retaliation",
        "type": "o",
    }
    # URL is the search endpoint.
    assert captured["urls"][0].endswith("/api/rest/v3/search/")


async def test_search_parses_results(monkeypatch):
    payload = json.dumps(_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    results = await courtlistener.search("first amendment retaliation")

    first = results[0]
    assert first.source_kind == "courtlistener"
    assert first.url == (
        "https://www.courtlistener.com/opinion/4341372/lozman-v-city-of-riviera-beach/"
    )
    assert first.title == "Lozman v. City of Riviera Beach"
    assert "retaliation" in first.snippet
    assert "<em>" not in first.snippet
    assert first.published_at is not None
    assert first.published_at.year == 2018
    assert first.extras["court"] == "Supreme Court of the United States"
    assert first.extras["citation"] == "585 U.S. 87"
    assert first.extras["docket_number"] == "17-21"
    assert first.extras["kind"] == "opinions"

    # Second result has no snippet field — falls back to text[:300].
    second = results[1]
    assert second.snippet.startswith("This is a long opinion text")
    # Falls back to lexisCite when citation list is empty.
    assert second.extras["citation"] == "999 F.3d 1"


async def test_search_kind_dockets_uses_type_r(monkeypatch):
    payload = json.dumps({"results": []})

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await courtlistener.search("anything", kind="dockets")

    assert captured["params"][0]["type"] == "r"


async def test_search_kind_oral_arguments_uses_type_oa(monkeypatch):
    payload = json.dumps({"results": []})

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await courtlistener.search("anything", kind="oral_arguments")

    assert captured["params"][0]["type"] == "oa"


async def test_search_unknown_kind_raises(monkeypatch):
    with pytest.raises(ValueError, match="unknown kind"):
        await courtlistener.search("anything", kind="bogus")


async def test_search_returns_empty_on_401(monkeypatch):
    def _respond(url, params):
        return 401, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await courtlistener.search("anything") == []


async def test_search_returns_empty_on_500(monkeypatch):
    def _respond(url, params):
        return 500, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await courtlistener.search("anything") == []


async def test_search_returns_empty_on_non_json(monkeypatch):
    def _respond(url, params):
        return 200, "<html>not json</html>"

    _patch_httpx(monkeypatch, responder=_respond)

    assert await courtlistener.search("anything") == []


async def test_search_returns_empty_on_transport_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(courtlistener.httpx, "AsyncClient", _client_factory)

    assert await courtlistener.search("anything") == []


async def test_search_raises_when_token_missing(monkeypatch):
    monkeypatch.delenv("COURTLISTENER_API_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="COURTLISTENER_API_TOKEN"):
        await courtlistener.search("anything")


async def test_search_max_results_caps_output(monkeypatch):
    payload = json.dumps(_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    results = await courtlistener.search("anything", max_results=1)

    assert len(results) == 1


async def test_rate_limit_gate_sleeps_within_interval(monkeypatch):
    """Two concurrent search calls must both pass through the rate gate."""
    clock = [100.0]

    def fake_monotonic() -> float:
        return clock[0]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(courtlistener.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(courtlistener.asyncio, "sleep", fake_sleep)

    payload = json.dumps({"results": []})

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    await asyncio.gather(
        courtlistener.search("a"), courtlistener.search("b")
    )

    assert any(s > 0 for s in sleep_calls), (
        f"expected at least one >0 sleep through the rate gate; got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


_OPINION_URL = (
    "https://www.courtlistener.com/opinion/4341372/lozman-v-city-of-riviera-beach/"
)
_OPINION_API = "https://www.courtlistener.com/api/rest/v3/opinions/4341372/"

_DOCKET_URL = "https://www.courtlistener.com/docket/555/united-states-v-acme/"
_DOCKET_API = "https://www.courtlistener.com/api/rest/v3/dockets/555/"
_DOCKET_ENTRIES_API = "https://www.courtlistener.com/api/rest/v3/docket-entries/"


async def test_fetch_opinion_uses_plain_text(monkeypatch, cache_dir: Path):
    opinion_payload = json.dumps(_OPINION_PAYLOAD)

    def _respond(url, params):
        if url == _OPINION_API:
            return 200, opinion_payload
        return 404, ""

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await courtlistener.fetch(_OPINION_URL)

    assert source is not None
    assert source.source_kind == "courtlistener"
    assert source.url == _OPINION_URL
    assert "probable cause" in source.cleaned_text
    assert "First Amendment retaliation" in source.cleaned_text
    assert source.metadata["court"] == "scotus"
    assert source.metadata["docket_number"] == "17-21"
    assert source.metadata["citation"] == "585 U.S. 87"
    assert source.metadata["recap_available"] is False
    assert _OPINION_API in captured["urls"]
    # Cache file was written.
    cached = list(cache_dir.glob("opinion-*.json"))
    assert len(cached) == 1


async def test_fetch_opinion_falls_back_to_html_when_no_plain_text(
    monkeypatch, cache_dir: Path
):
    payload = dict(_OPINION_PAYLOAD)
    payload["plain_text"] = ""
    payload["html_with_citations"] = (
        "<html><body><p>Falling back to extracted HTML body content with "
        "enough text to be picked up by trafilatura's recall mode.</p></body></html>"
    )

    def _respond(url, params):
        if url == _OPINION_API:
            return 200, json.dumps(payload)
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    source = await courtlistener.fetch(_OPINION_URL)

    assert source is not None
    assert "Falling back" in source.cleaned_text


async def test_fetch_docket_paginates_entries_and_flags_missing_recap(
    monkeypatch, cache_dir: Path
):
    docket_payload = json.dumps(_DOCKET_PAYLOAD)
    entries_payload = json.dumps(_DOCKET_ENTRIES_PAYLOAD)

    def _respond(url, params):
        if url == _DOCKET_API:
            return 200, docket_payload
        if url == _DOCKET_ENTRIES_API:
            assert params == {"docket": "555", "order_by": "entry_number"}
            return 200, entries_payload
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    source = await courtlistener.fetch(_DOCKET_URL)

    assert source is not None
    assert source.source_kind == "courtlistener"
    assert "United States v. Acme" in source.cleaned_text
    assert "## Entry 1" in source.cleaned_text
    assert "## Entry 2" in source.cleaned_text
    assert "COMPLAINT" in source.cleaned_text
    # Entry 2 had an empty `recap_documents`, so the placeholder is rendered.
    assert "no RECAP document available" in source.cleaned_text
    assert source.metadata["court"] == "nysd"
    assert source.metadata["docket_number"] == "1:23-cv-12345"
    assert source.metadata["recap_available"] is True
    assert source.metadata["entry_count"] == 2


async def test_fetch_caches_api_response_on_second_call(
    monkeypatch, cache_dir: Path
):
    opinion_payload = json.dumps(_OPINION_PAYLOAD)

    def _respond(url, params):
        if url == _OPINION_API:
            return 200, opinion_payload
        return 404, ""

    captured = _patch_httpx(monkeypatch, responder=_respond)

    s1 = await courtlistener.fetch(_OPINION_URL)
    s2 = await courtlistener.fetch(_OPINION_URL)

    assert s1 is not None
    assert s2 is not None
    # Only one network hit thanks to the JSON cache.
    api_calls = [u for u in captured["urls"] if u == _OPINION_API]
    assert len(api_calls) == 1


async def test_fetch_returns_none_on_404(monkeypatch, cache_dir: Path):
    def _respond(url, params):
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await courtlistener.fetch(_OPINION_URL) is None


async def test_fetch_returns_none_on_transport_error(
    monkeypatch, cache_dir: Path
):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(courtlistener.httpx, "AsyncClient", _client_factory)

    assert await courtlistener.fetch(_OPINION_URL) is None


async def test_fetch_returns_none_on_unrecognised_url(
    monkeypatch, cache_dir: Path
):
    assert (
        await courtlistener.fetch("https://example.com/some-page")
        is None
    )
    assert (
        await courtlistener.fetch(
            "https://www.courtlistener.com/random/path/"
        )
        is None
    )


async def test_fetch_token_missing_raises(monkeypatch, cache_dir: Path):
    monkeypatch.delenv("COURTLISTENER_API_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="COURTLISTENER_API_TOKEN"):
        await courtlistener.fetch(_OPINION_URL)


# ---------------------------------------------------------------------------
# Smoke registration
# ---------------------------------------------------------------------------


def test_smoke_registry_includes_courtlistener():
    from research_agent.tools import TOOL_REGISTRY

    assert "courtlistener" in TOOL_REGISTRY
