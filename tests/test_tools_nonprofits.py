"""Tests for `research_agent.tools.nonprofits` (issue #100)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import nonprofits

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    nonprofits.reset_for_tests()
    monkeypatch.setattr(nonprofits.asyncio, "sleep", AsyncMock())
    yield
    nonprofits.reset_for_tests()


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "nonprofits-cache"
    monkeypatch.setattr(nonprofits, "_CACHE_DIR", target)
    return target


_SEARCH_PAYLOAD = {
    "total_results": 2,
    "organizations": [
        {
            "ein": 237327730,
            "strein": "23-7327730",
            "name": "Heritage Foundation",
            "sub_name": "Heritage Foundation",
            "city": "Washington",
            "state": "DC",
            "ntee_code": "W050",
            "raw_ntee_code": "W050",
            "subseccd": 3,
            "has_subseccd": True,
            "score": 63.94825,
        },
        {
            "ein": 411275875,
            "strein": "41-1275875",
            "name": "Heritage Foundation",
            "sub_name": "Heritage Foundation",
            "city": "Karlstad",
            "state": "MN",
            "ntee_code": None,
            "raw_ntee_code": None,
            "subseccd": 3,
            "has_subseccd": True,
            "score": 63.94825,
        },
    ],
}


_EIN_DIGITS = "237327730"
_ORG_URL = "https://projects.propublica.org/nonprofits/organizations/237327730"
_ORG_API = (
    "https://projects.propublica.org/nonprofits/api/v2/"
    "organizations/237327730.json"
)


_ORG_PAYLOAD = {
    "organization": {
        "id": 237327730,
        "ein": 237327730,
        "name": "Heritage Foundation",
        "careofname": "% TEST OFFICER",
        "address": "214 MASSACHUSETTS AVENUE NE",
        "city": "Washington",
        "state": "DC",
        "zipcode": "20002-4958",
        "subsection_code": 3,
        "classification_codes": "1000",
        "ntee_code": "W050",
        "exempt_organization_status_code": 1,
        "related_orgs": [
            {"name": "Heritage Action for America", "ein": "311660019"},
        ],
    },
    "filings_with_data": [
        {
            "tax_prd": 202212,
            "tax_prd_yr": 2022,
            "formtype": 0,
            "pdf_url": "https://example.invalid/990-2022.pdf",
            "updated": "2024-08-28T20:07:42.723Z",
            "totrevenue": 106329524,
            "totfuncexpns": 93668116,
            "totassetsend": 387665165,
            "totliabend": 55677294,
            "compnsatncurrofcr": 5944854,
            "pct_compnsatncurrofcr": 0.0,
        },
        {
            "tax_prd": 202112,
            "tax_prd_yr": 2021,
            "formtype": 0,
            "pdf_url": "https://example.invalid/990-2021.pdf",
            "updated": "2023-08-07T19:36:44.418Z",
            "totrevenue": 101783032,
            "totfuncexpns": 85809083,
            "totassetsend": 421262128,
            "totliabend": 53989966,
        },
    ],
    "filings_without_data": [
        {
            "tax_prd": 202312,
            "tax_prd_yr": 2023,
            "formtype": 0,
            "formtype_str": "990",
            "pdf_url": "https://example.invalid/990-2023.pdf",
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

    monkeypatch.setattr(nonprofits.httpx, "AsyncClient", _client_factory)
    return captured


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_builds_correct_query_and_headers(monkeypatch):
    payload = json.dumps(_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await nonprofits.search("Heritage Foundation")

    assert len(results) == 2
    headers = captured["headers"][0]
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"]
    assert captured["urls"][0].endswith("/api/v2/search.json")

    params = captured["params"][0]
    if isinstance(params, dict):
        assert params.get("q") == "Heritage Foundation"
    else:
        assert ("q", "Heritage Foundation") in list(params)


async def test_search_parses_organizations(monkeypatch):
    payload = json.dumps(_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    results = await nonprofits.search("Heritage Foundation")

    first = results[0]
    assert first.source_kind == "nonprofits"
    assert first.url == _ORG_URL
    assert first.title == "Heritage Foundation"
    assert first.published_at is None
    assert first.extras["ein"] == "23-7327730"
    assert first.extras["ein_digits"] == "237327730"
    assert first.extras["ntee_code"] == "W050"
    assert first.extras["city"] == "Washington"
    assert first.extras["state"] == "DC"
    assert first.extras["subsection_code"] == 3
    assert first.extras["subsection"] == "501(c)(3)"
    assert "Washington, DC" in first.snippet
    assert "501(c)(3)" in first.snippet
    assert "NTEE W050" in first.snippet

    second = results[1]
    # Missing NTEE should not crash; ein must still be set.
    assert second.extras["ein"] == "41-1275875"
    assert second.extras["ntee_code"] == ""


async def test_search_returns_empty_on_500(monkeypatch):
    def _respond(url, params):
        return 500, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await nonprofits.search("anything") == []


async def test_search_returns_empty_on_transport_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(nonprofits.httpx, "AsyncClient", _client_factory)

    assert await nonprofits.search("anything") == []


async def test_search_returns_empty_on_non_json(monkeypatch):
    def _respond(url, params):
        return 200, "<html>not json</html>"

    _patch_httpx(monkeypatch, responder=_respond)

    assert await nonprofits.search("anything") == []


async def test_rate_limit_gate_sleeps_within_interval(monkeypatch):
    """Two concurrent search calls must both pass through the rate gate."""
    clock = [100.0]

    def fake_monotonic() -> float:
        return clock[0]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(nonprofits.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(nonprofits.asyncio, "sleep", fake_sleep)

    payload = json.dumps({"organizations": []})

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    await asyncio.gather(
        nonprofits.search("a"), nonprofits.search("b")
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
    org_payload = json.dumps(_ORG_PAYLOAD)

    def _respond(url, params):
        if url == _ORG_API:
            return 200, org_payload
        return 404, ""

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await nonprofits.fetch(_ORG_URL)

    assert source is not None
    assert source.source_kind == "nonprofits"
    assert source.url == _ORG_URL
    assert source.title == "Heritage Foundation"
    body = source.cleaned_text
    assert body.startswith("# Heritage Foundation")
    assert "EIN 23-7327730" in body
    assert "501(c)(3)" in body
    assert "NTEE W050" in body
    assert "Washington, DC" in body
    # Latest filing summary present
    assert "Latest filing (FY 2022)" in body
    assert "$106,329,524" in body  # totrevenue
    assert "$5,944,854" in body  # top officer comp
    # Filing history (without_data 2023 + with_data 2022/2021) sorted desc by year
    assert "Filings" in body
    assert "FY 2023" in body
    assert "FY 2022" in body
    assert "FY 2021" in body
    # Related orgs surfaced when API supplies them
    assert "Heritage Action for America" in body

    # Metadata
    assert source.metadata["ein"] == "23-7327730"
    assert source.metadata["ein_digits"] == "237327730"
    assert source.metadata["ntee_code"] == "W050"
    assert source.metadata["subsection_code"] == 3
    assert source.metadata["subsection"] == "501(c)(3)"
    assert source.metadata["city"] == "Washington"
    assert source.metadata["state"] == "DC"
    assert source.metadata["latest_filing_year"] == 2022
    # AC: PDFs not auto-fetched but URLs surfaced for downstream extract_findings.
    filings = source.metadata["filings"]
    assert isinstance(filings, list)
    assert len(filings) == 3
    assert any(f["pdf_url"].endswith("990-2022.pdf") for f in filings)
    assert source.metadata["related_orgs"][0]["name"] == "Heritage Action for America"

    # Cache written
    cached = list(cache_dir.glob("org-*.json"))
    assert len(cached) == 1

    # Second call serves from cache — no second HTTP hit.
    api_calls_before = [u for u in captured["urls"] if u == _ORG_API]
    s2 = await nonprofits.fetch(_ORG_URL)
    assert s2 is not None
    api_calls_after = [u for u in captured["urls"] if u == _ORG_API]
    assert len(api_calls_after) == len(api_calls_before)


async def test_fetch_returns_none_for_unknown_host(monkeypatch, cache_dir: Path):
    assert await nonprofits.fetch("https://example.com/some-page") is None


async def test_fetch_rejects_lookalike_host(monkeypatch, cache_dir: Path):
    """A subdomain spoof like ``projects.propublica.org.evil.example`` must not pass."""
    spoof = (
        "https://projects.propublica.org.evil.example/"
        "nonprofits/organizations/237327730"
    )
    assert await nonprofits.fetch(spoof) is None


async def test_fetch_returns_none_for_non_org_path(monkeypatch, cache_dir: Path):
    assert (
        await nonprofits.fetch(
            "https://projects.propublica.org/nonprofits/search?q=heritage"
        )
        is None
    )


async def test_fetch_returns_none_on_404(monkeypatch, cache_dir: Path):
    def _respond(url, params):
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await nonprofits.fetch(_ORG_URL) is None


async def test_fetch_returns_none_on_transport_error(monkeypatch, cache_dir: Path):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(nonprofits.httpx, "AsyncClient", _client_factory)

    assert await nonprofits.fetch(_ORG_URL) is None


# ---------------------------------------------------------------------------
# Smoke registration
# ---------------------------------------------------------------------------


def test_smoke_registry_includes_nonprofits():
    from research_agent.tools import TOOL_REGISTRY

    assert "nonprofits" in TOOL_REGISTRY
