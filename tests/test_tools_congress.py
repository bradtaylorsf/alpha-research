"""Tests for `research_agent.tools.congress` (issue #99)."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import congress

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("DATA_GOV_API_KEY", "test-key-1234567890abcdef")
    yield


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    congress.reset_for_tests()
    monkeypatch.setattr(congress.asyncio, "sleep", AsyncMock())
    yield
    congress.reset_for_tests()


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "congress-cache"
    monkeypatch.setattr(congress, "_CACHE_DIR", target)
    return target


# ---------------------------------------------------------------------------
# Test payloads
# ---------------------------------------------------------------------------


_BILL_SEARCH_PAYLOAD = {
    "bills": [
        {
            "congress": 117,
            "type": "HR",
            "number": "5376",
            "title": "Inflation Reduction Act of 2022",
            "originChamber": "House",
            "session": 2,
            "latestAction": {
                "actionDate": "2022-08-16",
                "text": "Became Public Law No: 117-169.",
            },
            "sponsors": [
                {"fullName": "Rep. Yarmuth, John A. [D-KY-3]", "bioguideId": "Y000062"}
            ],
            "updateDate": "2022-08-17",
            "url": "https://api.congress.gov/v3/bill/117/hr/5376?format=json",
        }
    ]
}


_MEMBER_SEARCH_PAYLOAD = {
    "members": [
        {
            "bioguideId": "S000033",
            "name": "Sanders, Bernard",
            "partyName": "Independent",
            "state": "Vermont",
            "district": None,
            "terms": {"item": [{"chamber": "Senate", "startYear": 2007}]},
            "leadership": [],
            "updateDate": "2024-01-01",
            "url": "https://api.congress.gov/v3/member/S000033",
        }
    ]
}


_COMMITTEE_SEARCH_PAYLOAD = {
    "committees": [
        {
            "name": "Committee on Finance",
            "chamber": "Senate",
            "systemCode": "ssfi00",
            "committeeTypeCode": "Standing",
            "chair": {"fullName": "Sen. Wyden, Ron"},
            "url": "https://api.congress.gov/v3/committee/senate/ssfi00",
        }
    ]
}


_HEARING_SEARCH_PAYLOAD = {
    "hearings": [
        {
            "congress": 118,
            "chamber": "House",
            "jacketNumber": 51234,
            "title": "Oversight of the Department of Justice",
            "date": "2024-03-12",
            "committees": [{"name": "Committee on the Judiciary"}],
            "url": "https://api.congress.gov/v3/hearing/118/house/51234",
        }
    ]
}


_RECORD_SEARCH_PAYLOAD = {
    "Results": {
        "Issues": [
            {
                "Volume": 170,
                "Issue": 45,
                "PublishDate": "2024-03-14",
                "Congress": 118,
                "Session": 2,
                "Sections": [{"Name": "Senate"}, {"Name": "House"}],
                "Url": "https://www.congress.gov/congressional-record/2024/03/14",
            }
        ]
    }
}


_BILL_HEADER_PAYLOAD = {
    "bill": {
        "congress": 117,
        "type": "HR",
        "number": "5376",
        "title": "Inflation Reduction Act of 2022",
        "originChamber": "House",
        "sponsors": [
            {"fullName": "Rep. Yarmuth, John A. [D-KY-3]", "bioguideId": "Y000062"}
        ],
        "summaries": [
            {"text": "<p>Provides funding for energy security and climate.</p>"}
        ],
        "latestAction": {
            "actionDate": "2022-08-16",
            "text": "Became Public Law No: 117-169.",
        },
    }
}


_BILL_TEXT_PAYLOAD = {
    "textVersions": [
        {
            "type": "Public Law",
            "date": "2022-08-16",
            "formats": [
                {
                    "type": "Formatted Text",
                    "url": "https://www.congress.gov/117/bills/hr5376/BILLS-117hr5376enr.htm",
                },
                {
                    "type": "PDF",
                    "url": "https://www.congress.gov/117/bills/hr5376/BILLS-117hr5376enr.pdf",
                },
            ],
        }
    ]
}


_BILL_ACTIONS_PAYLOAD = {
    "actions": [
        {
            "actionDate": "2022-08-16",
            "text": "Became Public Law No: 117-169.",
            "type": "BecameLaw",
        },
        {
            "actionDate": "2022-08-12",
            "text": "Signed by President.",
            "type": "President",
        },
    ]
}


_MEMBER_HEADER_PAYLOAD = {
    "member": {
        "bioguideId": "S000033",
        "directOrderName": "Bernard Sanders",
        "partyName": "Independent",
        "state": "Vermont",
        "district": None,
        "terms": {"item": [{"chamber": "Senate", "startYear": 2007}]},
        "committees": [{"name": "Committee on the Budget"}],
        "rollCallVotes": {
            "url": "https://www.senate.gov/legislative/LIS/roll_call_lists/votes_S000033.xml"
        },
    }
}


_MEMBER_SPONSORED_PAYLOAD = {
    "sponsoredLegislation": [
        {
            "congress": 118,
            "type": "S",
            "number": "1234",
            "title": "Medicare for All Act",
            "latestAction": {
                "actionDate": "2024-02-01",
                "text": "Read twice and referred to the Committee on Finance.",
            },
        }
    ]
}


_MEMBER_COSPONSORED_PAYLOAD = {
    "cosponsoredLegislation": [
        {
            "congress": 118,
            "type": "S",
            "number": "777",
            "title": "Climate Resilience Act",
            "latestAction": {
                "actionDate": "2024-03-01",
                "text": "Referred to committee.",
            },
        }
    ]
}


_HEARING_DETAIL_PAYLOAD = {
    "hearing": {
        "jacketNumber": 51234,
        "congress": 118,
        "chamber": "House",
        "title": "Oversight of the Department of Justice",
        "citation": "H.Hrg. 118-45",
        "dates": [{"date": "2024-03-12"}],
        "committees": [
            {"name": "Committee on the Judiciary", "systemCode": "hsju00"}
        ],
        "formats": [
            {
                "type": "Formatted Text",
                "url": "https://www.govinfo.gov/content/pkg/CHRG-118hhrg51234/html/CHRG-118hhrg51234.htm",
            },
            {
                "type": "PDF",
                "url": "https://www.govinfo.gov/content/pkg/CHRG-118hhrg51234/pdf/CHRG-118hhrg51234.pdf",
            },
        ],
    }
}


# ---------------------------------------------------------------------------
# httpx mock plumbing
# ---------------------------------------------------------------------------


def _patch_httpx(monkeypatch, *, responder):
    """Replace ``httpx.AsyncClient`` with a fake driven by ``responder(url, params)``."""
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

    monkeypatch.setattr(congress.httpx, "AsyncClient", _client_factory)
    return captured


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_bill_builds_correct_query(monkeypatch):
    payload = json.dumps(_BILL_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await congress.search("Inflation Reduction Act", kind="bill")

    assert len(results) == 1
    assert captured["urls"][0].endswith("/v3/bill")
    params = captured["params"][0]
    assert params["query"] == "Inflation Reduction Act"
    assert params["api_key"] == "test-key-1234567890abcdef"
    assert params["format"] == "json"
    assert params["limit"] == 20

    hit = results[0]
    assert hit.source_kind == "congress"
    assert hit.title == "Inflation Reduction Act of 2022"
    assert hit.url == "https://www.congress.gov/bill/117th-congress/house-bill/5376"
    assert hit.extras["congress"] == 117
    assert hit.extras["bill_type"] == "hr"
    assert hit.extras["bill_number"] == "5376"
    assert hit.extras["sponsor"].startswith("Rep. Yarmuth")
    assert "117th Congress" in hit.snippet
    assert "Sponsor" in hit.snippet
    assert hit.published_at is not None


async def test_search_limit_capped_at_250(monkeypatch):
    payload = json.dumps({"bills": []})

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)
    await congress.search("anything", kind="bill", max_results=999)

    assert captured["params"][0]["limit"] == 250


async def test_search_member_endpoint(monkeypatch):
    payload = json.dumps(_MEMBER_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)
    results = await congress.search("Bernie Sanders", kind="member", max_results=5)

    assert captured["urls"][0].endswith("/v3/member")
    assert captured["params"][0]["query"] == "Bernie Sanders"
    assert captured["params"][0]["limit"] == 5

    assert len(results) == 1
    hit = results[0]
    assert hit.source_kind == "congress"
    assert hit.title == "Sanders, Bernard"
    assert hit.url == "https://www.congress.gov/member/S000033"
    assert hit.extras["bioguide_id"] == "S000033"
    assert hit.extras["party"] == "Independent"
    assert hit.extras["state"] == "Vermont"


async def test_search_committee_endpoint(monkeypatch):
    payload = json.dumps(_COMMITTEE_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)
    results = await congress.search("Finance", kind="committee")

    assert captured["urls"][0].endswith("/v3/committee")
    assert len(results) == 1
    hit = results[0]
    assert hit.title == "Committee on Finance"
    assert hit.url == "https://www.congress.gov/committee/senate/ssfi00"
    assert hit.extras["chamber"] == "Senate"
    assert hit.extras["system_code"] == "ssfi00"
    assert "Wyden" in hit.extras["chair"]


async def test_search_hearing_endpoint(monkeypatch):
    payload = json.dumps(_HEARING_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)
    results = await congress.search("DOJ oversight", kind="hearing")

    assert captured["urls"][0].endswith("/v3/hearing")
    assert len(results) == 1
    hit = results[0]
    assert hit.title == "Oversight of the Department of Justice"
    assert hit.extras["jacket_number"] == 51234
    assert hit.extras["congress"] == 118
    assert hit.extras["chamber"] == "House"
    assert hit.extras["committee"] == "Committee on the Judiciary"


async def test_search_congressional_record_endpoint(monkeypatch):
    payload = json.dumps(_RECORD_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)
    results = await congress.search("anything", kind="congressional-record")

    assert captured["urls"][0].endswith("/v3/congressional-record")
    assert len(results) == 1
    hit = results[0]
    assert hit.source_kind == "congress"
    assert hit.extras["volume"] == 170
    assert hit.extras["issue"] == 45
    assert hit.extras["congress"] == 118
    assert "Senate" in hit.extras["sections"]
    assert hit.url.startswith("https://www.congress.gov/")


async def test_search_unknown_kind_returns_empty(monkeypatch):
    called = {"count": 0}

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        called["count"] += 1

        class _Client:
            async def get(self, *_a, **_k):
                raise AssertionError("should not be called")

        yield _Client()

    monkeypatch.setattr(congress.httpx, "AsyncClient", _client_factory)
    assert await congress.search("anything", kind="bogus") == []
    assert called["count"] == 0


async def test_search_returns_empty_on_500(monkeypatch):
    def _respond(url, params):
        return 500, ""

    _patch_httpx(monkeypatch, responder=_respond)
    assert await congress.search("anything") == []


async def test_search_returns_empty_on_transport_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(congress.httpx, "AsyncClient", _client_factory)
    assert await congress.search("anything") == []


async def test_search_returns_empty_on_non_json(monkeypatch):
    def _respond(url, params):
        return 200, "<html>not json</html>"

    _patch_httpx(monkeypatch, responder=_respond)
    assert await congress.search("anything") == []


async def test_demo_key_fallback_when_unset(monkeypatch):
    monkeypatch.delenv("DATA_GOV_API_KEY", raising=False)
    payload = json.dumps({"bills": []})

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)
    await congress.search("anything")

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

    monkeypatch.setattr(congress.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(congress.asyncio, "sleep", fake_sleep)

    payload = json.dumps({"bills": []})

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    await asyncio.gather(congress.search("a"), congress.search("b"))

    assert any(s > 0 for s in sleep_calls), (
        f"expected at least one >0 sleep through the rate gate; got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# fetch() — URL classifier
# ---------------------------------------------------------------------------


async def test_fetch_returns_none_for_unknown_host(cache_dir: Path):
    assert await congress.fetch("https://example.com/bill/117th-congress/house-bill/5376") is None


async def test_fetch_rejects_lookalike_host(cache_dir: Path):
    spoof = "https://www.congress.gov.attacker.example/bill/117th-congress/house-bill/5376"
    assert await congress.fetch(spoof) is None


async def test_fetch_returns_none_for_unrecognised_path(cache_dir: Path):
    assert await congress.fetch("https://www.congress.gov/about") is None


async def test_fetch_returns_none_for_empty_url(cache_dir: Path):
    assert await congress.fetch("") is None


# ---------------------------------------------------------------------------
# fetch() — bill happy path + caching
# ---------------------------------------------------------------------------


def _bill_responder(url, _params):
    if url.endswith("/bill/117/hr/5376/text"):
        return 200, json.dumps(_BILL_TEXT_PAYLOAD)
    if url.endswith("/bill/117/hr/5376/actions"):
        return 200, json.dumps(_BILL_ACTIONS_PAYLOAD)
    if url.endswith("/bill/117/hr/5376"):
        return 200, json.dumps(_BILL_HEADER_PAYLOAD)
    return 404, ""


async def test_fetch_bill_builds_markdown_and_caches(monkeypatch, cache_dir: Path):
    captured = _patch_httpx(monkeypatch, responder=_bill_responder)

    url = "https://www.congress.gov/bill/117th-congress/house-bill/5376"
    source = await congress.fetch(url)

    assert source is not None
    assert source.source_kind == "congress"
    assert source.url == url
    assert source.title == "Inflation Reduction Act of 2022"
    body = source.cleaned_text

    assert body.startswith("# Inflation Reduction Act of 2022")
    assert "HR 5376" in body
    assert "117th Congress" in body
    assert "Sponsor" in body and "Yarmuth" in body
    assert "## Summary" in body
    assert "## Actions" in body
    assert "Became Public Law No: 117-169." in body
    assert "## Text" in body
    assert "Formatted Text" in body
    # Bill text URL recorded but the body itself is not embedded
    assert "BILLS-117hr5376enr.htm" in body

    md = source.metadata
    assert md["congress"] == 117
    assert md["bill_type"] == "hr"
    assert md["bill_number"] == "5376"
    assert md["text_url"].endswith("BILLS-117hr5376enr.htm")
    assert md["text_format"] == "Formatted Text"
    # Issue #193: alias keys consumed by the loop's bill-text fan-out.
    assert md["bill_text_url"] == md["text_url"]
    assert md["bill_text_format"] == md["text_format"]
    assert isinstance(md["actions"], list)
    assert md["actions"][0]["text"].startswith("Became Public Law")

    cached = sorted(p.name for p in cache_dir.glob("*.json"))
    assert any(c.startswith("bill-") for c in cached)
    assert any(c.startswith("bill-text-") for c in cached)
    assert any(c.startswith("bill-actions-") for c in cached)

    # Second call serves from cache — no additional HTTP hits.
    api_calls_before = len(captured["urls"])
    s2 = await congress.fetch(url)
    assert s2 is not None
    assert len(captured["urls"]) == api_calls_before


async def test_fetch_bill_no_public_text_omits_bill_text_url(monkeypatch, cache_dir: Path):
    """Issue #193: when the bill has no published text yet (common for newly-
    introduced bills), the metadata's ``bill_text_url`` must be ``None`` so the
    loop's fan-out helper silently skips emitting a follow-up.
    """

    def _no_text_responder(url, _params):
        if url.endswith("/bill/117/hr/5376/text"):
            return 200, json.dumps({"textVersions": []})
        if url.endswith("/bill/117/hr/5376/actions"):
            return 200, json.dumps(_BILL_ACTIONS_PAYLOAD)
        if url.endswith("/bill/117/hr/5376"):
            return 200, json.dumps(_BILL_HEADER_PAYLOAD)
        return 404, ""

    _patch_httpx(monkeypatch, responder=_no_text_responder)

    url = "https://www.congress.gov/bill/117th-congress/house-bill/5376"
    source = await congress.fetch(url)

    assert source is not None
    md = source.metadata
    assert md["bill_text_url"] is None
    assert md["bill_text_format"] is None
    # And the cleaned-text body should reflect the absence rather than
    # showing a stale URL.
    assert "_No public text available yet._" in source.cleaned_text


# ---------------------------------------------------------------------------
# fetch() — member happy path
# ---------------------------------------------------------------------------


def _member_responder(url, _params):
    if url.endswith("/member/S000033/sponsored-legislation"):
        return 200, json.dumps(_MEMBER_SPONSORED_PAYLOAD)
    if url.endswith("/member/S000033/cosponsored-legislation"):
        return 200, json.dumps(_MEMBER_COSPONSORED_PAYLOAD)
    if url.endswith("/member/S000033"):
        return 200, json.dumps(_MEMBER_HEADER_PAYLOAD)
    return 404, ""


async def test_fetch_member_voting_record_url_in_metadata_not_inlined(
    monkeypatch, cache_dir: Path
):
    _patch_httpx(monkeypatch, responder=_member_responder)

    url = "https://www.congress.gov/member/S000033"
    source = await congress.fetch(url)

    assert source is not None
    assert source.title == "Bernard Sanders"
    body = source.cleaned_text

    assert "## Committees" in body and "Committee on the Budget" in body
    assert "## Recent sponsored bills" in body
    assert "Medicare for All Act" in body
    assert "## Recent cosponsored bills" in body
    assert "Climate Resilience Act" in body
    assert "## Voting record" in body
    assert "votes_S000033.xml" in body  # URL referenced
    # XML must NOT be inlined into cleaned_text — only the URL pointer.
    assert "<rollCallVotes" not in body
    assert "<?xml" not in body

    md = source.metadata
    assert md["bioguide_id"] == "S000033"
    assert md["voting_record_xml_url"].endswith("votes_S000033.xml")
    assert md["sponsored_count"] == 1
    assert md["cosponsored_count"] == 1
    assert "Committee on the Budget" in md["committees"]


# ---------------------------------------------------------------------------
# fetch() — hearing happy path
# ---------------------------------------------------------------------------


def _hearing_responder(url, _params):
    if "/hearing/118/house/51234" in url:
        return 200, json.dumps(_HEARING_DETAIL_PAYLOAD)
    return 404, ""


async def test_fetch_hearing_rolls_up_committees_and_transcript(
    monkeypatch, cache_dir: Path
):
    _patch_httpx(monkeypatch, responder=_hearing_responder)

    url = "https://www.congress.gov/congressional-hearings/118th-congress/house/51234"
    source = await congress.fetch(url)

    assert source is not None
    assert source.source_kind == "congress"
    assert source.url == url
    assert source.title == "Oversight of the Department of Justice"

    body = source.cleaned_text
    assert body.startswith("# Oversight of the Department of Justice")
    assert "118th Congress" in body
    assert "2024-03-12" in body
    assert "H.Hrg. 118-45" in body
    assert "## Committees" in body
    assert "Committee on the Judiciary" in body
    assert "## Transcript" in body
    assert "CHRG-118hhrg51234.htm" in body

    md = source.metadata
    assert md["congress"] == 118
    assert md["chamber"] == "house"
    assert md["jacket_number"] == "51234"
    assert md["citation"] == "H.Hrg. 118-45"
    assert md["dates"] == ["2024-03-12"]
    assert "Committee on the Judiciary" in md["committees"]
    assert md["transcript_url"].endswith("CHRG-118hhrg51234.htm")
    assert md["transcript_format"] == "Formatted Text"


async def test_fetch_returns_none_on_404(monkeypatch, cache_dir: Path):
    def _respond(url, params):
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    url = "https://www.congress.gov/bill/117th-congress/house-bill/5376"
    assert await congress.fetch(url) is None


async def test_fetch_returns_none_on_transport_error(monkeypatch, cache_dir: Path):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(congress.httpx, "AsyncClient", _client_factory)

    url = "https://www.congress.gov/bill/117th-congress/house-bill/5376"
    assert await congress.fetch(url) is None


async def test_cache_ttl_expires_after_one_hour(monkeypatch, cache_dir: Path):
    """A cache file older than ``_CACHE_TTL`` (1h) is dropped + re-fetched."""
    captured = _patch_httpx(monkeypatch, responder=_bill_responder)

    url = "https://www.congress.gov/bill/117th-congress/house-bill/5376"
    source = await congress.fetch(url)
    assert source is not None
    calls_after_first = len(captured["urls"])
    assert calls_after_first > 0

    # Age every cache file to 2 hours old (TTL is 1h).
    two_hours_ago = time.time() - 7200
    for path in cache_dir.glob("*.json"):
        import os as _os

        _os.utime(path, (two_hours_ago, two_hours_ago))

    source2 = await congress.fetch(url)
    assert source2 is not None
    assert len(captured["urls"]) > calls_after_first


# ---------------------------------------------------------------------------
# Smoke registration
# ---------------------------------------------------------------------------


def test_smoke_registry_includes_congress():
    from research_agent.tools import TOOL_REGISTRY

    assert "congress" in TOOL_REGISTRY
