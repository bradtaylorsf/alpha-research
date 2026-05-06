"""Tests for `research_agent.tools.linkedin` (issue #115)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import linkedin

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("LINKEDIN_DATA_API_KEY", "proxycurl-test-1234567890abcdef")
    monkeypatch.delenv("LINKEDIN_BROKER", raising=False)
    monkeypatch.delenv("LIX_API_KEY", raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    linkedin.reset_for_tests()
    monkeypatch.setattr(linkedin.asyncio, "sleep", AsyncMock())
    yield
    linkedin.reset_for_tests()


_PERSON_SEARCH_PAYLOAD = {
    "results": [
        {
            "linkedin_profile_url": "https://www.linkedin.com/in/george-santos-1/",
            "profile": {
                "first_name": "George",
                "last_name": "Santos",
                "full_name": "George Santos",
                "headline": "U.S. Representative-Elect for NY-03",
                "city": "New York",
                "country_full_name": "United States",
                "occupation": "U.S. Representative-Elect at U.S. House",
                "experiences": [
                    {
                        "title": "Representative",
                        "company": "U.S. House of Representatives",
                        "starts_at": {"year": 2023, "month": 1},
                        "ends_at": None,
                    }
                ],
            },
        },
        {
            "linkedin_profile_url": "https://www.linkedin.com/in/george-santos-2/",
            "profile": {
                "full_name": "George Santos",
                "headline": "Independent Consultant",
                "city": "Queens, NY",
                "occupation": "Consultant",
                "experiences": [
                    {
                        "title": "Consultant",
                        "company": "Self Employed",
                        "starts_at": {"year": 2020, "month": 5},
                        "ends_at": {"year": 2022, "month": 12},
                    }
                ],
            },
        },
    ]
}


_COMPANY_SEARCH_PAYLOAD = {
    "results": [
        {
            "linkedin_profile_url": "https://www.linkedin.com/company/anthropic/",
            "profile": {
                "name": "Anthropic",
                "tagline": "AI safety company",
                "industry": "Research",
                "company_size": [501, 1000],
                "hq": {
                    "city": "San Francisco",
                    "state": "California",
                    "country": "US",
                },
            },
        }
    ]
}


_FETCH_PERSON_PAYLOAD = {
    "public_identifier": "george-santos-1",
    "full_name": "George Santos",
    "headline": "U.S. Representative-Elect",
    "country_full_name": "United States",
    "city": "New York",
    "experiences": [
        {
            "title": "Representative",
            "company": "U.S. House of Representatives",
            "starts_at": {"year": 2023, "month": 1},
            "ends_at": None,
            "description": "Member of Congress representing NY-03.",
            "location": "Washington, DC",
        },
        {
            "title": "Founder",
            "company": "Devolder Organization",
            "starts_at": {"year": 2021, "month": 5},
            "ends_at": {"year": 2022, "month": 11},
            "description": "Family LLC.",
            "location": "New York",
        },
    ],
    "education": [
        {
            "school": "Baruch College",
            "degree_name": "BA",
            "field_of_study": "Finance",
            "starts_at": {"year": 2010},
            "ends_at": {"year": 2014},
        }
    ],
    "certifications": [
        {
            "name": "FINRA Series 7",
            "authority": "FINRA",
            "starts_at": {"year": 2014},
            "ends_at": None,
        }
    ],
    "skills": ["Public Speaking", "Finance"],
}


_FETCH_COMPANY_PAYLOAD = {
    "linkedin_internal_id": "12345",
    "name": "Anthropic",
    "tagline": "AI safety company",
    "industry": "Research Services",
    "description": "Anthropic builds reliable, interpretable, steerable AI.",
    "company_size": [501, 1000],
    "employee_count": 850,
    "hq": {"city": "San Francisco", "state": "California", "country": "US"},
    "locations": [
        {"city": "San Francisco", "state": "California", "country": "US"},
        {"city": "London", "country": "GB"},
    ],
    "specialities": ["AI safety", "Large language models"],
    "updates": [{"text": "We released a new model."}],
}


def _patch_httpx(monkeypatch, *, responder):
    captured: dict[str, list] = {
        "urls": [],
        "headers": [],
        "params": [],
    }

    class _FakeResp:
        def __init__(self, status: int, text: str, headers: dict[str, str] | None = None) -> None:
            self.status_code = status
            self.text = text
            self.headers = headers or {}
            self.content = text.encode()

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
                status, text = result
                return _FakeResp(status, text)

        yield _Client()

    monkeypatch.setattr(linkedin.httpx, "AsyncClient", _client_factory)
    return captured


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_person_parses_proxycurl_payload(monkeypatch):
    payload = json.dumps(_PERSON_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await linkedin.search("George Santos", kind="person")

    assert len(results) == 2
    assert captured["urls"][0] == linkedin._PROXYCURL_PERSON_SEARCH
    params = captured["params"][0]
    assert params["first_name"] == "George"
    assert params["last_name"] == "Santos"
    assert params["country"] == "us"
    headers = captured["headers"][0]
    assert headers["Authorization"].startswith("Bearer proxycurl-test-")

    first = results[0]
    assert first.source_kind == "linkedin"
    assert first.url == "https://www.linkedin.com/in/george-santos-1/"
    assert first.title == "George Santos"
    assert "Representative-Elect" in first.snippet
    assert first.extras["kind"] == "person"
    assert first.extras["broker"] == "proxycurl"
    assert first.extras["location"].startswith("New York")
    assert first.extras["current_company"] == "U.S. House of Representatives"
    assert first.extras["current_title"] == "Representative"


async def test_search_company_hits_company_endpoint(monkeypatch):
    payload = json.dumps(_COMPANY_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await linkedin.search("Anthropic", kind="company")

    assert captured["urls"][0] == linkedin._PROXYCURL_COMPANY_SEARCH
    assert captured["params"][0]["name"] == "Anthropic"
    assert len(results) == 1
    hit = results[0]
    assert hit.title == "Anthropic"
    assert hit.extras["kind"] == "company"
    assert hit.extras["industry"] == "Research"
    assert hit.extras["headcount"] == "501-1000"
    assert "San Francisco" in hit.extras["hq_location"]


async def test_search_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown kind"):
        await linkedin.search("anything", kind="bogus")


async def test_search_raises_when_key_missing(monkeypatch):
    monkeypatch.delenv("LINKEDIN_DATA_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="LINKEDIN_DATA_API_KEY"):
        await linkedin.search("anyone")


async def test_search_returns_empty_on_500(monkeypatch):
    def _respond(url, params):
        return 500, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await linkedin.search("anyone") == []


async def test_search_returns_empty_on_non_json(monkeypatch):
    def _respond(url, params):
        return 200, "<html>not json</html>"

    _patch_httpx(monkeypatch, responder=_respond)

    assert await linkedin.search("anyone") == []


async def test_search_returns_empty_on_transport_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(linkedin.httpx, "AsyncClient", _client_factory)

    assert await linkedin.search("anyone") == []


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def test_fetch_person_returns_markdown_rollup(monkeypatch):
    payload = json.dumps(_FETCH_PERSON_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await linkedin.fetch("https://www.linkedin.com/in/george-santos-1/")

    assert source is not None
    assert source.source_kind == "linkedin"
    assert source.title == "George Santos"
    assert "## Experience" in source.cleaned_text
    assert "Representative @ U.S. House of Representatives" in source.cleaned_text
    assert "## Education" in source.cleaned_text
    assert "Baruch College" in source.cleaned_text
    assert "## Certifications" in source.cleaned_text
    assert source.metadata["broker"] == "proxycurl"
    assert source.metadata["broker_payload"]["full_name"] == "George Santos"
    assert source.metadata["profile_url"].endswith("/george-santos-1/")
    assert len(source.metadata["employment_history"]) == 2
    assert source.metadata["education"][0]["school"] == "Baruch College"

    assert captured["urls"][0] == linkedin._PROXYCURL_PERSON_FETCH
    assert captured["params"][0]["url"].endswith("/george-santos-1/")


async def test_fetch_company_returns_markdown_rollup(monkeypatch):
    payload = json.dumps(_FETCH_COMPANY_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await linkedin.fetch("https://www.linkedin.com/company/anthropic/")

    assert source is not None
    assert source.source_kind == "linkedin"
    assert source.title == "Anthropic"
    assert "Industry: Research Services" in source.cleaned_text
    assert "Employees: 850" in source.cleaned_text
    assert "## Locations" in source.cleaned_text
    assert "London" in source.cleaned_text
    assert "## Specialities" in source.cleaned_text
    assert source.metadata["industry"] == "Research Services"
    assert source.metadata["employee_count"] == 850
    assert source.metadata["broker"] == "proxycurl"

    assert captured["urls"][0] == linkedin._PROXYCURL_COMPANY_FETCH


async def test_fetch_non_linkedin_url_returns_none(monkeypatch):
    # No HTTP call should fire — assertion-by-absence: if it did, the
    # responder would never be invoked anyway, but we verify None.
    source = await linkedin.fetch("https://example.org/some-page")
    assert source is None


async def test_fetch_unsupported_linkedin_url_returns_none(monkeypatch):
    # /pulse/<slug> is not /in/ or /company/ — connector should bail.
    source = await linkedin.fetch("https://www.linkedin.com/pulse/some-article/")
    assert source is None


async def test_fetch_returns_none_on_http_error(monkeypatch):
    def _respond(url, params):
        return 500, ""

    _patch_httpx(monkeypatch, responder=_respond)

    source = await linkedin.fetch("https://www.linkedin.com/in/someone/")
    assert source is None


async def test_fetch_returns_none_on_transport_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(linkedin.httpx, "AsyncClient", _client_factory)

    assert await linkedin.fetch("https://www.linkedin.com/in/someone/") is None


# ---------------------------------------------------------------------------
# Broker switch
# ---------------------------------------------------------------------------


async def test_search_routes_to_lix_when_broker_set(monkeypatch):
    monkeypatch.setenv("LINKEDIN_BROKER", "lix")
    monkeypatch.setenv("LIX_API_KEY", "lix-test-key")
    monkeypatch.delenv("LINKEDIN_DATA_API_KEY", raising=False)

    payload = json.dumps(
        {
            "people": [
                {
                    "link": "https://www.linkedin.com/in/jane-doe/",
                    "name": "Jane Doe",
                    "headline": "Software Engineer",
                    "location": "Brooklyn, NY",
                    "company": "Acme",
                    "position": "Software Engineer",
                }
            ]
        }
    )

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await linkedin.search("Jane Doe", kind="person")

    assert captured["urls"][0] == linkedin._LIX_PERSON_SEARCH
    headers = captured["headers"][0]
    # Lix uses raw token in Authorization header (no Bearer prefix).
    assert headers["Authorization"] == "lix-test-key"
    assert len(results) == 1
    hit = results[0]
    assert hit.extras["broker"] == "lix"
    assert hit.extras["current_company"] == "Acme"


async def test_search_unknown_broker_raises(monkeypatch):
    monkeypatch.setenv("LINKEDIN_BROKER", "wonkacurl")

    with pytest.raises(RuntimeError, match="Unknown LINKEDIN_BROKER"):
        await linkedin.search("anyone")


async def test_search_lix_broker_missing_key_raises(monkeypatch):
    monkeypatch.setenv("LINKEDIN_BROKER", "lix")
    monkeypatch.delenv("LIX_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="LIX_API_KEY"):
        await linkedin.search("anyone")


# ---------------------------------------------------------------------------
# Smoke registration
# ---------------------------------------------------------------------------


def test_smoke_registry_includes_linkedin():
    from research_agent.tools import TOOL_REGISTRY

    assert "linkedin" in TOOL_REGISTRY
