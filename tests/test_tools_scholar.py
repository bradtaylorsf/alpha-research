"""Tests for `research_agent.tools.scholar` (issue #114)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import scholar

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("SERPAPI_KEY", "serp-test-1234567890abcdef")
    yield


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    scholar.reset_for_tests()
    monkeypatch.setattr(scholar.asyncio, "sleep", AsyncMock())
    yield
    scholar.reset_for_tests()


# Sample SERPAPI google_scholar payload — case_law shape.
_SEARCH_PAYLOAD_CASES = {
    "search_metadata": {"id": "abc"},
    "organic_results": [
        {
            "position": 1,
            "title": "Lozman v. City of Riviera Beach",
            "result_id": "12345-lozman",
            "link": (
                "https://scholar.google.com/scholar_case?case=12345&hl=en"
            ),
            "snippet": (
                "First Amendment <em>retaliation</em> arrest claim — held "
                "probable cause does not bar the suit."
            ),
            "publication_info": {
                "summary": "Supreme Court, 2018 - scholar.google.com",
            },
            "inline_links": {
                "cited_by": {
                    "total": 142,
                    "link": "https://scholar.google.com/scholar?cites=12345",
                }
            },
            "resources": [
                {
                    "title": "supremecourt.gov",
                    "link": "https://www.supremecourt.gov/opinions/17pdf/17-21.pdf",
                    "file_format": "PDF",
                }
            ],
        },
        {
            "position": 2,
            "title": "Doe v. Roe",
            "result_id": "67890-doe",
            "link": "https://scholar.google.com/scholar_case?case=67890",
            "snippet": "9th Circuit retaliation case",
            "publication_info": {
                "summary": "9th Cir., 2021 - scholar.google.com",
            },
            "inline_links": {"cited_by": {"total": 8}},
        },
    ],
}


# Articles-shape payload — no court suffix, year embedded in publication info.
_SEARCH_PAYLOAD_ARTICLES = {
    "organic_results": [
        {
            "position": 1,
            "title": "Sample Paper",
            "result_id": "paper-1",
            "link": "https://example.org/paper.pdf",
            "snippet": "Abstract goes here",
            "publication_info": {"summary": "J Smith, A Jones - Nature, 2022"},
            "inline_links": {"cited_by": {"total": 3}},
        }
    ]
}


def _patch_httpx(monkeypatch, *, responder, head_responder=None):
    """Replace ``httpx.AsyncClient`` with a fake driven by ``responder``.

    ``responder(url, params)`` returns ``(status_code, body_text, headers)``.
    ``head_responder`` is invoked for ``client.head(url)``; falls back to
    ``responder`` when omitted.
    """
    captured: dict[str, list] = {
        "urls": [],
        "headers": [],
        "params": [],
        "head_urls": [],
    }

    class _FakeResp:
        def __init__(
            self,
            status: int,
            text: str,
            headers: dict[str, str] | None = None,
            content: bytes | None = None,
        ) -> None:
            self.status_code = status
            self.text = text
            self.headers = headers or {}
            self.content = content if content is not None else text.encode()

        def json(self):
            return json.loads(self.text)

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        captured["headers"].append(kwargs.get("headers"))

        class _Client:
            async def get(self, url, *, params=None, **_kwargs):
                captured["urls"].append(url)
                captured["params"].append(params)
                result = responder(url, params)
                if len(result) == 3:
                    status, text, headers = result
                    return _FakeResp(status, text, headers)
                if len(result) == 4:
                    status, text, headers, content = result
                    return _FakeResp(status, text, headers, content)
                status, text = result
                return _FakeResp(status, text)

            async def head(self, url, **_kwargs):
                captured["head_urls"].append(url)
                fn = head_responder or responder
                result = fn(url, None)
                if len(result) == 3:
                    status, text, headers = result
                    return _FakeResp(status, text, headers)
                status, text = result
                return _FakeResp(status, text)

        yield _Client()

    monkeypatch.setattr(scholar.httpx, "AsyncClient", _client_factory)
    return captured


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_case_law_includes_as_sdt_and_parses_fields(monkeypatch):
    payload = json.dumps(_SEARCH_PAYLOAD_CASES)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await scholar.search(
        "first amendment retaliation Ninth Circuit", kind="case_law"
    )

    assert len(results) == 2
    params = captured["params"][0]
    assert params["engine"] == "google_scholar"
    assert params["q"] == "first amendment retaliation Ninth Circuit"
    assert params["api_key"] == "serp-test-1234567890abcdef"
    assert params["as_sdt"] == "2006"
    assert captured["urls"][0] == scholar._BASE_URL

    first = results[0]
    assert first.source_kind == "scholar"
    assert first.url == (
        "https://scholar.google.com/scholar_case?case=12345&hl=en"
    )
    assert first.title == "Lozman v. City of Riviera Beach"
    # Snippet is HTML-stripped.
    assert "<em>" not in first.snippet
    assert "retaliation" in first.snippet
    assert first.published_at is not None
    assert first.published_at.year == 2018
    assert first.extras["kind"] == "case_law"
    assert "Supreme Court" in first.extras["court_or_journal"]
    assert first.extras["citation"] == 142
    assert first.extras["result_id"] == "12345-lozman"
    assert first.extras["resources"][0]["file_format"] == "PDF"


async def test_search_articles_omits_as_sdt(monkeypatch):
    payload = json.dumps(_SEARCH_PAYLOAD_ARTICLES)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await scholar.search("attention is all you need", kind="articles")

    params = captured["params"][0]
    assert "as_sdt" not in params
    assert params["engine"] == "google_scholar"
    assert len(results) == 1
    assert results[0].extras["kind"] == "articles"
    assert results[0].published_at is not None
    assert results[0].published_at.year == 2022


async def test_search_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown kind"):
        await scholar.search("anything", kind="bogus")


async def test_search_returns_empty_on_500(monkeypatch):
    def _respond(url, params):
        return 500, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await scholar.search("anything") == []


async def test_search_returns_empty_on_non_json(monkeypatch):
    def _respond(url, params):
        return 200, "<html>not json</html>"

    _patch_httpx(monkeypatch, responder=_respond)

    assert await scholar.search("anything") == []


async def test_search_returns_empty_on_transport_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(scholar.httpx, "AsyncClient", _client_factory)

    assert await scholar.search("anything") == []


async def test_search_raises_when_key_missing(monkeypatch):
    monkeypatch.delenv("SERPAPI_KEY", raising=False)

    with pytest.raises(RuntimeError, match="serpapi.com"):
        await scholar.search("anything")


async def test_search_max_results_caps_output(monkeypatch):
    payload = json.dumps(_SEARCH_PAYLOAD_CASES)

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    results = await scholar.search("anything", max_results=1)

    assert len(results) == 1


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


_HTML_BODY = (
    "<html>"
    "<head>"
    '<meta property="og:title" content="Sample Opinion"/>'
    "<title>fallback title</title>"
    "</head>"
    "<body>"
    "<article><p>This is the body of the opinion. It is long enough that"
    " trafilatura's recall mode will pick it up as the main content of the"
    " page rather than discarding the document.</p></article>"
    "</body>"
    "</html>"
)


async def test_fetch_html_returns_source(monkeypatch):
    def _respond(url, params):
        return 200, _HTML_BODY, {"content-type": "text/html; charset=utf-8"}

    _patch_httpx(monkeypatch, responder=_respond)

    source = await scholar.fetch("https://scholar.google.com/example")

    assert source is not None
    assert source.source_kind == "scholar"
    assert source.url == "https://scholar.google.com/example"
    assert source.title == "Sample Opinion"
    assert "body of the opinion" in source.cleaned_text
    assert source.metadata["content_type"].startswith("text/html")


async def test_fetch_pdf_routes_through_pdf_module(monkeypatch):
    extracted = "## Page 1\n\nMarkdown rendition of the PDF body."

    async def _fake_extract(url, **_kwargs):
        return extracted

    monkeypatch.setattr(scholar.pdf_tool, "extract", _fake_extract)

    def _respond(url, params):
        # HEAD returns Content-Type: application/pdf so the fetch short-circuits
        # before the full GET.
        return 200, "", {"content-type": "application/pdf"}

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await scholar.fetch("https://example.org/doc.pdf")

    assert source is not None
    assert source.source_kind == "scholar"
    assert source.cleaned_text == extracted
    assert source.metadata["content_type"] == "application/pdf"
    # We never issued a GET — only a HEAD probe.
    assert captured["head_urls"] == ["https://example.org/doc.pdf"]
    assert captured["urls"] == []


async def test_fetch_pdf_via_url_extension_when_head_blocked(monkeypatch):
    extracted = "## Page 1\n\nFallback PDF body."

    async def _fake_extract(url, **_kwargs):
        return extracted

    monkeypatch.setattr(scholar.pdf_tool, "extract", _fake_extract)

    def _respond(url, params):
        # HEAD returns 405; the URL ends with .pdf so we still treat as PDF.
        return 405, "", {}

    _patch_httpx(monkeypatch, responder=_respond)

    source = await scholar.fetch("https://example.org/paper.pdf")

    assert source is not None
    assert source.cleaned_text == extracted
    assert source.metadata["content_type"] == "application/pdf"


async def test_fetch_returns_none_on_http_error(monkeypatch):
    def _respond(url, params):
        return 500, "", {"content-type": "text/html"}

    _patch_httpx(monkeypatch, responder=_respond)

    source = await scholar.fetch("https://scholar.google.com/example")
    assert source is None


async def test_fetch_returns_none_on_transport_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def head(self, *_a, **_k):
                raise httpx.ConnectError("nope")

            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(scholar.httpx, "AsyncClient", _client_factory)

    assert await scholar.fetch("https://scholar.google.com/example") is None


async def test_fetch_empty_url_returns_none():
    assert await scholar.fetch("") is None


# ---------------------------------------------------------------------------
# Smoke registration
# ---------------------------------------------------------------------------


def test_smoke_registry_includes_scholar():
    from research_agent.tools import TOOL_REGISTRY

    assert "scholar" in TOOL_REGISTRY
