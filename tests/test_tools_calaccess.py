"""Tests for `research_agent.tools.calaccess` (issue #96)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import playwright.async_api
import pytest

from research_agent.tools import calaccess

# ---------------------------------------------------------------------------
# Fakes — Playwright surface area sufficient to exercise calaccess.py.
# ---------------------------------------------------------------------------


class _FakeLocator:
    def __init__(
        self,
        *,
        text: str = "",
        attrs: dict[str, str] | None = None,
        children: dict[str, _FakeLocator] | None = None,
        items: list[_FakeLocator] | None = None,
        on_fill: Any = None,
        on_click: Any = None,
        raise_on_locator: bool = False,
        raise_on_all: bool = False,
        raise_on_wait_for: bool = False,
    ) -> None:
        self._text = text
        self._attrs = attrs or {}
        self._children = children or {}
        self._items = items or []
        self._on_fill = on_fill
        self._on_click = on_click
        self._raise_on_locator = raise_on_locator
        self._raise_on_all = raise_on_all
        self._raise_on_wait_for = raise_on_wait_for
        self.fill_calls: list[str] = []
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

    async def fill(self, value: str) -> None:
        self.fill_calls.append(value)
        if self._on_fill is not None:
            self._on_fill(value)

    async def click(self) -> None:
        self.click_calls += 1
        if self._on_click is not None:
            self._on_click()

    async def wait_for(self, *, timeout: int = 0) -> None:  # noqa: ARG002
        self.wait_for_calls += 1
        if self._raise_on_wait_for:
            raise RuntimeError("rows did not render")

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

    def locator(self, selector: str) -> _FakeLocator:
        self.locator_calls.append(selector)
        return self._selector_map.get(selector, _FakeLocator())

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

    monkeypatch.setattr(calaccess.browser, "browser_session", _session)
    monkeypatch.setattr(calaccess.browser, "navigate", _navigate)
    return captured


def _build_row(
    *,
    recipe: dict[str, Any],
    primary: str = "",
    committee: str = "",
    amount: str = "",
    date: str = "",
    href: str = "",
) -> _FakeLocator:
    """Build a row whose child selectors mirror the live Power Search DOM."""
    primary_label = recipe["primary_label"]
    primary_selector = recipe[f"{primary_label}_selector"]
    children: dict[str, _FakeLocator] = {
        primary_selector: _FakeLocator(text=primary),
        recipe["committee_selector"]: _FakeLocator(text=committee),
        recipe["amount_selector"]: _FakeLocator(text=amount),
        recipe["date_selector"]: _FakeLocator(text=date),
        recipe["permalink_selector"]: _FakeLocator(
            text=primary, attrs={"href": href}
        ),
    }
    return _FakeLocator(children=children)


def _search_page(recipe: dict[str, Any], rows: list[_FakeLocator]) -> _FakePage:
    return _FakePage(
        {
            recipe["query_input"]: _FakeLocator(),
            recipe["submit_button"]: _FakeLocator(),
            recipe["row_selector"]: _FakeLocator(items=rows),
        }
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    calaccess.reset_for_tests()
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    yield
    calaccess.reset_for_tests()


# ---------------------------------------------------------------------------
# search() — coverage across all three kinds
# ---------------------------------------------------------------------------


async def test_search_contributions_returns_parsed_rows(monkeypatch):
    recipe = calaccess._KIND_RECIPES["contributions"]
    rows = [
        _build_row(
            recipe=recipe,
            primary="Newsom for California Governor 2022",
            committee="Friends of Gavin Newsom",
            amount="$32,400",
            date="2022-09-15",
            href="/contributions/abc123",
        ),
        _build_row(
            recipe=recipe,
            primary="Some Donor LLC",
            committee="Other Committee",
            amount="$5,000",
            date="2022-08-01",
            href="https://powersearch.sos.ca.gov/contributions/xyz",
        ),
    ]
    page = _search_page(recipe, rows)
    captured = _stub_browser(monkeypatch, page)

    results = await calaccess.search(
        "Gavin Newsom", kind="contributions", max_results=5
    )

    assert captured["navigations"] == [recipe["search_url"]]
    assert len(results) == 2

    top = results[0]
    assert top.source_kind == "calaccess"
    assert top.title == "Newsom for California Governor 2022"
    assert top.url == "https://powersearch.sos.ca.gov/contributions/abc123"
    assert top.extras["kind"] == "contributions"
    assert top.extras["donor"] == "Newsom for California Governor 2022"
    assert top.extras["payee"] == ""
    assert top.extras["committee"] == "Friends of Gavin Newsom"
    assert top.extras["amount"] == "$32,400"
    assert top.extras["date"] == "2022-09-15"
    assert top.extras["permalink"] == top.url
    assert "$32,400" in top.snippet
    assert "Friends of Gavin Newsom" in top.snippet

    # Absolute href on second result is preserved verbatim.
    assert results[1].url == "https://powersearch.sos.ca.gov/contributions/xyz"


async def test_search_independent_expenditures_returns_parsed_rows(monkeypatch):
    recipe = calaccess._KIND_RECIPES["independent_expenditures"]
    rows = [
        _build_row(
            recipe=recipe,
            primary="Acme Media Buy LLC",
            committee="Stop Prop 99 PAC",
            amount="$120,000",
            date="2022-10-25",
            href="/independent-expenditures/ie-1",
        ),
    ]
    page = _search_page(recipe, rows)
    _stub_browser(monkeypatch, page)

    results = await calaccess.search(
        "Prop 99", kind="independent_expenditures", max_results=5
    )

    assert len(results) == 1
    top = results[0]
    assert top.extras["kind"] == "independent_expenditures"
    assert top.extras["payee"] == "Acme Media Buy LLC"
    assert top.extras["donor"] == ""
    assert top.extras["committee"] == "Stop Prop 99 PAC"
    assert top.extras["amount"] == "$120,000"


async def test_search_lobbying_is_documented_gap(monkeypatch, caplog):
    """Power Search does not include lobbying — the connector returns ``[]``
    with a clear WARN rather than scraping the wrong frameset.
    """

    @asynccontextmanager
    async def _no_session(headful=None, block_media=True):
        raise AssertionError("should not open browser for documented-gap kind")
        yield  # pragma: no cover

    monkeypatch.setattr(calaccess.browser, "browser_session", _no_session)

    with caplog.at_level(logging.WARNING, logger=calaccess.logger.name):
        results = await calaccess.search("Megacorp", kind="lobbying", max_results=5)

    assert results == []
    assert any("not implemented" in rec.message for rec in caplog.records)


async def test_search_unknown_kind_warns_and_returns_empty(monkeypatch, caplog):
    page = _search_page(calaccess._KIND_RECIPES["contributions"], [])
    _stub_browser(monkeypatch, page)

    with caplog.at_level(logging.WARNING, logger=calaccess.logger.name):
        results = await calaccess.search("anything", kind="bogus")
    assert results == []
    assert any("unknown kind" in rec.message for rec in caplog.records)


async def test_search_empty_query_returns_empty(monkeypatch):
    page = _search_page(calaccess._KIND_RECIPES["contributions"], [])
    _stub_browser(monkeypatch, page)
    assert await calaccess.search("", kind="contributions") == []
    assert await calaccess.search("   ", kind="contributions") == []


async def test_search_returns_empty_when_rows_never_render(
    monkeypatch, caplog, tmp_path
):
    """If the Vue/React table never paints, ``wait_for`` raises and we bail
    with a diagnostic screenshot rather than parsing whatever stale DOM was
    left over from a previous render.
    """
    recipe = calaccess._KIND_RECIPES["contributions"]
    page = _FakePage(
        {
            recipe["query_input"]: _FakeLocator(),
            recipe["submit_button"]: _FakeLocator(),
            recipe["row_selector"]: _FakeLocator(raise_on_wait_for=True),
        }
    )
    _stub_browser(monkeypatch, page)
    monkeypatch.setattr(calaccess, "_DIAGNOSTICS_DIR", tmp_path / "diagnostics")

    with caplog.at_level(logging.WARNING, logger=calaccess.logger.name):
        results = await calaccess.search("anything", kind="contributions")

    assert results == []
    assert any("did not render" in rec.message for rec in caplog.records)
    assert page.screenshots, "expected a diagnostic screenshot path"


async def test_search_row_extraction_failure_logs_and_skips(
    monkeypatch, caplog
):
    """A row whose child locator raises is logged and skipped without
    aborting the rest of the batch.
    """
    recipe = calaccess._KIND_RECIPES["contributions"]
    bad_row = _FakeLocator(raise_on_locator=True)
    good_row = _build_row(
        recipe=recipe,
        primary="Good Donor",
        committee="Good Committee",
        amount="$100",
        date="2022-01-01",
        href="/contributions/good",
    )
    page = _search_page(recipe, [bad_row, good_row])
    _stub_browser(monkeypatch, page)

    with caplog.at_level(logging.WARNING, logger=calaccess.logger.name):
        results = await calaccess.search("anything", kind="contributions")

    # Only the good row makes it through; the bad one is logged as a row
    # parse failure rather than aborting the entire batch.
    assert len(results) == 1
    assert results[0].extras["donor"] == "Good Donor"
    assert any("row parse failed" in rec.message for rec in caplog.records)


async def test_search_respects_max_results(monkeypatch):
    recipe = calaccess._KIND_RECIPES["contributions"]
    rows = [
        _build_row(
            recipe=recipe,
            primary=f"Donor {i}",
            committee=f"Committee {i}",
            amount=f"${i * 100}",
            date="2022-01-01",
            href=f"/contributions/{i}",
        )
        for i in range(10)
    ]
    page = _search_page(recipe, rows)
    _stub_browser(monkeypatch, page)

    results = await calaccess.search(
        "anything", kind="contributions", max_results=3
    )
    assert len(results) == 3


async def test_search_synth_url_when_href_missing(monkeypatch):
    """When the row's permalink anchor has no href, fall back to the search URL."""
    recipe = calaccess._KIND_RECIPES["contributions"]
    rows = [
        _build_row(
            recipe=recipe,
            primary="Donor No Link",
            committee="Some Committee",
            amount="$1,000",
            date="2022-01-01",
            href="",
        ),
    ]
    page = _search_page(recipe, rows)
    _stub_browser(monkeypatch, page)

    results = await calaccess.search("x", kind="contributions", max_results=1)
    assert len(results) == 1
    assert results[0].url == recipe["search_url"]
    assert results[0].extras["permalink"] == recipe["search_url"]


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def test_fetch_off_host_returns_none_without_browser(monkeypatch):
    """Look-alike hosts must be rejected without opening a browser."""

    @asynccontextmanager
    async def _no_session(headful=None, block_media=True):
        raise AssertionError("should not open browser")
        yield  # pragma: no cover

    monkeypatch.setattr(calaccess.browser, "browser_session", _no_session)

    spoof = "https://powersearch.sos.ca.gov.attacker.example/contributions/1"
    assert await calaccess.fetch(spoof) is None
    assert await calaccess.fetch("https://example.com/contributions/1") is None
    assert await calaccess.fetch("") is None


async def test_fetch_on_host_returns_markdown_source(monkeypatch):
    """On-host detail page renders a markdown Source with metadata populated."""
    page = _FakePage(
        {
            "h1": _FakeLocator(text="Contribution Detail"),
            ".record, [data-section='record']": _FakeLocator(
                text="Record: $32,400 from Donor Inc to Friends of Gavin Newsom"
            ),
            ".parties, [data-section='parties']": _FakeLocator(
                text="Donor Inc → Friends of Gavin Newsom"
            ),
            ".amount, [data-section='amount']": _FakeLocator(text="$32,400"),
            ".date, [data-section='date']": _FakeLocator(text="2022-09-15"),
            ".filing, [data-section='filing']": _FakeLocator(text="Filing #ABC-123"),
        }
    )
    _stub_browser(monkeypatch, page)

    url = "https://powersearch.sos.ca.gov/contributions/abc123"
    source = await calaccess.fetch(url)

    assert source is not None
    assert source.source_kind == "calaccess"
    assert source.title == "Contribution Detail"
    assert source.url == url

    body = source.cleaned_text
    assert "# Contribution Detail" in body
    assert "## Record" in body
    assert "## Parties" in body
    assert "## Amount" in body
    assert "$32,400" in body
    assert "## Date" in body
    assert "2022-09-15" in body
    assert "## Filing reference" in body
    assert "Filing #ABC-123" in body

    md = source.metadata
    assert md["amount"] == "$32,400"
    assert md["date"] == "2022-09-15"
    assert "Donor Inc" in md["parties"]
    assert md["filing_reference"] == "Filing #ABC-123"


async def test_fetch_playwright_error_returns_none(monkeypatch, caplog):
    """A playwright.Error mid-fetch is logged and returns None without crashing."""

    @asynccontextmanager
    async def _boom(headful=None, block_media=True):
        raise playwright.async_api.Error("nav failed")
        yield  # pragma: no cover

    monkeypatch.setattr(calaccess.browser, "browser_session", _boom)

    with caplog.at_level(logging.WARNING, logger=calaccess.logger.name):
        result = await calaccess.fetch(
            "https://powersearch.sos.ca.gov/contributions/abc"
        )
    assert result is None
    assert any("playwright error" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Source kind literal & smoke registration
# ---------------------------------------------------------------------------


def test_source_kind_literal():
    from research_agent.tools.models import SearchResult

    result = SearchResult(
        url="https://powersearch.sos.ca.gov/contributions/1",
        title="t",
        snippet="s",
        source_kind="calaccess",
    )
    assert result.source_kind == "calaccess"


def test_smoke_registry_includes_calaccess():
    from research_agent.tools import TOOL_REGISTRY

    assert "calaccess" in TOOL_REGISTRY
