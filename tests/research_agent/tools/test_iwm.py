"""Tests for ``research_agent.tools.iwm`` (issue #237)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from research_agent.tools import browser, iwm
from research_agent.tools.models import SearchResult, Source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "iwm"


@pytest.fixture(autouse=True)
def _reset_state():
    browser.reset_for_tests()
    iwm.reset_for_tests()
    yield
    browser.reset_for_tests()
    iwm.reset_for_tests()


class _FakePage:
    def __init__(self, html: str) -> None:
        self._html = html
        self.closed = False

    async def content(self) -> str:
        return self._html

    async def close(self) -> None:
        self.closed = True

    async def screenshot(self, *, path: str) -> None:
        Path(path).write_text("fake screenshot", encoding="utf-8")


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.page = page

    async def new_page(self) -> _FakePage:
        return self.page


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _patch_browser(monkeypatch: pytest.MonkeyPatch, html: str) -> dict[str, object]:
    page = _FakePage(html)
    captured: dict[str, object] = {"urls": [], "page": page}

    @asynccontextmanager
    async def _browser_session(*_args, **_kwargs):
        yield _FakeContext(page)

    async def _navigate(_page: _FakePage, url: str, **_kwargs) -> None:
        assert _page is page
        captured["urls"].append(url)  # type: ignore[union-attr]

    monkeypatch.setattr(iwm.browser, "browser_session", _browser_session)
    monkeypatch.setattr(iwm.browser, "navigate", _navigate)
    return captured


async def test_search_happy_path_parses_recorded_fixture_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_browser(monkeypatch, _fixture("results-battle-of-britain.html"))

    results = await iwm.search("Battle of Britain", max_results=20)

    assert captured["urls"] == [
        "https://www.iwm.org.uk/collections/search?query=Battle+of+Britain"
    ]
    assert len(results) == 2

    first = results[0]
    assert first.source_kind == "iwm_search"
    assert first.title == "BATTLE OF BRITAIN"
    assert first.url == "https://www.iwm.org.uk/collections/item/object/205226579"
    assert "HU 810" in first.snippet
    assert first.extras["object_type"] == "Photographs"
    assert first.extras["catalogue_id"] == "HU 810"
    assert first.extras["production_date"] == "Unknown"
    assert first.extras["creator"] == "Unknown"

    second = results[1]
    assert second.published_at == datetime(1940, 1, 1, tzinfo=UTC)
    assert second.extras["period"] == (
        "Second World War (production), Second World War (content)"
    )


async def test_search_url_supports_public_filter_knobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_browser(monkeypatch, _fixture("results-empty.html"))

    await iwm.search(
        "2nd Highland Light Infantry",
        object_category="oral histories",
        related_period="WW1",
        records_with_media=True,
        style="list",
        page_size=30,
    )

    assert captured["urls"] == [
        "https://www.iwm.org.uk/collections/search?"
        "query=2nd+Highland+Light+Infantry&pageSize=30&style=list&"
        "media-records=records-with-media&"
        "filters%5BwebCategory%5D%5BSound%5D=on&"
        "filters%5BperiodString%5D%5BFirst+World+War%5D=on"
    ]


async def test_fetch_happy_path_populates_source_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://www.iwm.org.uk/collections/item/object/205226579"
    _patch_browser(monkeypatch, _fixture("item-battle-of-britain.html"))

    source = await iwm.fetch(url)

    assert source is not None
    assert source.source_kind == "iwm_search"
    assert source.url == url
    assert source.title == "BATTLE OF BRITAIN"
    assert source.metadata["object_type"] == "Photographs"
    assert source.metadata["period"] == (
        "Second World War (production), Second World War (content)"
    )
    assert source.metadata["collection"] == (
        "FOREIGN OFFICE POLITICAL INTELLIGENCE DEPARTMENT (PID) SECOND WORLD WAR "
        "PHOTOGRAPH LIBRARY: CLASSIFIED PRINT COLLECTION"
    )
    assert source.metadata["catalogue_id"] == "HU 810"
    assert "## Object Details" in source.cleaned_text
    assert "newspaper seller" in source.cleaned_text


async def test_search_empty_fixture_returns_empty_without_drift_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _patch_browser(monkeypatch, _fixture("results-empty.html"))

    with caplog.at_level(logging.WARNING):
        results = await iwm.search("definitely absent", max_results=20)

    assert results == []
    assert "selector drift" not in caplog.text


async def test_selector_drift_warning_writes_diagnostic_dump(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _patch_browser(monkeypatch, _fixture("results-drift.html"))
    monkeypatch.setattr(iwm, "_DIAGNOSTICS_DIR", tmp_path)

    with caplog.at_level(logging.WARNING):
        results = await iwm.search("Battle of Britain", max_results=20)

    assert results == []
    assert "iwm search selector drift" in caplog.text
    assert list(tmp_path.glob("search-selector-drift-*.html"))
    assert list(tmp_path.glob("search-selector-drift-*.png"))


async def test_rate_limit_is_half_rps(monkeypatch: pytest.MonkeyPatch) -> None:
    browser.reset_for_tests()
    iwm.reset_for_tests()
    clock = [100.0]
    sleep_calls: list[float] = []

    def _monotonic() -> float:
        return clock[0]

    async def _sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(browser.time, "monotonic", _monotonic)
    monkeypatch.setattr(browser.asyncio, "sleep", _sleep)

    await browser.throttle("https://www.iwm.org.uk/collections/search")
    clock[0] += 0.25
    await browser.throttle("https://www.iwm.org.uk/collections/item/object/205226579")

    assert sleep_calls == pytest.approx([1.75])


async def test_fetch_rejects_foreign_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(*_args, **_kwargs):
        raise AssertionError("foreign hosts must not launch Playwright")

    monkeypatch.setattr(iwm.browser, "browser_session", _boom)

    assert await iwm.fetch("https://example.com/collections/item/object/1") is None
    attacker_url = "https://www.iwm.org.uk.attacker.example/collections/item/object/1"
    assert await iwm.fetch(attacker_url) is None


def test_source_kind_accepts_iwm_search() -> None:
    result = SearchResult(
        url="https://www.iwm.org.uk/collections/item/object/205226579",
        title="t",
        snippet="s",
        source_kind="iwm_search",
    )
    assert result.source_kind == "iwm_search"
    source = Source(
        url="https://www.iwm.org.uk/collections/item/object/205226579",
        title="t",
        cleaned_text="metadata",
        fetched_at=datetime.now(UTC),
        source_kind="iwm_search",
    )
    assert source.source_kind == "iwm_search"


def test_smoke_registry_includes_iwm_search() -> None:
    from research_agent.tools import TOOL_REGISTRY

    assert "iwm_search" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["iwm_search"])


def test_smoke_wrapper_requires_non_empty_iwm_title_and_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def fake_search(query: str, *, max_results: int = 20):
        assert query == "Battle of Britain"
        assert max_results == 5
        return [
            SearchResult(
                url="https://www.iwm.org.uk/collections/item/object/205226579",
                title="BATTLE OF BRITAIN",
                snippet="HU 810 Photographs",
                source_kind="iwm_search",
                extras={
                    "object_type": "Photographs",
                    "period": "Second World War",
                    "collection": "PID SECOND WORLD WAR PHOTOGRAPH LIBRARY",
                    "catalogue_id": "HU 810",
                },
            )
        ]

    async def fake_shutdown() -> None:
        return None

    monkeypatch.setattr(iwm, "search", fake_search)
    monkeypatch.setattr(iwm.browser, "shutdown", fake_shutdown)

    out = TOOL_REGISTRY["iwm_search"]("Battle of Britain")

    assert "iwm_search: returned 1 hits for query: Battle of Britain" in out
    assert "BATTLE OF BRITAIN" in out
    assert "https://www.iwm.org.uk/collections/item/object/205226579" in out
    assert "object_type: Photographs" in out
    assert "catalogue_id: HU 810" in out


def test_registered_kind_links_iwm_skill() -> None:
    from research_agent.tools._registry import get_kind

    entry = get_kind("iwm_search")
    assert entry is not None
    assert entry.skill_name == "iwm"
    assert entry.fetch_fn is iwm.fetch
    assert entry.host_patterns == ("iwm.org.uk", "www.iwm.org.uk")
    assert "object_category" in entry.optional_payload_knobs
    assert "related_period" in entry.optional_payload_knobs


def test_doctor_registry_skill_coherence_passes_for_iwm() -> None:
    from research_agent import doctor

    rows = doctor.check_registry_skill_coherence()
    iwm_rows = [row for row in rows if row.name == "registry_skill:iwm_search"]

    assert len(iwm_rows) == 1
    assert iwm_rows[0].status == "ok"


def test_iwm_skill_covers_required_operator_topics() -> None:
    from research_agent.skills import load_skill

    body = load_skill("connectors", "iwm")

    for needle in (
        "read-only public browser connector",
        "not an API",
        "data/diagnostics/iwm/",
        "Object Category",
        "Related Period",
        "Photographs",
        "oral histories",
        "documents",
        "Algeria",
        "Falklands",
        "0.5 RPS",
        'metadata["object_type"]',
        'metadata["period"]',
        'metadata["collection"]',
        'metadata["catalogue_id"]',
        "No auth",
    ):
        assert needle in body
