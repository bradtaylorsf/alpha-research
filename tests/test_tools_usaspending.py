"""Tests for `research_agent.tools.usaspending` (issue #104)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import usaspending

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    usaspending.reset_for_tests()
    monkeypatch.setattr(usaspending.asyncio, "sleep", AsyncMock())
    yield
    usaspending.reset_for_tests()


# ---------------------------------------------------------------------------
# Test payloads
# ---------------------------------------------------------------------------


_SEARCH_PAYLOAD = {
    "results": [
        {
            "Award ID": "FA8650-20-D-1234",
            "Recipient Name": "BOOZ ALLEN HAMILTON INC.",
            "Recipient UEI": "ABC123XYZ456",
            "Award Amount": 5000000.0,
            "Description": "ENGINEERING SERVICES FOR DEFENSE INNOVATION",
            "Contract Award Type": "DEFINITIVE CONTRACT",
            "Award Type": "",
            "Awarding Agency": "Department of Defense",
            "Awarding Sub Agency": "Air Force",
            "Action Date": "2024-03-15",
            "Period of Performance Start Date": "2024-03-15",
            "Period of Performance Current End Date": "2026-03-14",
            "NAICS": "541512",
            "psc": "R425",
            "extent_competed": "FULL AND OPEN COMPETITION",
            "generated_internal_id": "CONT_AWD_FA865020D1234_9700_-NONE-_-NONE-",
        },
        {
            "Award ID": "GS-35F-9999",
            "Recipient Name": "ACME CONSULTING LLC",
            "Recipient UEI": "ZZZ987",
            "Award Amount": 250000.0,
            "Description": "IT SUPPORT",
            "Contract Award Type": "PURCHASE ORDER",
            "Awarding Agency": "General Services Administration",
            "Action Date": "2024-02-01",
            "extent_competed": None,
            "generated_internal_id": "CONT_AWD_GS35F9999",
        },
    ]
}


_IDV_A_NO_BID_PAYLOAD = {
    "results": [
        {
            "Award ID": "IDV-001",
            "Recipient Name": "NO-BID VENDOR INC.",
            "Recipient UEI": "NB1",
            "Award Amount": 9999999.0,
            "Description": "Sole-source GWAC",
            "Contract Award Type": "IDV_A",
            "Awarding Agency": "Department of Defense",
            "Action Date": "2024-01-01",
            "extent_competed": None,
            "generated_internal_id": "IDV_AWD_NOBID_001",
        }
    ]
}


_AWARD_DETAIL_PAYLOAD = {
    "id": 12345,
    "type": "D",
    "type_description": "DEFINITIVE CONTRACT",
    "category": "contract",
    "piid": "FA8650-20-D-1234",
    "description": "ENGINEERING SERVICES FOR DEFENSE INNOVATION",
    "generated_unique_award_id": "CONT_AWD_FA865020D1234_9700_-NONE-_-NONE-",
    "total_obligation": 7500000.0,
    "base_and_all_options_value": 5000000.0,
    "base_exercised_options_val": 2500000.0,
    "period_of_performance": {
        "start_date": "2024-03-15",
        "end_date": "2026-03-14",
    },
    "recipient": {
        "recipient_name": "BOOZ ALLEN HAMILTON INC.",
        "recipient_uei": "ABC123XYZ456",
        "parent_recipient_name": "BOOZ ALLEN HAMILTON HOLDING CORPORATION",
        "parent_recipient_uei": "PARENT123",
        "location": {
            "address_line1": "8283 Greensboro Drive",
            "city_name": "McLean",
            "state_code": "VA",
            "zip5": "22102",
        },
    },
    "awarding_agency": {
        "id": 1,
        "toptier_agency": {"name": "Department of Defense", "code": "097"},
        "subtier_agency": {"name": "Air Force"},
    },
    "naics_hierarchy": {
        "toptier_code": "54",
        "toptier_description": "Professional Services",
        "midtier_code": "5415",
        "subtier_code": "541512",
    },
    "psc_hierarchy": {
        "toptier_code": "R",
        "toptier_description": "Support Services",
        "midtier_code": "R4",
        "subtier_code": "R425",
    },
    "parent_award": {"modification_count": 3},
}


# ---------------------------------------------------------------------------
# httpx mock plumbing
# ---------------------------------------------------------------------------


def _patch_httpx(monkeypatch, *, post_responder=None, get_responder=None):
    """Replace ``httpx.AsyncClient`` with a fake routing post/get to responders.

    Returns a dict capturing urls, headers, json bodies, params per call.
    """
    captured: dict[str, list] = {
        "post_urls": [],
        "post_bodies": [],
        "get_urls": [],
        "headers": [],
    }

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
            async def post(self, url, *, json=None, **_kwargs):
                captured["post_urls"].append(url)
                captured["post_bodies"].append(json)
                if post_responder is None:
                    raise AssertionError(f"unexpected POST {url}")
                status, text = post_responder(url, json)
                return _FakeResp(status, text)

            async def get(self, url, **_kwargs):
                captured["get_urls"].append(url)
                if get_responder is None:
                    raise AssertionError(f"unexpected GET {url}")
                status, text = get_responder(url)
                return _FakeResp(status, text)

        yield _Client()

    monkeypatch.setattr(usaspending.httpx, "AsyncClient", _client_factory)
    return captured


# ---------------------------------------------------------------------------
# search() — POST body construction
# ---------------------------------------------------------------------------


async def test_search_default_builds_post_body(monkeypatch):
    payload = json.dumps(_SEARCH_PAYLOAD)

    def _post(url, body):
        return 200, payload

    captured = _patch_httpx(monkeypatch, post_responder=_post)

    results = await usaspending.search("Booz Allen Hamilton", max_results=10)

    assert len(captured["post_urls"]) == 1
    assert captured["post_urls"][0].endswith("/api/v2/search/spending_by_award/")
    body = captured["post_bodies"][0]
    assert body["filters"]["keywords"] == ["Booz Allen Hamilton"]
    # No award_type filter on the default call.
    assert "award_type_codes" not in body["filters"]
    assert body["limit"] == 10
    assert body["page"] == 1
    assert body["sort"] == "Action Date"
    assert body["order"] == "desc"
    assert isinstance(body["fields"], list) and "Award Amount" in body["fields"]

    assert len(results) == 2
    hit = results[0]
    assert hit.source_kind == "usaspending"
    assert hit.extras["recipient_name"] == "BOOZ ALLEN HAMILTON INC."
    assert hit.extras["recipient_uei"] == "ABC123XYZ456"
    assert hit.extras["award_amount"] == 5000000.0
    assert hit.extras["awarding_agency"] == "Department of Defense"
    assert hit.extras["action_date"] == "2024-03-15"
    assert hit.extras["naics_code"] == "541512"
    assert hit.extras["psc_code"] == "R425"
    assert hit.extras["generated_internal_id"].startswith("CONT_AWD_")
    assert hit.extras["no_bid_flag"] is False
    # Permalink built from generated_internal_id.
    assert hit.url.startswith("https://www.usaspending.gov/award/")
    assert hit.url.endswith("/")
    assert hit.published_at is not None


async def test_search_award_type_contracts_sets_codes(monkeypatch):
    payload = json.dumps({"results": []})

    def _post(url, body):
        return 200, payload

    captured = _patch_httpx(monkeypatch, post_responder=_post)

    await usaspending.search("anything", award_type="contracts")

    body = captured["post_bodies"][0]
    assert body["filters"]["award_type_codes"] == ["A", "B", "C", "D"]


async def test_search_award_type_grants_sets_codes(monkeypatch):
    payload = json.dumps({"results": []})

    def _post(url, body):
        return 200, payload

    captured = _patch_httpx(monkeypatch, post_responder=_post)

    await usaspending.search("anything", award_type="grants")

    body = captured["post_bodies"][0]
    assert body["filters"]["award_type_codes"] == ["02", "03", "04", "05"]


async def test_search_award_type_loans_sets_codes(monkeypatch):
    payload = json.dumps({"results": []})

    def _post(url, body):
        return 200, payload

    captured = _patch_httpx(monkeypatch, post_responder=_post)

    await usaspending.search("anything", award_type="loans")

    body = captured["post_bodies"][0]
    assert body["filters"]["award_type_codes"] == ["07", "08"]


async def test_search_award_type_idv_a_sets_codes_and_flags_no_bid(monkeypatch):
    payload = json.dumps(_IDV_A_NO_BID_PAYLOAD)

    def _post(url, body):
        return 200, payload

    captured = _patch_httpx(monkeypatch, post_responder=_post)

    results = await usaspending.search("anything", award_type="IDV_A")

    body = captured["post_bodies"][0]
    assert body["filters"]["award_type_codes"] == ["IDV_A"]
    assert len(results) == 1
    # AC: IDV_A with no competitive flag => no_bid_flag True for synthesis.
    assert results[0].extras["no_bid_flag"] is True


async def test_search_unknown_award_type_returns_empty(monkeypatch, caplog):
    """Unknown award_type short-circuits to ``[]`` without an HTTP call."""

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def post(self, *_a, **_k):
                raise AssertionError("should not be called")

            async def get(self, *_a, **_k):
                raise AssertionError("should not be called")

        yield _Client()

    monkeypatch.setattr(usaspending.httpx, "AsyncClient", _client_factory)

    with caplog.at_level("WARNING"):
        result = await usaspending.search("anything", award_type="bogus")

    assert result == []
    assert any("unknown award_type" in r.message for r in caplog.records)


async def test_search_http_error_returns_empty(monkeypatch):
    def _post(url, body):
        return 500, ""

    _patch_httpx(monkeypatch, post_responder=_post)

    assert await usaspending.search("anything") == []


async def test_search_transport_error_returns_empty(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def post(self, *_a, **_k):
                raise httpx.ConnectError("nope")

            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(usaspending.httpx, "AsyncClient", _client_factory)

    assert await usaspending.search("anything") == []


async def test_search_non_json_returns_empty(monkeypatch):
    def _post(url, body):
        return 200, "<html>not json</html>"

    _patch_httpx(monkeypatch, post_responder=_post)

    assert await usaspending.search("anything") == []


async def test_rate_limit_gate_enforces_two_rps(monkeypatch):
    """Two concurrent search calls must space out by at least ~0.5s."""
    clock = [100.0]

    def fake_monotonic() -> float:
        return clock[0]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(usaspending.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(usaspending.asyncio, "sleep", fake_sleep)

    payload = json.dumps({"results": []})

    def _post(url, body):
        return 200, payload

    _patch_httpx(monkeypatch, post_responder=_post)

    await asyncio.gather(
        usaspending.search("a"),
        usaspending.search("b"),
    )

    # At least one sleep should have been ~0.5s (2 RPS gate).
    assert any(abs(s - 0.5) < 1e-6 for s in sleep_calls), (
        f"expected a ~0.5s sleep through the rate gate; got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def test_fetch_award_renders_base_award_and_modifications(monkeypatch):
    """Happy path: detail JSON renders to markdown with all required sections."""
    detail = json.dumps(_AWARD_DETAIL_PAYLOAD)
    award_id = "CONT_AWD_FA865020D1234_9700_-NONE-_-NONE-"

    def _get(url):
        if f"/awards/{award_id}/" in url:
            return 200, detail
        return 404, ""

    _patch_httpx(monkeypatch, get_responder=_get)

    url = f"https://www.usaspending.gov/award/{award_id}/"
    source = await usaspending.fetch(url)

    assert source is not None
    assert source.source_kind == "usaspending"
    assert source.url == url
    body = source.cleaned_text
    # Base award amount is the value file analysis runs on per AC.
    assert "Base award" in body
    assert "$5,000,000" in body
    assert "Modifications" in body
    assert "BOOZ ALLEN HAMILTON" in body
    assert "## Recipient" in body
    assert "## Classification" in body
    assert "Parent NAICS" in body
    assert "Parent PSC" in body
    assert "## Period of performance" in body
    assert "2024-03-15" in body
    assert "2026-03-14" in body

    md = source.metadata
    assert md["base_award_amount"] == 5000000.0
    assert md["total_obligation"] == 7500000.0
    assert md["modifications_count"] == 3
    # Modifications total = total_obligation - base_award.
    assert md["modifications_total"] == pytest.approx(2500000.0)
    assert md["recipient"]["name"] == "BOOZ ALLEN HAMILTON INC."
    assert md["parent_naics"]["code"] == "54"
    assert md["parent_psc"]["code"] == "R"
    assert md["period_of_performance_start"] == "2024-03-15"
    assert md["period_of_performance_end"] == "2026-03-14"


async def test_fetch_rejects_non_accepted_host(monkeypatch):
    """Look-alike hosts must not pass."""

    def _get(url):
        raise AssertionError("should not be called")

    _patch_httpx(monkeypatch, get_responder=_get)

    spoof = "https://www.usaspending.gov.attacker.example/award/CONT_AWD_X/"
    assert await usaspending.fetch(spoof) is None


async def test_fetch_unknown_path_returns_none(monkeypatch):
    """Paths outside ``/award/<id>/`` resolve to ``None``."""

    def _get(url):
        raise AssertionError("should not be called")

    _patch_httpx(monkeypatch, get_responder=_get)

    assert (
        await usaspending.fetch("https://www.usaspending.gov/agency/dod/")
        is None
    )


async def test_fetch_returns_none_for_empty_url():
    assert await usaspending.fetch("") is None


async def test_fetch_returns_none_on_404(monkeypatch):
    def _get(url):
        return 404, ""

    _patch_httpx(monkeypatch, get_responder=_get)

    url = "https://www.usaspending.gov/award/CONT_AWD_DOES_NOT_EXIST/"
    assert await usaspending.fetch(url) is None


async def test_fetch_returns_none_on_transport_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def post(self, *_a, **_k):
                raise httpx.ConnectError("nope")

            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(usaspending.httpx, "AsyncClient", _client_factory)

    url = "https://www.usaspending.gov/award/CONT_AWD_X/"
    assert await usaspending.fetch(url) is None


async def test_fetch_accepts_root_usaspending_host(monkeypatch):
    """The bare ``usaspending.gov`` host is also accepted."""
    detail = json.dumps(_AWARD_DETAIL_PAYLOAD)

    def _get(url):
        return 200, detail

    _patch_httpx(monkeypatch, get_responder=_get)

    url = "https://usaspending.gov/award/CONT_AWD_FA865020D1234_9700_-NONE-_-NONE-/"
    source = await usaspending.fetch(url)
    assert source is not None
    assert source.url == url


# ---------------------------------------------------------------------------
# Smoke registration
# ---------------------------------------------------------------------------


def test_smoke_registry_includes_usaspending():
    from research_agent.tools import TOOL_REGISTRY

    assert "usaspending" in TOOL_REGISTRY
