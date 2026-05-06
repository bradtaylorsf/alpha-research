"""Tests for `research_agent.tools.fec` (issue #94)."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import fec

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("DATA_GOV_API_KEY", "test-key-1234567890abcdef")
    yield


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    fec.reset_for_tests()
    monkeypatch.setattr(fec.asyncio, "sleep", AsyncMock())
    yield
    fec.reset_for_tests()


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "fec-cache"
    monkeypatch.setattr(fec, "_CACHE_DIR", target)
    return target


# ---------------------------------------------------------------------------
# Test payloads
# ---------------------------------------------------------------------------


_CANDIDATE_SEARCH_PAYLOAD = {
    "results": [
        {
            "candidate_id": "H0NY03169",
            "name": "SANTOS, GEORGE",
            "party": "REP",
            "state": "NY",
            "office": "H",
            "office_full": "House",
            "incumbent_challenge_full": "Incumbent",
            "election_years": [2020, 2022, 2024],
        },
        {
            "candidate_id": "H8NY03000",
            "name": "DOE, JANE",
            "party": "DEM",
            "state": "NY",
            "office": "H",
            "office_full": "House",
            "incumbent_challenge_full": "Challenger",
            "election_years": [2024],
        },
    ]
}


_COMMITTEE_SEARCH_PAYLOAD = {
    "results": [
        {
            "committee_id": "C00500587",
            "name": "MAKE AMERICA GREAT AGAIN PAC",
            "committee_type_full": "Political Action Committee",
            "designation_full": "Lobbyist/Registrant PAC",
            "organization_type_full": "",
            "party_full": "",
            "state": "FL",
        }
    ]
}


_SCHEDULE_A_PAYLOAD = {
    "results": [
        {
            "contributor_name": "SMITH, JOHN A.",
            "contribution_receipt_amount": 2900.0,
            "contribution_receipt_date": "2024-04-15",
            "contributor_employer": "ACME CORP",
            "contributor_occupation": "EXECUTIVE",
            "committee": {"name": "SANTOS FOR CONGRESS"},
            "committee_id": "C00712641",
        }
    ]
}


_SCHEDULE_E_PAYLOAD = {
    "results": [
        {
            "payee_name": "PATRIOT MEDIA LLC",
            "expenditure_amount": 50000.0,
            "expenditure_date": "2024-09-15",
            "candidate_name": "SMITH, JANE",
            "support_oppose_indicator": "S",
            "committee_id": "C00712641",
        }
    ]
}


_CANDIDATE_HEADER_PAYLOAD = {
    "results": [
        {
            "candidate_id": "H0NY03169",
            "name": "SANTOS, GEORGE",
            "party_full": "REPUBLICAN PARTY",
            "party": "REP",
            "state": "NY",
            "office_full": "House",
            "office": "H",
            "incumbent_challenge_full": "Incumbent",
            "election_years": [2020, 2022, 2024],
        }
    ]
}


_CANDIDATE_TOTALS_PAYLOAD = {
    "results": [
        {
            "candidate_id": "H0NY03169",
            "cycle": 2024,
            "receipts": 1234567.0,
            "disbursements": 1100000.0,
            "cash_on_hand_end_period": 134567.0,
        }
    ]
}


_CANDIDATE_DONORS_PAYLOAD = {
    "results": [
        {"employer": "ACME CORP", "total": 25000.0},
        {"employer": "WIDGET INC", "total": 18500.0},
    ]
}


_CANDIDATE_PURPOSES_PAYLOAD = {
    "results": [
        {"purpose": "Media", "total": 600000.0},
        {"purpose": "Travel", "total": 75000.0},
    ]
}


_COMMITTEE_HEADER_PAYLOAD = {
    "results": [
        {
            "committee_id": "C00500587",
            "name": "MAKE AMERICA GREAT AGAIN PAC",
            "committee_type_full": "Political Action Committee",
            "designation_full": "Lobbyist/Registrant PAC",
            "party_full": "",
            "party": "",
            "state": "FL",
            "organization_type_full": "",
            "cycles": [2018, 2020, 2022, 2024],
        }
    ]
}


_COMMITTEE_TOTALS_PAYLOAD = {
    "results": [
        {
            "committee_id": "C00500587",
            "cycle": 2024,
            "receipts": 9_999_999.0,
            "disbursements": 8_888_888.0,
            "cash_on_hand_end_period": 1_111_111.0,
        }
    ]
}


# ---------------------------------------------------------------------------
# httpx mock plumbing
# ---------------------------------------------------------------------------


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

    monkeypatch.setattr(fec.httpx, "AsyncClient", _client_factory)
    return captured


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_candidates_builds_correct_query(monkeypatch):
    payload = json.dumps(_CANDIDATE_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await fec.search("George Santos", kind="candidates")

    assert len(results) == 2
    assert captured["urls"][0].endswith("/v1/candidates/search/")
    params = captured["params"][0]
    assert params["q"] == "George Santos"
    assert params["api_key"] == "test-key-1234567890abcdef"
    assert params["per_page"] == 20

    first = results[0]
    assert first.source_kind == "fec"
    assert first.url == "https://www.fec.gov/data/candidate/H0NY03169/"
    assert first.title == "SANTOS, GEORGE"
    assert first.extras["candidate_id"] == "H0NY03169"
    assert first.extras["party"] == "REP"
    assert first.extras["state"] == "NY"
    assert first.extras["office"] == "House"
    assert first.extras["election_years"] == [2020, 2022, 2024]
    assert "REP" in first.snippet
    assert "NY" in first.snippet


async def test_search_committees_endpoint(monkeypatch):
    payload = json.dumps(_COMMITTEE_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await fec.search("MAGA PAC", kind="committees", max_results=5)

    assert captured["urls"][0].endswith("/v1/committees/")
    params = captured["params"][0]
    assert params["q"] == "MAGA PAC"
    assert params["per_page"] == 5

    assert len(results) == 1
    hit = results[0]
    assert hit.url == "https://www.fec.gov/data/committee/C00500587/"
    assert hit.extras["committee_id"] == "C00500587"
    assert hit.extras["committee_type_full"] == "Political Action Committee"
    assert hit.extras["state"] == "FL"


async def test_search_schedule_a_endpoint(monkeypatch):
    payload = json.dumps(_SCHEDULE_A_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await fec.search("John Smith", kind="schedules/schedule_a")

    assert captured["urls"][0].endswith("/v1/schedules/schedule_a/")
    params = captured["params"][0]
    assert params["contributor_name"] == "John Smith"

    assert len(results) == 1
    hit = results[0]
    assert hit.source_kind == "fec"
    assert hit.extras["amount"] == 2900.0
    assert hit.extras["contributor_name"] == "SMITH, JOHN A."
    assert hit.extras["contributor_employer"] == "ACME CORP"
    assert hit.extras["recipient_name"] == "SANTOS FOR CONGRESS"
    assert hit.extras["contribution_receipt_date"] == "2024-04-15"
    assert "$2,900" in hit.snippet


async def test_search_schedule_e_endpoint(monkeypatch):
    payload = json.dumps(_SCHEDULE_E_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await fec.search("Patriot Media", kind="schedules/schedule_e")

    assert captured["urls"][0].endswith("/v1/schedules/schedule_e/")
    params = captured["params"][0]
    assert params["payee_name"] == "Patriot Media"

    assert len(results) == 1
    hit = results[0]
    assert hit.extras["expenditure_amount"] == 50000.0
    assert hit.extras["payee_name"] == "PATRIOT MEDIA LLC"
    assert hit.extras["candidate_name"] == "SMITH, JANE"
    assert hit.extras["support_oppose_indicator"] == "S"
    assert "Support" in hit.snippet
    assert hit.url == "https://www.fec.gov/data/committee/C00712641/"


async def test_search_unknown_kind_returns_empty(monkeypatch):
    # Should not even call httpx — emit warning + return [].
    called = {"count": 0}

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        called["count"] += 1

        class _Client:
            async def get(self, *_a, **_k):
                raise AssertionError("should not be called")

        yield _Client()

    monkeypatch.setattr(fec.httpx, "AsyncClient", _client_factory)

    assert await fec.search("anything", kind="bogus") == []
    assert called["count"] == 0


async def test_search_returns_empty_on_500(monkeypatch):
    def _respond(url, params):
        return 500, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await fec.search("anything") == []


async def test_search_returns_empty_on_transport_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(fec.httpx, "AsyncClient", _client_factory)

    assert await fec.search("anything") == []


async def test_search_returns_empty_on_non_json(monkeypatch):
    def _respond(url, params):
        return 200, "<html>not json</html>"

    _patch_httpx(monkeypatch, responder=_respond)

    assert await fec.search("anything") == []


async def test_demo_key_fallback_when_unset(monkeypatch):
    monkeypatch.delenv("DATA_GOV_API_KEY", raising=False)
    payload = json.dumps({"results": []})

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await fec.search("anything")

    assert captured["params"][0]["api_key"] == "DEMO_KEY"


async def test_rate_limit_gate_sleeps_within_interval(monkeypatch):
    """Two concurrent search calls must both pass through the rate gate."""
    clock = [100.0]

    def fake_monotonic() -> float:
        return clock[0]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(fec.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(fec.asyncio, "sleep", fake_sleep)

    payload = json.dumps({"results": []})

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    await asyncio.gather(fec.search("a"), fec.search("b"))

    assert any(s > 0 for s in sleep_calls), (
        f"expected at least one >0 sleep through the rate gate; got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# fetch() — URL classifier
# ---------------------------------------------------------------------------


async def test_fetch_returns_none_for_unknown_host(cache_dir: Path):
    assert await fec.fetch("https://example.com/data/candidate/H0NY03169/") is None


async def test_fetch_rejects_lookalike_host(cache_dir: Path):
    spoof = "https://www.fec.gov.evil.example/data/candidate/H0NY03169/"
    assert await fec.fetch(spoof) is None


async def test_fetch_returns_none_for_non_resource_path(cache_dir: Path):
    assert await fec.fetch("https://www.fec.gov/data/browse-data/") is None


async def test_fetch_returns_none_for_empty_url(cache_dir: Path):
    assert await fec.fetch("") is None


# ---------------------------------------------------------------------------
# fetch() — candidate happy path + caching
# ---------------------------------------------------------------------------


def _candidate_responder(_url, _params):
    """Route candidate-fetch URLs to canned payloads."""
    if "candidate/H0NY03169/totals" in _url:
        return 200, json.dumps(_CANDIDATE_TOTALS_PAYLOAD)
    if "candidate/H0NY03169/" in _url:
        return 200, json.dumps(_CANDIDATE_HEADER_PAYLOAD)
    if "schedule_a/by_employer" in _url:
        return 200, json.dumps(_CANDIDATE_DONORS_PAYLOAD)
    if "schedule_b/by_purpose" in _url:
        return 200, json.dumps(_CANDIDATE_PURPOSES_PAYLOAD)
    return 404, ""


async def test_fetch_candidate_builds_markdown_and_caches(
    monkeypatch, cache_dir: Path
):
    captured = _patch_httpx(monkeypatch, responder=_candidate_responder)

    url = "https://www.fec.gov/data/candidate/H0NY03169/"
    source = await fec.fetch(url)

    assert source is not None
    assert source.source_kind == "fec"
    assert source.url == url
    assert source.title == "SANTOS, GEORGE"
    body = source.cleaned_text
    assert body.startswith("# SANTOS, GEORGE")
    # Header metadata
    assert "REPUBLICAN PARTY" in body
    assert "NY" in body
    assert "House" in body
    # Cycle totals roll-up — uses latest cycle from election_years (2024)
    assert "Cycle totals (2024)" in body
    assert "$1,234,567" in body  # receipts
    assert "$1,100,000" in body  # disbursements
    assert "$134,567" in body  # cash on hand
    # Top donors
    assert "Top donors" in body
    assert "ACME CORP" in body
    assert "$25,000" in body
    # Top expenditures
    assert "Top expenditures" in body
    assert "Media" in body
    assert "$600,000" in body

    # Metadata structured roll-up
    md = source.metadata
    assert md["candidate_id"] == "H0NY03169"
    assert md["cycle_totals"]["cycle"] == 2024
    assert md["cycle_totals"]["receipts"] == 1234567.0
    assert len(md["top_donors"]) == 2
    assert md["top_donors"][0]["label"] == "ACME CORP"
    assert md["top_donors"][0]["total"] == 25000.0
    assert md["top_expenditures"][0]["label"] == "Media"

    # Cache files written
    cached = sorted(p.name for p in cache_dir.glob("*.json"))
    assert any(c.startswith("candidate-") for c in cached)
    assert any(c.startswith("candidate-totals-") for c in cached)
    assert any(c.startswith("candidate-donors-") for c in cached)
    assert any(c.startswith("candidate-purposes-") for c in cached)

    # Second call serves from cache — no additional HTTP hits.
    api_calls_before = len(captured["urls"])
    s2 = await fec.fetch(url)
    assert s2 is not None
    assert len(captured["urls"]) == api_calls_before


async def test_fetch_committee_happy_path(monkeypatch, cache_dir: Path):
    def _respond(_url, _params):
        if "committee/C00500587/totals" in _url:
            return 200, json.dumps(_COMMITTEE_TOTALS_PAYLOAD)
        if "committee/C00500587/" in _url:
            return 200, json.dumps(_COMMITTEE_HEADER_PAYLOAD)
        if "schedule_a/by_employer" in _url:
            return 200, json.dumps(_CANDIDATE_DONORS_PAYLOAD)
        if "schedule_b/by_purpose" in _url:
            return 200, json.dumps(_CANDIDATE_PURPOSES_PAYLOAD)
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    url = "https://www.fec.gov/data/committee/C00500587/"
    source = await fec.fetch(url)

    assert source is not None
    assert source.title == "MAKE AMERICA GREAT AGAIN PAC"
    body = source.cleaned_text
    assert "Cycle totals (2024)" in body
    assert "$9,999,999" in body
    md = source.metadata
    assert md["committee_id"] == "C00500587"
    assert md["committee_type_full"] == "Political Action Committee"
    assert md["cycle_totals"]["cycle"] == 2024


async def test_fetch_returns_none_on_404(monkeypatch, cache_dir: Path):
    def _respond(url, params):
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    url = "https://www.fec.gov/data/candidate/H0NY03169/"
    assert await fec.fetch(url) is None


async def test_fetch_returns_none_on_transport_error(monkeypatch, cache_dir: Path):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(fec.httpx, "AsyncClient", _client_factory)

    url = "https://www.fec.gov/data/candidate/H0NY03169/"
    assert await fec.fetch(url) is None


async def test_cache_ttl_expires_after_one_hour(monkeypatch, cache_dir: Path):
    """A cache file older than ``_CACHE_TTL`` (1h) is dropped + re-fetched."""
    captured = _patch_httpx(monkeypatch, responder=_candidate_responder)

    url = "https://www.fec.gov/data/candidate/H0NY03169/"
    source = await fec.fetch(url)
    assert source is not None
    calls_after_first = len(captured["urls"])
    assert calls_after_first > 0

    # Age every cache file to 2 hours old (TTL is 1h).
    two_hours_ago = time.time() - 7200
    for path in cache_dir.glob("*.json"):
        import os as _os

        _os.utime(path, (two_hours_ago, two_hours_ago))

    source2 = await fec.fetch(url)
    assert source2 is not None
    # A second round of HTTP calls should have fired (stale cache dropped).
    assert len(captured["urls"]) > calls_after_first


# ---------------------------------------------------------------------------
# Smoke registration
# ---------------------------------------------------------------------------


def test_smoke_registry_includes_fec():
    from research_agent.tools import TOOL_REGISTRY

    assert "fec" in TOOL_REGISTRY
