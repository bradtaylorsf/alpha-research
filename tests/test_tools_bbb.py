"""Tests for `research_agent.tools.bbb` (issue #95)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import playwright.async_api
import pytest

from research_agent.tools import bbb

# ---------------------------------------------------------------------------
# Fakes — Playwright surface area sufficient to exercise bbb.py.
# ---------------------------------------------------------------------------


class _FakeLocator:
    def __init__(
        self,
        *,
        text: str = "",
        attrs: dict[str, str] | None = None,
        children: dict[str, _FakeLocator] | None = None,
        items: list[_FakeLocator] | None = None,
        on_click: Any = None,
        raise_on_locator: bool = False,
        raise_on_all: bool = False,
        raise_on_wait_for: bool = False,
        raise_on_click: bool = False,
    ) -> None:
        self._text = text
        self._attrs = attrs or {}
        self._children = children or {}
        self._items = items or []
        self._on_click = on_click
        self._raise_on_locator = raise_on_locator
        self._raise_on_all = raise_on_all
        self._raise_on_wait_for = raise_on_wait_for
        self._raise_on_click = raise_on_click
        self.click_calls: int = 0
        self.wait_for_calls: int = 0

    @property
    def first(self) -> _FakeLocator:
        return self

    async def all(self) -> list[_FakeLocator]:
        if self._raise_on_all:
            raise RuntimeError("selector drift")
        return list(self._items)

    async def inner_text(self) -> str:
        return self._text

    async def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)

    async def click(self) -> None:
        self.click_calls += 1
        if self._raise_on_click:
            raise RuntimeError("click failed")
        if self._on_click is not None:
            self._on_click()

    async def wait_for(self, *, timeout: int = 0) -> None:  # noqa: ARG002
        self.wait_for_calls += 1
        if self._raise_on_wait_for:
            raise RuntimeError("cards did not render")

    def locator(self, selector: str) -> _FakeLocator:
        if self._raise_on_locator:
            raise RuntimeError("selector drift")
        return self._children.get(selector, _FakeLocator())


class _FakePage:
    def __init__(self, selector_map: dict[str, _FakeLocator]) -> None:
        self._selector_map = selector_map
        self.closed = False
        self.screenshots: list[str] = []
        self.locator_calls: list[str] = []
        self.click_counts: dict[str, int] = {}

    def locator(self, selector: str) -> _FakeLocator:
        self.locator_calls.append(selector)
        loc = self._selector_map.get(selector, _FakeLocator())
        # Tally clicks per selector. Wrap each child item's existing click
        # with a counter hook so we can assert reveal-button click counts
        # in the show-more test.
        for item in list(loc._items):
            original = item._on_click

            def _make_hook(sel: str, prev: Any) -> Any:
                def _hook() -> None:
                    self.click_counts[sel] = self.click_counts.get(sel, 0) + 1
                    if prev is not None:
                        prev()

                return _hook

            item._on_click = _make_hook(selector, original)
        return loc

    async def close(self) -> None:
        self.closed = True

    async def screenshot(self, *, path: str) -> None:
        self.screenshots.append(path)


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self._page = page

    async def new_page(self) -> _FakePage:
        return self._page


def _stub_browser(monkeypatch, page: _FakePage) -> dict[str, list[str]]:
    captured: dict[str, list[str]] = {"navigations": []}

    @asynccontextmanager
    async def _session(headful=None, block_media=True):
        yield _FakeContext(page)

    async def _navigate(p, url, **kwargs):
        captured["navigations"].append(url)

    monkeypatch.setattr(bbb.browser, "browser_session", _session)
    monkeypatch.setattr(bbb.browser, "navigate", _navigate)
    return captured


def _build_card(*, name: str, href: str = "", rating: str = "", location: str = "") -> _FakeLocator:
    return _FakeLocator(
        children={
            bbb._RESULT_NAME_SELECTOR: _FakeLocator(text=name),
            bbb._RESULT_LINK_SELECTOR: _FakeLocator(text=name, attrs={"href": href}),
            bbb._RESULT_RATING_SELECTOR: _FakeLocator(text=rating),
            bbb._RESULT_LOCATION_SELECTOR: _FakeLocator(text=location),
        }
    )


def _search_page(cards: list[_FakeLocator]) -> _FakePage:
    return _FakePage({bbb._RESULT_CARD_SELECTOR: _FakeLocator(items=cards)})


def _profile_page(
    *,
    title: str = "SBI Builders Inc.",
    rating: str = "A+",
    accreditation: str = "BBB Accredited Business since 2015",
    complaints_12mo: str = "3",
    complaints_3yr: str = "12",
    categories: list[str] | None = None,
    government_actions: str = "",
    reveal_buttons: dict[str, list[_FakeLocator]] | None = None,
) -> _FakePage:
    selectors: dict[str, _FakeLocator] = {
        "h1": _FakeLocator(text=title),
        bbb._PROFILE_RATING_SELECTOR: _FakeLocator(text=rating),
        bbb._PROFILE_ACCREDITATION_SELECTOR: _FakeLocator(text=accreditation),
        bbb._PROFILE_COMPLAINTS_12MO_SELECTOR: _FakeLocator(text=complaints_12mo),
        bbb._PROFILE_COMPLAINTS_3YR_SELECTOR: _FakeLocator(text=complaints_3yr),
        bbb._PROFILE_COMPLAINT_CATEGORIES_SELECTOR: _FakeLocator(
            items=[_FakeLocator(text=c) for c in (categories or [])]
        ),
        bbb._PROFILE_GOVERNMENT_ACTIONS_SELECTOR: _FakeLocator(text=government_actions),
    }
    # Default: every reveal selector resolves to an empty locator (no buttons).
    for selector in bbb._REVEAL_BUTTON_SELECTORS:
        items = (reveal_buttons or {}).get(selector, [])
        selectors[selector] = _FakeLocator(items=items)
    return _FakePage(selectors)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    bbb.reset_for_tests()
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    yield
    bbb.reset_for_tests()


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_returns_parsed_cards(monkeypatch):
    cards = [
        _build_card(
            name="SBI Builders Inc.",
            href="/us/ca/san-jose/profile/general-contractor/sbi-builders-1234",
            rating="A+",
            location="San Jose, CA",
        ),
        _build_card(
            name="SBI Builders LLC",
            href="https://www.bbb.org/us/tx/austin/profile/general-contractor/sbi-builders-llc-5678",
            rating="B",
            location="Austin, TX",
        ),
    ]
    page = _search_page(cards)
    captured = _stub_browser(monkeypatch, page)

    results = await bbb.search("SBI Builders", max_results=5)

    assert captured["navigations"] == [
        "https://www.bbb.org/search?find_country=USA&find_text=SBI+Builders"
    ]
    assert len(results) == 2

    top = results[0]
    assert top.source_kind == "bbb"
    assert top.title == "SBI Builders Inc."
    assert top.url == (
        "https://www.bbb.org/us/ca/san-jose/profile/general-contractor/sbi-builders-1234"
    )
    assert top.extras["rating"] == "A+"
    assert top.extras["location"] == "San Jose, CA"
    assert "A+" in top.snippet
    assert "San Jose, CA" in top.snippet

    second = results[1]
    # Absolute URL kept verbatim.
    assert second.url == (
        "https://www.bbb.org/us/tx/austin/profile/general-contractor/sbi-builders-llc-5678"
    )


async def test_search_empty_query_returns_empty(monkeypatch):
    page = _search_page([])
    _stub_browser(monkeypatch, page)
    assert await bbb.search("") == []
    assert await bbb.search("   ") == []


async def test_search_selector_miss_writes_diagnostic(monkeypatch, caplog, tmp_path):
    page = _FakePage({bbb._RESULT_CARD_SELECTOR: _FakeLocator(raise_on_all=True)})
    _stub_browser(monkeypatch, page)
    monkeypatch.setattr(bbb, "_DIAGNOSTICS_DIR", tmp_path / "diagnostics")

    with caplog.at_level(logging.WARNING, logger=bbb.logger.name):
        results = await bbb.search("anything")

    assert results == []
    assert any("selector miss" in rec.message for rec in caplog.records)
    assert page.screenshots, "expected a diagnostic screenshot path"
    assert page.screenshots[0].endswith(".png")


async def test_search_returns_empty_when_cards_never_render(
    monkeypatch, caplog, tmp_path
):
    """If the React app never paints cards, ``wait_for`` raises and we bail
    with a diagnostic screenshot rather than parsing whatever stale DOM was
    there.
    """
    page = _FakePage(
        {bbb._RESULT_CARD_SELECTOR: _FakeLocator(raise_on_wait_for=True)}
    )
    _stub_browser(monkeypatch, page)
    monkeypatch.setattr(bbb, "_DIAGNOSTICS_DIR", tmp_path / "diagnostics")

    with caplog.at_level(logging.WARNING, logger=bbb.logger.name):
        results = await bbb.search("anything")

    assert results == []
    assert any("did not render" in rec.message for rec in caplog.records)
    assert page.screenshots


async def test_search_respects_max_results(monkeypatch):
    cards = [
        _build_card(name=f"Company {i}", href=f"/us/ca/x/profile/y/company-{i}", rating="A")
        for i in range(10)
    ]
    page = _search_page(cards)
    _stub_browser(monkeypatch, page)

    results = await bbb.search("anything", max_results=3)
    assert len(results) == 3


async def test_search_swallows_playwright_errors(monkeypatch, caplog):
    @asynccontextmanager
    async def _session(headful=None, block_media=True):
        raise playwright.async_api.Error("browser crashed")
        yield  # pragma: no cover

    monkeypatch.setattr(bbb.browser, "browser_session", _session)

    with caplog.at_level(logging.WARNING, logger=bbb.logger.name):
        results = await bbb.search("anything")

    assert results == []
    assert any("playwright error" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def test_fetch_builds_markdown_source(monkeypatch):
    page = _profile_page(
        categories=["Advertising/Sales Issues — 4", "Billing/Collection Issues — 2"],
        government_actions="No government actions on file.",
    )
    _stub_browser(monkeypatch, page)

    url = "https://www.bbb.org/us/ca/san-jose/profile/general-contractor/sbi-builders-1234"
    source = await bbb.fetch(url)

    assert source is not None
    assert source.source_kind == "bbb"
    assert source.title == "SBI Builders Inc."
    assert source.url == url

    body = source.cleaned_text
    assert "# SBI Builders Inc." in body
    assert "## Rating" in body
    assert "A+" in body
    assert "## Accreditation" in body
    assert "BBB Accredited Business" in body
    assert "## Complaints (12mo / 3yr)" in body
    assert "Last 12 months: 3" in body
    assert "Last 3 years: 12" in body
    assert "## Complaint summary categories" in body
    assert "Advertising/Sales Issues" in body
    assert "## Government actions" in body
    assert "No government actions" in body

    md = source.metadata
    assert md["rating"] == "A+"
    assert "BBB Accredited Business" in md["accreditation"]
    assert md["complaints_12mo"] == "3"
    assert md["complaints_3yr"] == "12"
    assert len(md["complaint_categories"]) == 2
    assert md["government_actions"]


async def test_fetch_rejects_non_bbb_host(monkeypatch):
    """Look-alike hosts must be rejected without opening a browser."""

    @asynccontextmanager
    async def _no_session(headful=None, block_media=True):
        raise AssertionError("should not open browser")
        yield  # pragma: no cover

    monkeypatch.setattr(bbb.browser, "browser_session", _no_session)

    spoof = "https://www.bbb.org.attacker.example/us/ca/profile/x/y"
    assert await bbb.fetch(spoof) is None
    assert await bbb.fetch("https://opencorporates.com/companies/us_ca/1") is None
    assert await bbb.fetch("") is None


async def test_fetch_clicks_show_more_reveals_before_scraping(monkeypatch):
    """Complaint bodies are gated behind 'Show more' / 'Show full complaint'
    reveals — fetch() must click every visible reveal button before the
    scraping pass so the rolled-up markdown captures the full text.
    """
    show_more_buttons = [_FakeLocator(text="Show more"), _FakeLocator(text="Show more")]
    show_full_buttons = [_FakeLocator(text="Show full complaint")]
    read_more_buttons: list[_FakeLocator] = []

    page = _profile_page(
        categories=["Service Issues — 1"],
        reveal_buttons={
            "button:has-text('Show more')": show_more_buttons,
            "button:has-text('Show full complaint')": show_full_buttons,
            "button:has-text('Read more')": read_more_buttons,
        },
    )
    _stub_browser(monkeypatch, page)

    url = "https://www.bbb.org/us/ca/san-jose/profile/general-contractor/sbi-builders-1234"
    source = await bbb.fetch(url)

    assert source is not None
    # Three reveals fired: 2 "Show more" + 1 "Show full complaint".
    total_clicks = sum(b.click_calls for b in show_more_buttons + show_full_buttons)
    assert total_clicks == 3


async def test_fetch_swallows_reveal_click_errors(monkeypatch):
    """A reveal button that raises on click must not block the scrape — the
    show-more controls are best-effort and a click failure is non-fatal.
    """
    flaky = _FakeLocator(text="Show more", raise_on_click=True)
    page = _profile_page(
        categories=["Service Issues — 1"],
        reveal_buttons={"button:has-text('Show more')": [flaky]},
    )
    _stub_browser(monkeypatch, page)

    url = "https://www.bbb.org/us/ca/san-jose/profile/general-contractor/sbi-builders-1234"
    source = await bbb.fetch(url)

    assert source is not None
    assert "## Rating" in source.cleaned_text


async def test_fetch_swallows_playwright_errors(monkeypatch, caplog):
    @asynccontextmanager
    async def _session(headful=None, block_media=True):
        raise playwright.async_api.Error("nav crashed")
        yield  # pragma: no cover

    monkeypatch.setattr(bbb.browser, "browser_session", _session)

    url = "https://www.bbb.org/us/ca/san-jose/profile/x/y"
    with caplog.at_level(logging.WARNING, logger=bbb.logger.name):
        result = await bbb.fetch(url)

    assert result is None
    assert any("playwright error" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Source kind literal & smoke registration
# ---------------------------------------------------------------------------


def test_source_kind_literal():
    from research_agent.tools.models import SearchResult

    result = SearchResult(
        url="https://www.bbb.org/us/ca/san-jose/profile/x/y",
        title="t",
        snippet="s",
        source_kind="bbb",
    )
    assert result.source_kind == "bbb"


def test_smoke_registry_includes_bbb():
    from research_agent.tools import TOOL_REGISTRY

    assert "bbb" in TOOL_REGISTRY
