"""Tests for `research_agent.tools.lda` (issue #103)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import lda

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.delenv("LDA_API_KEY", raising=False)
    lda.reset_for_tests()
    monkeypatch.setattr(lda.asyncio, "sleep", AsyncMock())
    yield
    lda.reset_for_tests()


# ---------------------------------------------------------------------------
# Test payloads
# ---------------------------------------------------------------------------


_FILINGS_PAYLOAD = {
    "results": [
        {
            "filing_uuid": "11111111-2222-3333-4444-555555555555",
            "filing_type": "Q1",
            "filing_type_display": "QUARTERLY REPORT",
            "filing_year": 2024,
            "filing_period": "first_quarter",
            "filing_period_display": "First Quarter",
            "dt_posted": "2024-04-20T15:30:00Z",
            "income": 250000.0,
            "expenses": None,
            "client": {"name": "HERITAGE FOUNDATION"},
            "registrant": {"name": "ACME LOBBYING LLC"},
            "filing_document_url": (
                "https://lda.senate.gov/filings/public/filing/"
                "11111111-2222-3333-4444-555555555555/print/"
            ),
        }
    ]
}


_REGISTRANTS_PAYLOAD = {
    "results": [
        {
            "id": 12345,
            "name": "ACME LOBBYING LLC",
            "address_1": "123 K Street NW",
            "city": "Washington",
            "state": "DC",
            "state_display": "DC",
            "zip": "20005",
            "country_display": "UNITED STATES OF AMERICA",
            "contact_name": "Jane Doe",
        }
    ]
}


_CONTRIBUTIONS_PAYLOAD = {
    "results": [
        {
            "filing_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "filer_type": "lobbyist",
            "filer_type_display": "Lobbyist",
            "filer_name": "JANE DOE",
            "filing_year": 2024,
            "filing_period": "mid_year",
            "filing_period_display": "Mid-Year",
            "contributions_total": 5000.0,
            "dt_posted": "2024-08-01T12:00:00Z",
        }
    ]
}


_LD2_DETAIL_PAYLOAD = {
    "filing_uuid": "11111111-2222-3333-4444-555555555555",
    "filing_type": "Q1",
    "filing_type_display": "QUARTERLY REPORT",
    "filing_year": 2024,
    "filing_period": "first_quarter",
    "filing_period_display": "First Quarter",
    "income": 250000.0,
    "expenses": None,
    "client": {"name": "HERITAGE FOUNDATION"},
    "registrant": {"name": "ACME LOBBYING LLC"},
    "lobbying_activities": [
        {
            "general_issue_code": "TAX",
            "general_issue_code_display": "Taxation/Internal Revenue Code",
            "description": "Tax reform legislation including HR 1234.",
            "lobbyists": [
                {
                    "lobbyist": {
                        "first_name": "Jane",
                        "last_name": "Doe",
                    }
                },
                {
                    "lobbyist": {
                        "first_name": "John",
                        "last_name": "Smith",
                    }
                },
            ],
        },
        {
            "general_issue_code": "BUD",
            "general_issue_code_display": "Budget/Appropriations",
            "description": "FY2024 appropriations.",
            "lobbyists": [
                {
                    "lobbyist": {
                        "first_name": "Jane",
                        "last_name": "Doe",
                    }
                },
            ],
        },
    ],
}


# ---------------------------------------------------------------------------
# httpx mock plumbing
# ---------------------------------------------------------------------------


def _patch_httpx(monkeypatch, *, responder):
    """Replace ``httpx.AsyncClient`` with a fake driven by ``responder(url, params)``.

    Returns a dict capturing urls / headers / params per call.
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

    monkeypatch.setattr(lda.httpx, "AsyncClient", _client_factory)
    return captured


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_filings_endpoint(monkeypatch):
    payload = json.dumps(_FILINGS_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await lda.search("Heritage Foundation", kind="filings")

    assert captured["urls"][0].endswith("/api/v1/filings/")
    params = captured["params"][0]
    assert params["registrant_name"] == "Heritage Foundation"
    assert params["page_size"] == 20

    assert len(results) == 1
    hit = results[0]
    assert hit.source_kind == "lda"
    assert hit.extras["filing_uuid"] == "11111111-2222-3333-4444-555555555555"
    assert hit.extras["client_name"] == "HERITAGE FOUNDATION"
    assert hit.extras["registrant_name"] == "ACME LOBBYING LLC"
    assert hit.extras["filing_year"] == 2024
    assert hit.extras["income"] == 250000.0
    assert "HERITAGE FOUNDATION" in hit.title
    assert "QUARTERLY REPORT" in hit.title
    assert "First Quarter" in hit.title
    assert "ACME LOBBYING LLC" in hit.snippet
    assert "$250,000" in hit.snippet
    # Permalink falls through to filing_document_url when present.
    assert hit.url.startswith("https://lda.senate.gov/")
    assert hit.published_at is not None


async def test_search_registrants_endpoint(monkeypatch):
    payload = json.dumps(_REGISTRANTS_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await lda.search("Acme Lobbying", kind="registrants", max_results=5)

    assert captured["urls"][0].endswith("/api/v1/registrants/")
    params = captured["params"][0]
    assert params["name"] == "Acme Lobbying"
    assert params["page_size"] == 5

    assert len(results) == 1
    hit = results[0]
    assert hit.title == "ACME LOBBYING LLC"
    assert hit.extras["registrant_id"] == 12345
    assert "123 K Street NW" in hit.extras["address"]
    assert "Washington" in hit.extras["address"]
    assert "DC" in hit.extras["address"]
    assert hit.extras["contact"] == "Jane Doe"
    assert hit.extras["country"] == "UNITED STATES OF AMERICA"


async def test_search_contributions_endpoint(monkeypatch):
    payload = json.dumps(_CONTRIBUTIONS_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await lda.search("Jane Doe", kind="contributions")

    assert captured["urls"][0].endswith("/api/v1/contributions/")
    params = captured["params"][0]
    assert params["filer_name"] == "Jane Doe"

    assert len(results) == 1
    hit = results[0]
    assert hit.extras["filing_uuid"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert hit.extras["filer_type"] == "Lobbyist"
    assert hit.extras["filer_name"] == "JANE DOE"
    assert hit.extras["contribution_total"] == 5000.0
    assert hit.extras["filing_year"] == 2024
    assert "$5,000" in hit.snippet
    assert hit.url.endswith("/filings/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/")


async def test_search_unknown_kind_returns_empty(monkeypatch):
    """Unknown kinds short-circuit to ``[]`` without an HTTP call."""
    called = {"count": 0}

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        called["count"] += 1

        class _Client:
            async def get(self, *_a, **_k):
                raise AssertionError("should not be called")

        yield _Client()

    monkeypatch.setattr(lda.httpx, "AsyncClient", _client_factory)

    assert await lda.search("anything", kind="bogus") == []
    assert called["count"] == 0


async def test_search_anonymous_no_auth_header(monkeypatch):
    """With LDA_API_KEY unset, requests must NOT include an Authorization header."""
    payload = json.dumps({"results": []})

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await lda.search("Anything", kind="filings")

    headers = captured["headers"][0] or {}
    assert "Authorization" not in headers


async def test_search_with_api_key_sends_token(monkeypatch):
    """With LDA_API_KEY set, requests carry ``Authorization: Token <key>``."""
    monkeypatch.setenv("LDA_API_KEY", "abc-test-key")
    payload = json.dumps({"results": []})

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await lda.search("Anything", kind="filings")

    headers = captured["headers"][0] or {}
    assert headers.get("Authorization") == "Token abc-test-key"


async def test_search_http_error_returns_empty(monkeypatch):
    """A non-200 response is logged and returns ``[]``."""

    def _respond(url, params):
        return 500, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await lda.search("anything", kind="filings") == []


async def test_search_transport_error_returns_empty(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(lda.httpx, "AsyncClient", _client_factory)

    assert await lda.search("anything", kind="filings") == []


async def test_search_non_json_returns_empty(monkeypatch):
    def _respond(url, params):
        return 200, "<html>not json</html>"

    _patch_httpx(monkeypatch, responder=_respond)

    assert await lda.search("anything", kind="filings") == []


async def test_rate_limit_gate_enforces_one_rps(monkeypatch):
    """Two concurrent search calls must space out by at least ~1s."""
    clock = [100.0]

    def fake_monotonic() -> float:
        return clock[0]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(lda.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(lda.asyncio, "sleep", fake_sleep)

    payload = json.dumps({"results": []})

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    await asyncio.gather(
        lda.search("a", kind="filings"), lda.search("b", kind="filings")
    )

    # At least one sleep should have been ~1.0s (the rate gate forcing the
    # second call to wait out the 1 RPS interval).
    assert any(abs(s - 1.0) < 1e-6 for s in sleep_calls), (
        f"expected a ~1s sleep through the rate gate; got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def test_fetch_ld2_filing_returns_markdown_sections(monkeypatch):
    """Happy path: detail JSON renders to markdown with all required sections."""

    def _respond(url, params):
        if "filings/11111111-2222-3333-4444-555555555555" in url:
            return 200, json.dumps(_LD2_DETAIL_PAYLOAD)
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    url = "https://lda.senate.gov/filings/11111111-2222-3333-4444-555555555555/"
    source = await lda.fetch(url)

    assert source is not None
    assert source.source_kind == "lda"
    assert source.url == url
    body = source.cleaned_text
    assert "HERITAGE FOUNDATION" in body
    assert "## Issues lobbied" in body
    assert "Taxation/Internal Revenue Code" in body
    assert "Budget/Appropriations" in body
    assert "## Lobbyists" in body
    assert "Jane Doe" in body
    assert "John Smith" in body
    # Jane Doe appears in two activities — assert de-duplication.
    assert body.count("- Jane Doe") == 1
    assert "## Amount" in body
    assert "$250,000" in body

    md = source.metadata
    assert md["filing_uuid"] == "11111111-2222-3333-4444-555555555555"
    assert md["client_name"] == "HERITAGE FOUNDATION"
    assert md["registrant_name"] == "ACME LOBBYING LLC"
    assert md["income"] == 250000.0
    assert md["expenses"] is None
    assert len(md["issues"]) == 2
    assert md["lobbyists"] == ["Jane Doe", "John Smith"]


async def test_fetch_rejects_non_lda_host(monkeypatch):
    """A look-alike host like ``lda.senate.gov.attacker.example`` must not pass."""

    def _respond(url, params):
        raise AssertionError("should not be called")

    _patch_httpx(monkeypatch, responder=_respond)

    spoof = (
        "https://lda.senate.gov.attacker.example/filings/"
        "11111111-2222-3333-4444-555555555555/"
    )
    assert await lda.fetch(spoof) is None


async def test_fetch_unknown_path_returns_none(monkeypatch):
    """Paths outside ``/filings/<uuid>/`` resolve to ``None``."""

    def _respond(url, params):
        raise AssertionError("should not be called")

    _patch_httpx(monkeypatch, responder=_respond)

    assert await lda.fetch("https://lda.senate.gov/api/v1/registrants/") is None


async def test_fetch_returns_none_for_empty_url():
    assert await lda.fetch("") is None


async def test_fetch_returns_none_on_404(monkeypatch):
    def _respond(url, params):
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    url = "https://lda.senate.gov/filings/11111111-2222-3333-4444-555555555555/"
    assert await lda.fetch(url) is None


async def test_fetch_returns_none_on_transport_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(lda.httpx, "AsyncClient", _client_factory)

    url = "https://lda.senate.gov/filings/11111111-2222-3333-4444-555555555555/"
    assert await lda.fetch(url) is None


async def test_fetch_accepts_lda_gov_cutover_host(monkeypatch):
    """The post-cutover ``lda.gov`` host is accepted alongside ``lda.senate.gov``."""

    def _respond(url, params):
        return 200, json.dumps(_LD2_DETAIL_PAYLOAD)

    _patch_httpx(monkeypatch, responder=_respond)

    url = "https://lda.gov/filings/11111111-2222-3333-4444-555555555555/"
    source = await lda.fetch(url)
    assert source is not None
    assert source.url == url


# ---------------------------------------------------------------------------
# Smoke registration
# ---------------------------------------------------------------------------


def test_smoke_registry_includes_lda():
    from research_agent.tools import TOOL_REGISTRY

    assert "lda" in TOOL_REGISTRY
