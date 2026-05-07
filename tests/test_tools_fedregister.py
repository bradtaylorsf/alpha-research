"""Tests for `research_agent.tools.fedregister` (issue #102)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import unquote_plus, urlsplit

import httpx
import pytest

from research_agent.tools import fedregister

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    fedregister.reset_for_tests()
    monkeypatch.setattr(fedregister.asyncio, "sleep", AsyncMock())
    yield
    fedregister.reset_for_tests()


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "fedregister-cache"
    monkeypatch.setattr(fedregister, "_CACHE_DIR", target)
    return target


_SEARCH_PAYLOAD = {
    "count": 2,
    "results": [
        {
            "document_number": "2023-28147",
            "title": "Safe, Secure, and Trustworthy Development and Use of AI",
            "abstract": (
                "Executive Order on the Safe, Secure, and Trustworthy "
                "Development and Use of Artificial Intelligence."
            ),
            "publication_date": "2023-11-01",
            "html_url": (
                "https://www.federalregister.gov/documents/2023/11/01/"
                "2023-28147/safe-secure-and-trustworthy-development-and-use-of-ai"
            ),
            "type": "Presidential Document",
            "agencies": [
                {"name": "Executive Office of the President", "raw_name": "EOP"}
            ],
            "significant": True,
        },
        {
            "document_number": "2024-99999",
            "title": "AI Notice of Inquiry",
            "abstract": "",
            "publication_date": "2024-02-15",
            "html_url": (
                "https://www.federalregister.gov/documents/2024/02/15/"
                "2024-99999/ai-notice-of-inquiry"
            ),
            "type": "Notice",
            "agencies": [
                {"name": "Department of Commerce", "raw_name": "DOC"}
            ],
            "significant": False,
        },
    ],
}


_DOC_NUMBER = "2023-28147"
_DOC_URL = (
    "https://www.federalregister.gov/documents/2023/11/01/"
    "2023-28147/safe-secure-and-trustworthy-development-and-use-of-ai"
)
_DOC_API = (
    "https://www.federalregister.gov/api/v1/documents/2023-28147.json"
)

_DOC_PAYLOAD = {
    "document_number": "2023-28147",
    "title": "Safe, Secure, and Trustworthy Development and Use of AI",
    "abstract": "Short abstract about the executive order.",
    "publication_date": "2023-11-01",
    "html_url": _DOC_URL,
    "pdf_url": "https://www.federalregister.gov/d/2023-28147.pdf",
    "public_inspection_pdf_url": "",
    "type": "Presidential Document",
    "agencies": [{"name": "Executive Office of the President", "raw_name": "EOP"}],
    "significant": True,
    "body_html": (
        "<html><body><p>Section 1 of the executive order directs agencies "
        "to coordinate AI safety standards, model evaluations, and reporting "
        "requirements across the federal government.</p></body></html>"
    ),
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

    monkeypatch.setattr(fedregister.httpx, "AsyncClient", _client_factory)
    return captured


def _params_to_pairs(params) -> list[tuple[str, object]]:
    """Normalise the captured params (which are passed as a list of tuples) to a list."""
    if params is None:
        return []
    if isinstance(params, dict):
        return list(params.items())
    return list(params)


def _query_pairs_from_url(url: str) -> list[tuple[str, str]]:
    """Parse a URL's query string keeping bracketed keys literal.

    The connector now builds query strings by hand to keep ``conditions[term]``
    literal — ``urllib.parse.parse_qsl`` handles that fine, but we want the
    keys preserved verbatim (no decoding), so do the split manually.
    """
    query = urlsplit(url).query
    if not query:
        return []
    pairs: list[tuple[str, str]] = []
    for piece in query.split("&"):
        if not piece:
            continue
        if "=" not in piece:
            pairs.append((piece, ""))
            continue
        key, value = piece.split("=", 1)
        pairs.append((key, unquote_plus(value)))
    return pairs


def _captured_query_pairs(captured: dict, idx: int = 0) -> list[tuple[str, object]]:
    """Return query pairs from either captured ``params=`` kwargs or the URL."""
    params = captured["params"][idx] if captured["params"] else None
    if params is not None:
        return _params_to_pairs(params)
    return list(_query_pairs_from_url(captured["urls"][idx]))


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_builds_correct_query_and_headers(monkeypatch):
    payload = json.dumps(_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await fedregister.search(
        "AI executive order",
        since=date(2023, 1, 1),
        agencies=[
            "executive-office-of-the-president",
            "department-of-commerce",
        ],
    )

    assert len(results) == 2
    headers = captured["headers"][0]
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"]
    request_url = captured["urls"][0]
    assert request_url.startswith("https://www.federalregister.gov/api/v1/documents.json?")

    pairs = _captured_query_pairs(captured)
    keys = [k for k, _ in pairs]

    assert ("conditions[term]", "AI executive order") in pairs
    assert ("conditions[publication_date][gte]", "2023-01-01") in pairs

    agency_values = [v for k, v in pairs if k == "conditions[agencies][]"]
    assert agency_values == [
        "executive-office-of-the-president",
        "department-of-commerce",
    ]

    assert "per_page" in keys
    fields = [v for k, v in pairs if k == "fields[]"]
    assert "document_number" in fields
    assert "title" in fields
    assert "html_url" in fields
    assert "agencies" in fields
    assert "significant" in fields


async def test_search_parses_results(monkeypatch):
    payload = json.dumps(_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    results = await fedregister.search("AI executive order")

    first = results[0]
    assert first.source_kind == "fedregister"
    assert first.url == _DOC_URL
    assert first.title.startswith("Safe, Secure")
    assert "Executive Order" in first.snippet
    assert first.published_at is not None
    assert first.published_at.year == 2023
    assert first.extras["document_type"] == "Presidential Document"
    assert first.extras["document_number"] == "2023-28147"
    assert first.extras["significant"] is True
    assert first.extras["agencies"] == [
        "Executive Office of the President"
    ]

    second = results[1]
    # Empty abstract -> snippet falls back to the title.
    assert second.snippet == second.title
    assert second.extras["significant"] is False


async def test_search_without_since_or_agencies_omits_those_params(monkeypatch):
    payload = json.dumps({"results": []})

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await fedregister.search("anything")

    pairs = _captured_query_pairs(captured)
    keys = [k for k, _ in pairs]
    assert "conditions[publication_date][gte]" not in keys
    assert "conditions[agencies][]" not in keys


async def test_search_since_accepts_iso_string(monkeypatch):
    payload = json.dumps({"results": []})

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await fedregister.search("anything", since="2024-05-01")

    pairs = _captured_query_pairs(captured)
    assert ("conditions[publication_date][gte]", "2024-05-01") in pairs


async def test_search_url_keeps_literal_brackets(monkeypatch):
    """Federal Register's parser rejects ``%5B`` / ``%5D`` — keys must stay literal."""
    payload = json.dumps({"results": []})

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await fedregister.search(
        "AI executive order",
        since="2023-01-01",
        agencies=["executive-office-of-the-president"],
    )

    request_url = captured["urls"][0]
    assert "conditions[term]=" in request_url
    assert "conditions[publication_date][gte]=" in request_url
    assert "conditions[agencies][]=" in request_url
    assert "fields[]=" in request_url
    assert "%5B" not in request_url
    assert "%5D" not in request_url
    # The query value (a phrase with spaces) should still be percent-encoded.
    assert "AI+executive+order" in request_url or "AI%20executive%20order" in request_url


async def test_search_against_recorded_fixture(monkeypatch):
    """Loading a recorded Federal Register response yields parsed SearchResults."""
    fixture_path = FIXTURES_DIR / "fedregister_search_ai_eo.json"
    payload_text = fixture_path.read_text(encoding="utf-8")

    def _respond(url, params):
        return 200, payload_text

    _patch_httpx(monkeypatch, responder=_respond)

    results = await fedregister.search("AI executive order")

    assert len(results) >= 1
    first = results[0]
    assert first.source_kind == "fedregister"
    assert first.title
    assert first.url.startswith("https://www.federalregister.gov/documents/")
    assert first.published_at is not None
    assert first.extras.get("document_number")


async def test_search_returns_empty_on_500(monkeypatch):
    def _respond(url, params):
        return 500, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await fedregister.search("anything") == []


async def test_search_returns_empty_on_transport_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(fedregister.httpx, "AsyncClient", _client_factory)

    assert await fedregister.search("anything") == []


async def test_search_returns_empty_on_non_json(monkeypatch):
    def _respond(url, params):
        return 200, "<html>not json</html>"

    _patch_httpx(monkeypatch, responder=_respond)

    assert await fedregister.search("anything") == []


async def test_rate_limit_gate_sleeps_within_interval(monkeypatch):
    """Two concurrent search calls must both pass through the rate gate."""
    clock = [100.0]

    def fake_monotonic() -> float:
        return clock[0]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(fedregister.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(fedregister.asyncio, "sleep", fake_sleep)

    payload = json.dumps({"results": []})

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    await asyncio.gather(
        fedregister.search("a"), fedregister.search("b")
    )

    assert any(s > 0 for s in sleep_calls), (
        f"expected at least one >0 sleep through the rate gate; got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def test_fetch_happy_path_builds_markdown_and_caches(
    monkeypatch, cache_dir: Path
):
    doc_payload = json.dumps(_DOC_PAYLOAD)

    def _respond(url, params):
        if url == _DOC_API:
            return 200, doc_payload
        return 404, ""

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await fedregister.fetch(_DOC_URL)

    assert source is not None
    assert source.source_kind == "fedregister"
    assert source.url == _DOC_URL
    assert source.title.startswith("Safe, Secure")
    assert source.cleaned_text.startswith("# Safe, Secure")
    assert "Section 1 of the executive order" in source.cleaned_text
    assert "Presidential Document" in source.cleaned_text
    assert "2023-11-01" in source.cleaned_text
    assert "Executive Office of the President" in source.cleaned_text
    assert source.metadata["document_number"] == "2023-28147"
    assert source.metadata["significant"] is True
    assert source.metadata["agencies"] == [
        "Executive Office of the President"
    ]
    assert source.metadata["pdf_url"].endswith("2023-28147.pdf")

    cached = list(cache_dir.glob("doc-*.json"))
    assert len(cached) == 1

    # Second call serves from cache — no second HTTP hit.
    api_calls_before = [u for u in captured["urls"] if u == _DOC_API]
    s2 = await fedregister.fetch(_DOC_URL)
    assert s2 is not None
    api_calls_after = [u for u in captured["urls"] if u == _DOC_API]
    assert len(api_calls_after) == len(api_calls_before)


async def test_fetch_returns_none_for_unknown_host(
    monkeypatch, cache_dir: Path
):
    assert await fedregister.fetch("https://example.com/some-page") is None


async def test_fetch_rejects_lookalike_host(monkeypatch, cache_dir: Path):
    """A subdomain spoof like ``federalregister.gov.evil.example`` must not pass."""
    spoof = (
        "https://federalregister.gov.evil.example/"
        "documents/2023/11/01/2023-28147/safe-secure"
    )
    assert await fedregister.fetch(spoof) is None


async def test_fetch_returns_none_for_non_doc_path(
    monkeypatch, cache_dir: Path
):
    assert (
        await fedregister.fetch("https://www.federalregister.gov/agencies")
        is None
    )


async def test_fetch_returns_none_on_404(monkeypatch, cache_dir: Path):
    def _respond(url, params):
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await fedregister.fetch(_DOC_URL) is None


async def test_fetch_returns_none_on_transport_error(
    monkeypatch, cache_dir: Path
):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(fedregister.httpx, "AsyncClient", _client_factory)

    assert await fedregister.fetch(_DOC_URL) is None


async def test_fetch_falls_back_to_abstract_when_no_body_html(
    monkeypatch, cache_dir: Path
):
    payload = dict(_DOC_PAYLOAD)
    payload["body_html"] = ""
    payload["abstract"] = "Bare abstract text used because body_html is empty."

    def _respond(url, params):
        if url == _DOC_API:
            return 200, json.dumps(payload)
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    source = await fedregister.fetch(_DOC_URL)

    assert source is not None
    assert "Bare abstract text" in source.cleaned_text


# ---------------------------------------------------------------------------
# Smoke registration
# ---------------------------------------------------------------------------


def test_smoke_registry_includes_fedregister():
    from research_agent.tools import TOOL_REGISTRY

    assert "fedregister" in TOOL_REGISTRY
