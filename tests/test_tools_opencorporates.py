"""Tests for `research_agent.tools.opencorporates` (issue #92)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import opencorporates

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    opencorporates.reset_for_tests()
    monkeypatch.setattr(opencorporates.asyncio, "sleep", AsyncMock())
    # Default: no API key set so anonymous-tier behaviour is exercised by
    # default. Tests that need a token set it explicitly via monkeypatch.
    monkeypatch.delenv("OPENCORPORATES_API_KEY", raising=False)
    yield
    opencorporates.reset_for_tests()


# ---------------------------------------------------------------------------
# Test payloads (modelled on OpenCorporates v0.4 envelopes)
# ---------------------------------------------------------------------------


_SEARCH_PAYLOAD = {
    "results": {
        "companies": [
            {
                "company": {
                    "name": "SBI BUILDERS, LLC",
                    "company_number": "201234567890",
                    "jurisdiction_code": "us_ca",
                    "current_status": "Active",
                    "company_type": "Limited Liability Company",
                    "incorporation_date": "2012-03-15",
                    "registered_address_in_full": (
                        "1234 Main St, San Jose, CA 95110, USA"
                    ),
                    "registered_agent_name": "Jane Q Agent",
                    "registered_agent_address": "5678 Agent Way, Sacramento, CA",
                    "opencorporates_url": (
                        "https://opencorporates.com/companies/us_ca/201234567890"
                    ),
                }
            },
            {
                "company": {
                    "name": "SBI BUILDERS INC",
                    "company_number": "C9876543",
                    "jurisdiction_code": "us_ca",
                    "current_status": "Dissolved",
                    "company_type": "Domestic Stock",
                    "incorporation_date": "1998-07-22",
                    "registered_address_in_full": "100 Old St, Oakland, CA",
                    "registered_agent_name": "",
                    "registered_agent_address": "",
                    "opencorporates_url": (
                        "https://opencorporates.com/companies/us_ca/C9876543"
                    ),
                }
            },
        ]
    }
}


_COMPANY_DETAIL_PAYLOAD = {
    "results": {
        "company": {
            "name": "SBI BUILDERS, LLC",
            "company_number": "201234567890",
            "jurisdiction_code": "us_ca",
            "current_status": "Active",
            "company_type": "Limited Liability Company",
            "incorporation_date": "2012-03-15",
            "registered_address_in_full": (
                "1234 Main St, San Jose, CA 95110, USA"
            ),
            "registered_agent": {
                "name": "Jane Q Agent",
                "address": "5678 Agent Way, Sacramento, CA",
            },
            "officers": [
                {
                    "officer": {
                        "name": "Alice Builder",
                        "position": "Manager",
                        "start_date": "2012-03-15",
                        "end_date": None,
                    }
                },
                {
                    "officer": {
                        "name": "Bob Builder",
                        "position": "Member",
                        "start_date": "2015-01-01",
                        "end_date": "2020-06-30",
                    }
                },
            ],
            "filings": [
                {
                    "filing": {
                        "title": "Statement of Information",
                        "filing_type_name": "SI-LLC",
                        "date": "2024-03-12",
                    }
                },
                {
                    "filing": {
                        "title": "LLC-12 Statement",
                        "filing_type_name": "Annual Report",
                        "date": "2023-03-08",
                    }
                },
            ],
            "previous_names": [
                {"company_name": "SBI Construction LLC"},
            ],
            "alternative_names": [
                {"name": "SBI Builders"},
            ],
        }
    }
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

    monkeypatch.setattr(opencorporates.httpx, "AsyncClient", _client_factory)
    return captured


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_returns_results(monkeypatch):
    """Happy path: search returns canonical fields including registered agent."""
    payload = json.dumps(_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await opencorporates.search("SBI Builders", max_results=5)

    assert captured["urls"][0].endswith("/companies/search")
    params = captured["params"][0]
    assert params["q"] == "SBI Builders"
    assert params["per_page"] == 5

    assert len(results) == 2
    top = results[0]
    assert top.source_kind == "opencorporates"
    assert top.title == "SBI BUILDERS, LLC"
    assert top.extras["company_number"] == "201234567890"
    assert top.extras["jurisdiction_code"] == "us_ca"
    assert top.extras["current_status"] == "Active"
    assert top.extras["registered_agent_name"] == "Jane Q Agent"
    assert top.extras["agent_address"] == "5678 Agent Way, Sacramento, CA"
    assert top.url == (
        "https://opencorporates.com/companies/us_ca/201234567890"
    )
    assert "us_ca" in top.snippet
    assert "Active" in top.snippet
    assert "Jane Q Agent" in top.snippet


async def test_search_with_jurisdiction_filter(monkeypatch):
    """``jurisdiction`` is forwarded as ``jurisdiction_code``."""
    payload = json.dumps(_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)
    await opencorporates.search("SBI Builders", jurisdiction="us_ca")
    params = captured["params"][0]
    assert params["jurisdiction_code"] == "us_ca"


async def test_search_no_key_omits_api_token(monkeypatch):
    """Anonymous-tier requests must not include ``api_token`` in params."""
    payload = json.dumps({"results": {"companies": []}})

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)
    await opencorporates.search("anything")
    params = captured["params"][0]
    assert "api_token" not in params


async def test_search_with_key_includes_api_token(monkeypatch):
    """When the env var is set, the token is sent as ``?api_token=...``."""
    payload = json.dumps({"results": {"companies": []}})

    def _respond(url, params):
        return 200, payload

    monkeypatch.setenv("OPENCORPORATES_API_KEY", "secret-token-abc")
    captured = _patch_httpx(monkeypatch, responder=_respond)

    await opencorporates.search("anything")

    params = captured["params"][0]
    assert params["api_token"] == "secret-token-abc"
    # API token must travel as a query param, NOT an Authorization header.
    headers = captured["headers"][0] or {}
    assert "Authorization" not in headers


async def test_search_returns_empty_on_500(monkeypatch):
    def _respond(url, params):
        return 500, ""

    _patch_httpx(monkeypatch, responder=_respond)
    assert await opencorporates.search("anything") == []


async def test_search_handles_non_json(monkeypatch):
    def _respond(url, params):
        return 200, "<html>not json</html>"

    _patch_httpx(monkeypatch, responder=_respond)
    assert await opencorporates.search("anything") == []


async def test_search_returns_empty_on_transport_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(opencorporates.httpx, "AsyncClient", _client_factory)
    assert await opencorporates.search("anything") == []


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def test_fetch_returns_source_with_officers_and_filings(monkeypatch):
    """Happy path: company detail renders agent/officers/filings/associated."""

    def _respond(url, params):
        if url.endswith("/companies/us_ca/201234567890"):
            return 200, json.dumps(_COMPANY_DETAIL_PAYLOAD)
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    url = "https://opencorporates.com/companies/us_ca/201234567890"
    source = await opencorporates.fetch(url)

    assert source is not None
    assert source.source_kind == "opencorporates"
    assert source.title == "SBI BUILDERS, LLC"
    assert source.url == url

    body = source.cleaned_text
    assert "# SBI BUILDERS, LLC" in body
    assert "us_ca" in body
    assert "Active" in body
    assert "## Registered agent" in body
    assert "Jane Q Agent" in body
    assert "5678 Agent Way" in body
    assert "## Officers" in body
    assert "Alice Builder" in body
    assert "Bob Builder" in body
    assert "Manager" in body
    assert "## Filings" in body
    assert "Statement of Information" in body
    assert "## Associated entities" in body
    assert "SBI Construction LLC" in body
    assert "SBI Builders" in body

    md = source.metadata
    assert md["company_number"] == "201234567890"
    assert md["jurisdiction_code"] == "us_ca"
    assert md["registered_agent"]["name"] == "Jane Q Agent"
    assert md["registered_agent"]["address"] == "5678 Agent Way, Sacramento, CA"
    assert md["registered_agent_name"] == "Jane Q Agent"
    assert len(md["officers"]) == 2
    assert md["officers"][0]["name"] == "Alice Builder"
    assert md["officers"][0]["position"] == "Manager"
    assert len(md["filings"]) == 2
    assert "SBI Construction LLC" in md["associated_entities"]
    assert "SBI Builders" in md["associated_entities"]


async def test_fetch_rejects_unknown_host(monkeypatch):
    """Look-alike hosts must be rejected without an HTTP call."""

    def _respond(url, params):
        raise AssertionError("should not be called")

    _patch_httpx(monkeypatch, responder=_respond)

    spoof = (
        "https://opencorporates.com.attacker.example/companies/us_ca/201234567890"
    )
    assert await opencorporates.fetch(spoof) is None


async def test_fetch_rejects_non_company_path(monkeypatch):
    """Non-company paths short-circuit to ``None``."""

    def _respond(url, params):
        raise AssertionError("should not be called")

    _patch_httpx(monkeypatch, responder=_respond)

    assert await opencorporates.fetch("https://opencorporates.com/about") is None
    assert (
        await opencorporates.fetch(
            "https://opencorporates.com/officers/12345"
        )
        is None
    )


async def test_fetch_returns_none_on_404(monkeypatch):
    def _respond(url, params):
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    url = "https://opencorporates.com/companies/us_ca/does-not-exist"
    assert await opencorporates.fetch(url) is None


async def test_fetch_returns_none_for_empty_url():
    assert await opencorporates.fetch("") is None


async def test_fetch_with_key_sends_api_token(monkeypatch):
    """When set, the token rides along on the detail call as well."""

    def _respond(url, params):
        return 200, json.dumps(_COMPANY_DETAIL_PAYLOAD)

    monkeypatch.setenv("OPENCORPORATES_API_KEY", "secret-token-abc")
    captured = _patch_httpx(monkeypatch, responder=_respond)

    url = "https://opencorporates.com/companies/us_ca/201234567890"
    source = await opencorporates.fetch(url)
    assert source is not None
    params = captured["params"][0]
    assert params is not None
    assert params["api_token"] == "secret-token-abc"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


async def test_rate_limit_gate_sleeps_between_calls(monkeypatch):
    """Two concurrent search calls must space out by ~2s (0.5 RPS)."""
    clock = [100.0]

    def fake_monotonic() -> float:
        return clock[0]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(opencorporates.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(opencorporates.asyncio, "sleep", fake_sleep)

    payload = json.dumps({"results": {"companies": []}})

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    await asyncio.gather(
        opencorporates.search("a"),
        opencorporates.search("b"),
    )

    assert any(abs(s - 2.0) < 1e-6 for s in sleep_calls), (
        f"expected a ~2s sleep through the rate gate; got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# Source kind literal & smoke registration
# ---------------------------------------------------------------------------


def test_source_kind_literal():
    """``opencorporates`` must be a valid SourceKind literal."""
    from research_agent.tools.models import SearchResult

    result = SearchResult(
        url="https://opencorporates.com/companies/us_ca/1",
        title="t",
        snippet="s",
        source_kind="opencorporates",
    )
    assert result.source_kind == "opencorporates"


def test_smoke_registry_includes_opencorporates():
    from research_agent.tools import TOOL_REGISTRY

    assert "opencorporates" in TOOL_REGISTRY
