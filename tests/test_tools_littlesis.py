"""Tests for `research_agent.tools.littlesis` (issue #97)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import littlesis

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    littlesis.reset_for_tests()
    monkeypatch.setattr(littlesis.asyncio, "sleep", AsyncMock())
    yield
    littlesis.reset_for_tests()


# ---------------------------------------------------------------------------
# Test payloads (modelled on LittleSis JSON:API envelopes)
# ---------------------------------------------------------------------------


_ENTITY_SEARCH_PAYLOAD = {
    "data": [
        {
            "type": "entities",
            "id": "12345",
            "attributes": {
                "id": 12345,
                "name": "Peter Thiel",
                "blurb": "American entrepreneur and venture capitalist",
                "summary": "Co-founder of PayPal and Palantir; investor in Facebook.",
                "primary_ext": "Person",
                "types": ["Person", "Business Person"],
            },
            "links": {"self": "https://littlesis.org/entity/12345-Peter_Thiel"},
        },
        {
            "type": "entities",
            "id": "67890",
            "attributes": {
                "id": 67890,
                "name": "Peter Thiel Foundation",
                "blurb": "Philanthropic foundation",
                "summary": "",
                "primary_ext": "Org",
                "types": ["Organization", "Foundation"],
            },
            "links": {"self": "https://littlesis.org/entity/67890-Peter_Thiel_Foundation"},
        },
    ]
}


_RELATIONSHIPS_PAYLOAD = {
    "data": [
        {
            "type": "relationships",
            "id": "1001",
            "attributes": {
                "id": 1001,
                "entity1_id": 12345,
                "entity2_id": 100,
                "entity1_name": "Peter Thiel",
                "entity2_name": "PayPal",
                "category_id": 1,  # Position
                "description1": "Co-founder",
                "description2": "Co-founded by",
                "start_date": "1998",
                "end_date": "2002",
                "amount": None,
            },
        },
        {
            "type": "relationships",
            "id": "1002",
            "attributes": {
                "id": 1002,
                "entity1_id": 12345,
                "entity2_id": 200,
                "entity1_name": "Peter Thiel",
                "entity2_name": "Palantir Technologies",
                "category_id": 1,  # Position
                "description1": "Co-founder, Chairman",
                "description2": None,
                "start_date": "2003",
                "end_date": None,
                "amount": None,
            },
        },
        {
            "type": "relationships",
            "id": "1003",
            "attributes": {
                "id": 1003,
                "entity1_id": 12345,
                "entity2_id": 300,
                "entity1_name": "Peter Thiel",
                "entity2_name": "Donald J. Trump for President",
                "category_id": 5,  # Donation
                "description": "Campaign donation",
                "start_date": "2016-10-15",
                "end_date": None,
                "amount": 1250000,
            },
        },
        {
            "type": "relationships",
            "id": "1004",
            "attributes": {
                "id": 1004,
                "entity1_id": 12345,
                "entity2_id": 400,
                "entity1_name": "Peter Thiel",
                "entity2_name": "Facebook",
                "category_id": 10,  # Ownership
                "description": "Early investor",
                "start_date": "2004",
                "end_date": None,
                "amount": 500000,
            },
        },
        # Second Facebook edge — board membership — exercises the dedup path
        # for ``Connected organizations``.
        {
            "type": "relationships",
            "id": "1005",
            "attributes": {
                "id": 1005,
                "entity1_id": 12345,
                "entity2_id": 400,
                "entity1_name": "Peter Thiel",
                "entity2_name": "Facebook",
                "category_id": 1,  # Position
                "description1": "Board member",
                "description2": None,
                "start_date": "2005",
                "end_date": "2022",
                "amount": None,
            },
        },
    ]
}


_ENTITY_DETAIL_PAYLOAD = {
    "data": {
        "type": "entities",
        "id": "12345",
        "attributes": {
            "id": 12345,
            "name": "Peter Thiel",
            "blurb": "American entrepreneur and venture capitalist",
            "summary": "Co-founder of PayPal and Palantir; investor in Facebook.",
            "primary_ext": "Person",
            "types": ["Person", "Business Person"],
        },
        "links": {"self": "https://littlesis.org/entity/12345-Peter_Thiel"},
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

    monkeypatch.setattr(littlesis.httpx, "AsyncClient", _client_factory)
    return captured


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_entities_happy_path(monkeypatch):
    """Entities search returns name, primary_ext, types, summary, permalink."""
    payload = json.dumps(_ENTITY_SEARCH_PAYLOAD)

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await littlesis.search("Peter Thiel", kind="entities", max_results=5)

    assert captured["urls"][0].endswith("/api/entities/search")
    params = captured["params"][0]
    assert params["q"] == "Peter Thiel"
    assert params["num"] == 5

    assert len(results) == 2
    hit = results[0]
    assert hit.source_kind == "littlesis"
    assert hit.title == "Peter Thiel"
    assert hit.extras["entity_id"] == 12345
    assert hit.extras["primary_ext"] == "Person"
    assert "Person" in hit.extras["types"]
    assert "PayPal" in hit.extras["summary"]
    assert hit.url == "https://littlesis.org/entity/12345-Peter_Thiel"
    assert "American entrepreneur" in hit.snippet


async def test_search_entities_no_auth_header(monkeypatch):
    """No API key needed — requests must not carry an Authorization header."""
    payload = json.dumps({"data": []})

    def _respond(url, params):
        return 200, payload

    captured = _patch_httpx(monkeypatch, responder=_respond)
    await littlesis.search("anything")

    headers = captured["headers"][0] or {}
    assert "Authorization" not in headers


async def test_search_relationships_happy_path(monkeypatch):
    """Relationships search: entity lookup → top-hit relationships envelope."""

    def _respond(url, params):
        if url.endswith("/api/entities/search"):
            return 200, json.dumps(_ENTITY_SEARCH_PAYLOAD)
        if url.endswith("/api/entities/12345/relationships"):
            return 200, json.dumps(_RELATIONSHIPS_PAYLOAD)
        return 404, ""

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await littlesis.search(
        "Peter Thiel", kind="relationships", max_results=10
    )

    # Two GETs: search, then relationships for the top hit.
    assert len(captured["urls"]) == 2
    assert captured["urls"][1].endswith("/api/entities/12345/relationships")

    assert len(results) == 5
    first = results[0]
    assert first.source_kind == "littlesis"
    assert first.title == "Peter Thiel → PayPal"
    assert first.extras["category_id"] == 1
    assert first.extras["category_label"] == "Position"
    assert first.extras["entity1_id"] == 12345
    assert first.extras["entity2_id"] == 100
    assert first.extras["related_id"] == 100
    assert first.extras["related_name"] == "PayPal"
    assert first.extras["start_date"] == "1998"
    assert first.extras["end_date"] == "2002"
    assert "Position" in first.snippet
    assert "Co-founder" in first.snippet

    donation = results[2]
    assert donation.extras["category_label"] == "Donation"
    assert donation.extras["amount"] == 1250000


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

    monkeypatch.setattr(littlesis.httpx, "AsyncClient", _client_factory)

    assert await littlesis.search("anything", kind="bogus") == []
    assert called["count"] == 0


async def test_search_http_error_returns_empty(monkeypatch):
    """A non-200 response returns ``[]``."""

    def _respond(url, params):
        return 500, ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await littlesis.search("anything", kind="entities") == []


async def test_search_transport_error_returns_empty(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(littlesis.httpx, "AsyncClient", _client_factory)

    assert await littlesis.search("anything", kind="entities") == []


async def test_search_non_json_returns_empty(monkeypatch):
    def _respond(url, params):
        return 200, "<html>not json</html>"

    _patch_httpx(monkeypatch, responder=_respond)

    assert await littlesis.search("anything", kind="entities") == []


async def test_rate_limit_gate_enforces_one_rps(monkeypatch):
    """Two concurrent search calls must space out by at least ~1s."""
    clock = [100.0]

    def fake_monotonic() -> float:
        return clock[0]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(littlesis.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(littlesis.asyncio, "sleep", fake_sleep)

    payload = json.dumps({"data": []})

    def _respond(url, params):
        return 200, payload

    _patch_httpx(monkeypatch, responder=_respond)

    await asyncio.gather(
        littlesis.search("a", kind="entities"),
        littlesis.search("b", kind="entities"),
    )

    assert any(abs(s - 1.0) < 1e-6 for s in sleep_calls), (
        f"expected a ~1s sleep through the rate gate; got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def test_fetch_entity_returns_markdown_sections(monkeypatch):
    """Happy path: entity + relationships JSON renders to markdown."""

    def _respond(url, params):
        if url.endswith("/api/entities/12345"):
            return 200, json.dumps(_ENTITY_DETAIL_PAYLOAD)
        if url.endswith("/api/entities/12345/relationships"):
            return 200, json.dumps(_RELATIONSHIPS_PAYLOAD)
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    url = "https://littlesis.org/entity/12345-Peter_Thiel"
    source = await littlesis.fetch(url)

    assert source is not None
    assert source.source_kind == "littlesis"
    assert source.url == url
    assert source.title == "Peter Thiel"

    body = source.cleaned_text
    assert "# Peter Thiel" in body
    assert "## Summary" in body
    assert "PayPal and Palantir" in body
    assert "## Roles / Positions" in body
    # Both Position-category relationships should appear under roles.
    assert "PayPal" in body
    assert "Palantir Technologies" in body
    assert "## Relationships" in body
    # Categories are surfaced as subheadings.
    assert "### Position" in body
    assert "### Donation" in body
    assert "### Ownership" in body
    assert "## Connected organizations" in body
    # Two distinct relationships point at "Facebook" but it should appear
    # exactly once in the Connected organizations roll-up.
    orgs_section = body.split("## Connected organizations", 1)[1]
    assert orgs_section.count("- Facebook") == 1

    md = source.metadata
    assert md["entity_id"] == "12345"
    assert md["primary_ext"] == "Person"
    assert "Person" in md["types"]
    assert len(md["relationships"]) == 5
    assert "PayPal" in md["connected_orgs"]
    assert "Facebook" in md["connected_orgs"]
    # Connected orgs metadata must also be de-duplicated.
    assert md["connected_orgs"].count("Facebook") == 1
    # Donation relationship preserves the dollar amount.
    donation = next(
        r for r in md["relationships"] if r["category_label"] == "Donation"
    )
    assert donation["amount"] == 1250000


async def test_fetch_accepts_api_path(monkeypatch):
    """``/api/entities/<id>`` paths route through fetch the same way."""

    def _respond(url, params):
        if url.endswith("/api/entities/12345"):
            return 200, json.dumps(_ENTITY_DETAIL_PAYLOAD)
        if url.endswith("/api/entities/12345/relationships"):
            return 200, json.dumps({"data": []})
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    url = "https://littlesis.org/api/entities/12345"
    source = await littlesis.fetch(url)
    assert source is not None
    assert source.title == "Peter Thiel"


async def test_fetch_rejects_disallowed_host(monkeypatch):
    """A look-alike host must not pass."""

    def _respond(url, params):
        raise AssertionError("should not be called")

    _patch_httpx(monkeypatch, responder=_respond)

    spoof = "https://littlesis.org.attacker.example/entity/12345-Peter_Thiel"
    assert await littlesis.fetch(spoof) is None


async def test_fetch_rejects_unrelated_path(monkeypatch):
    """Paths that aren't entity URLs resolve to ``None``."""

    def _respond(url, params):
        raise AssertionError("should not be called")

    _patch_httpx(monkeypatch, responder=_respond)

    assert await littlesis.fetch("https://littlesis.org/about") is None
    assert await littlesis.fetch("https://littlesis.org/relationship/1001") is None


async def test_fetch_returns_none_for_empty_url():
    assert await littlesis.fetch("") is None


async def test_fetch_returns_none_on_404(monkeypatch):
    def _respond(url, params):
        return 404, ""

    _patch_httpx(monkeypatch, responder=_respond)

    url = "https://littlesis.org/entity/99999-Unknown"
    assert await littlesis.fetch(url) is None


async def test_fetch_returns_none_on_transport_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(littlesis.httpx, "AsyncClient", _client_factory)

    url = "https://littlesis.org/entity/12345-Peter_Thiel"
    assert await littlesis.fetch(url) is None


# ---------------------------------------------------------------------------
# Smoke registration
# ---------------------------------------------------------------------------


def test_smoke_registry_includes_littlesis():
    from research_agent.tools import TOOL_REGISTRY

    assert "littlesis" in TOOL_REGISTRY
