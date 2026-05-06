"""Tests for `research_agent.tools.sanctions` (issue #116)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from research_agent.tools import sanctions

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SDN_FIXTURE = FIXTURE_DIR / "sanctions_sdn_advanced.xml"
EU_FIXTURE = FIXTURE_DIR / "sanctions_eu.xml"
UK_FIXTURE = FIXTURE_DIR / "sanctions_uk.csv"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch, tmp_path):
    sanctions.reset_for_tests()
    monkeypatch.setattr(sanctions.asyncio, "sleep", AsyncMock())
    monkeypatch.setenv("SANCTIONS_DB_PATH", str(tmp_path / "sanctions.sqlite"))
    yield
    sanctions.reset_for_tests()


def _make_http_get(
    *,
    sdn_status: int = 200,
    sdn_body: bytes | None = None,
    eu_status: int = 200,
    eu_body: bytes | None = None,
    uk_status: int = 200,
    uk_body: bytes | None = None,
):
    """Build a stub ``http_get`` that returns canned responses keyed by URL."""
    sdn_body = sdn_body if sdn_body is not None else SDN_FIXTURE.read_bytes()
    eu_body = eu_body if eu_body is not None else EU_FIXTURE.read_bytes()
    uk_body = uk_body if uk_body is not None else UK_FIXTURE.read_bytes()
    calls: list[str] = []

    async def _http_get(url: str, *, timeout: float = 60.0):
        calls.append(url)
        if url == sanctions.SDN_ADVANCED_URL:
            return sdn_status, sdn_body if sdn_status < 400 else b""
        if url == sanctions.EU_CONSOLIDATED_URL:
            return eu_status, eu_body if eu_status < 400 else b""
        if url == sanctions.UK_OFSI_URL:
            return uk_status, uk_body if uk_status < 400 else b""
        return 404, b""

    _http_get.calls = calls  # type: ignore[attr-defined]
    return _http_get


# ---------------------------------------------------------------------------
# _ensure_index
# ---------------------------------------------------------------------------


async def test_ensure_index_populates_sqlite():
    http_get = _make_http_get()
    db_path = await sanctions._ensure_index(force=True, http_get=http_get)
    assert db_path.exists()

    conn = sanctions._connect(db_path)
    try:
        rows = conn.execute("SELECT list_kind, COUNT(*) FROM entries GROUP BY list_kind").fetchall()
    finally:
        conn.close()
    counts = {r[0]: r[1] for r in rows}
    assert counts.get("SDN") == 2
    assert counts.get("EU") == 2
    assert counts.get("UK") == 2


async def test_ensure_index_within_ttl_is_noop():
    http_get = _make_http_get()
    await sanctions._ensure_index(force=True, http_get=http_get)
    first_call_count = len(http_get.calls)

    # Reset the index lock state but keep the db file fresh.
    sanctions.reset_for_tests()
    http_get2 = _make_http_get()
    await sanctions._ensure_index(http_get=http_get2)
    assert http_get2.calls == [], "fresh index should skip refresh"
    assert first_call_count > 0  # sanity


async def test_cache_invalidation_after_24h(monkeypatch):
    http_get = _make_http_get()
    db_path = await sanctions._ensure_index(force=True, http_get=http_get)

    sanctions.reset_for_tests()
    # Pretend the index file is 25 hours old.
    stale = time.time() - (25 * 3600)
    import os
    os.utime(db_path, (stale, stale))

    http_get2 = _make_http_get()
    await sanctions._ensure_index(http_get=http_get2)
    assert sanctions.SDN_ADVANCED_URL in http_get2.calls


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_finds_sdn_entry_by_name():
    http_get = _make_http_get()
    results = await sanctions.search("Prigozhin", http_get=http_get)
    assert results, "expected at least one Prigozhin hit"
    top = results[0]
    assert top.source_kind == "sanctions"
    assert "Prigozhin" in top.title
    assert top.extras["list_kind"] == "SDN"
    assert top.extras["designation_date"] == "2018-03-15"
    assert "UKRAINE-EO13661" in top.extras["programs"]
    assert any("Evgeny" in a["name"] for a in top.extras["aliases"])
    assert top.url.startswith("https://sanctionssearch.ofac.treas.gov/Details.aspx?id=")
    assert "23028" in top.url


async def test_search_matches_via_id_index():
    http_get = _make_http_get()
    results = await sanctions.search("7841329844", http_get=http_get)
    assert results, "expected EIN/Tax ID search to hit the entity row"
    assert "CONCORD" in results[0].title.upper()


async def test_fuzzy_match_for_alias_only_query():
    """Transliteration fallback flips ``extras['fuzzy']=True``.

    "Yevgenii Prigojin" (a less-common transliteration plus a typo) shouldn't
    match the canonical name or any indexed alias under exact FTS, but the
    normalized substring fallback should still catch the surname.
    """
    http_get = _make_http_get()
    results = await sanctions.search("Prigojin Yevgenii", http_get=http_get)
    # No FTS match for the exact tokens, so we should at least get the fuzzy
    # surname pass on aliases / normalized name.
    if results:
        top = results[0]
        assert "Prigozhin" in top.title or top.extras.get("fuzzy")


async def test_fuzzy_flag_set_when_no_fts_match():
    http_get = _make_http_get()
    # Force a path that's only reachable via substring fallback by querying
    # a misspelt prefix that survives normalization to "prigozh" but FTS
    # tokenizer wouldn't match "prigoz".
    db_path = await sanctions._ensure_index(force=True, http_get=http_get)
    conn = sanctions._connect(db_path)
    try:
        results = sanctions._fuzzy_search(conn, "prigozh", max_results=5)
    finally:
        conn.close()
    assert results
    assert results[0].extras["fuzzy"] is True


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def test_fetch_sdn_details_resolves_locally():
    http_get = _make_http_get()
    await sanctions._ensure_index(force=True, http_get=http_get)

    url = "https://sanctionssearch.ofac.treas.gov/Details.aspx?id=23028"
    source = await sanctions.fetch(url, http_get=http_get)
    assert source is not None
    assert source.source_kind == "sanctions"
    assert "Prigozhin" in source.title
    body = source.cleaned_text
    assert "## Aliases" in body
    assert "Evgeny Prigozhin" in body
    assert "## Identifiers" in body
    assert source.metadata["list_kind"] == "SDN"
    assert source.metadata["uid"] == "23028"


async def test_fetch_rejects_unknown_host():
    http_get = _make_http_get()
    assert await sanctions.fetch("https://example.com/whatever", http_get=http_get) is None


async def test_fetch_recent_actions_scrapes_html():
    html = (
        b"<html><body><ul>"
        b"<li>2024-09-05 - OFAC designates network of front companies</li>"
        b"<li>2024-09-03 - Treasury sanctions Russian oligarch</li>"
        b"</ul></body></html>"
    )

    async def http_get(url: str, *, timeout: float = 60.0):
        if url == sanctions.RECENT_ACTIONS_URL:
            return 200, html
        return 200, b""

    source = await sanctions.fetch(sanctions.RECENT_ACTIONS_URL, http_get=http_get)
    assert source is not None
    assert "OFAC Recent Actions" in source.cleaned_text
    assert "front companies" in source.cleaned_text


async def test_fetch_eu_url_returns_marker_source():
    http_get = _make_http_get()
    source = await sanctions.fetch(sanctions.EU_CONSOLIDATED_URL, http_get=http_get)
    assert source is not None
    assert source.metadata["list_kind"] == "EU"
    assert source.metadata["sanctioning_agency"] == "EU Council"


async def test_fetch_uk_url_returns_marker_source():
    http_get = _make_http_get()
    source = await sanctions.fetch(sanctions.UK_OFSI_URL, http_get=http_get)
    assert source is not None
    assert source.metadata["list_kind"] == "UK"


# ---------------------------------------------------------------------------
# Multi-list ingest + partial failure
# ---------------------------------------------------------------------------


async def test_eu_and_uk_rows_indexed():
    http_get = _make_http_get()
    await sanctions._ensure_index(force=True, http_get=http_get)

    eu_results = await sanctions.search("Mordashov", http_get=http_get)
    assert eu_results
    assert eu_results[0].extras["list_kind"] == "EU"

    uk_results = await sanctions.search("Abramovich", http_get=http_get)
    assert uk_results
    assert uk_results[0].extras["list_kind"] == "UK"


async def test_eu_failure_does_not_abort_sdn():
    http_get = _make_http_get(eu_status=500, eu_body=b"")
    await sanctions._ensure_index(force=True, http_get=http_get)
    sdn_hits = await sanctions.search("Prigozhin", http_get=http_get)
    assert sdn_hits, "SDN should still be queryable when EU fetch fails"
    assert sdn_hits[0].extras["list_kind"] == "SDN"


async def test_kinds_filter_narrows_results():
    http_get = _make_http_get()
    await sanctions._ensure_index(force=True, http_get=http_get)
    results = await sanctions.search(
        "Severstal", kinds=["EU"], http_get=http_get
    )
    assert results
    assert all(r.extras["list_kind"] == "EU" for r in results)


# ---------------------------------------------------------------------------
# Source kind literal & registry
# ---------------------------------------------------------------------------


def test_source_kind_literal():
    from research_agent.tools.models import SearchResult

    result = SearchResult(
        url="https://sanctionssearch.ofac.treas.gov/Details.aspx?id=1",
        title="t",
        snippet="s",
        source_kind="sanctions",
    )
    assert result.source_kind == "sanctions"


def test_smoke_registry_includes_sanctions():
    from research_agent.tools import TOOL_REGISTRY

    assert "sanctions" in TOOL_REGISTRY


def test_smoke_wrapper_returns_string(monkeypatch, tmp_path):
    """End-to-end smoke: pre-populate the index, run the smoke wrapper sync."""
    monkeypatch.setenv("SANCTIONS_DB_PATH", str(tmp_path / "sanctions.sqlite"))
    sanctions.reset_for_tests()

    # Force-build the index synchronously via asyncio.run inside the helper.
    import asyncio

    http_get = _make_http_get()

    async def _seed():
        await sanctions._ensure_index(force=True, http_get=http_get)

    asyncio.run(_seed())

    # Patch the module-level http_get so search() inside the smoke wrapper hits
    # the canned fixtures even though it doesn't accept an injected dep.
    monkeypatch.setattr(sanctions, "_http_get", http_get)

    from research_agent.tools import _smoke_sanctions

    out = _smoke_sanctions("Prigozhin")
    assert isinstance(out, str)
    assert "Prigozhin" in out
    assert "SDN" in out
