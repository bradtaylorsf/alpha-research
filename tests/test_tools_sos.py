"""Tests for `research_agent.tools.sos` (issue #101)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from research_agent.tools import sos

# ---------------------------------------------------------------------------
# Fakes — Playwright surface area sufficient to exercise sos.py.
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

    monkeypatch.setattr(sos.browser, "browser_session", _session)
    monkeypatch.setattr(sos.browser, "navigate", _navigate)
    return captured


def _build_row(
    *,
    name: str,
    href: str = "",
    entity_number: str = "",
    entity_type: str = "",
    status: str = "",
    formed: str = "",
    filing_date: str = "",
    registered_agent: str = "",
) -> _FakeLocator:
    """Build a row whose child selectors mirror the live bizfileonline DOM.

    Cell-1 is rendered as ``"<NAME> (<entity_number>)\\nClick to expand"`` —
    the test mirrors that, since the connector parses the entity number
    out of the displayed text rather than reading a separate column.
    """
    recipe = sos._STATE_RECIPES["CA"]
    cell_1_text = (
        f"{name} ({entity_number})\nClick to expand" if entity_number else name
    )
    return _FakeLocator(
        children={
            recipe["name_selector"]: _FakeLocator(text=cell_1_text),
            recipe["link_selector"]: _FakeLocator(text=name, attrs={"href": href}),
            recipe["filing_date_selector"]: _FakeLocator(text=filing_date),
            recipe["status_selector"]: _FakeLocator(text=status),
            recipe["type_selector"]: _FakeLocator(text=entity_type),
            recipe["formed_date_selector"]: _FakeLocator(text=formed),
            recipe["row_agent_selector"]: _FakeLocator(text=registered_agent),
        }
    )


def _ca_search_page(rows: list[_FakeLocator]) -> _FakePage:
    recipe = sos._STATE_RECIPES["CA"]
    return _FakePage(
        {
            recipe["query_input"]: _FakeLocator(),
            recipe["submit_button"]: _FakeLocator(),
            recipe["row_selector"]: _FakeLocator(items=rows),
        }
    )


def _ca_profile_page(
    *,
    title: str = "SBI BUILDERS, LLC",
    entity_number: str = "201234567890",
    entity_type: str = "Limited Liability Company",
    status: str = "Active",
    formed: str = "2012-03-15",
    agent: str = "Jane Q Agent — 5678 Agent Way, Sacramento, CA",
    principal: str = "1234 Main St, San Jose, CA 95110",
    officers: list[str] | None = None,
    soi_rows: list[str] | None = None,
    filing_rows: list[str] | None = None,
) -> _FakePage:
    recipe = sos._STATE_RECIPES["CA"]
    selectors: dict[str, _FakeLocator] = {
        "h1": _FakeLocator(text=title),
        recipe["profile_entity_number_selector"]: _FakeLocator(text=entity_number),
        recipe["profile_type_selector"]: _FakeLocator(text=entity_type),
        recipe["profile_status_selector"]: _FakeLocator(text=status),
        recipe["profile_formed_date_selector"]: _FakeLocator(text=formed),
        recipe["agent_selector"]: _FakeLocator(text=agent),
        recipe["principal_address_selector"]: _FakeLocator(text=principal),
        recipe["officers_selector"]: _FakeLocator(
            items=[_FakeLocator(text=t) for t in (officers or [])]
        ),
        recipe["soi_history_selector"]: _FakeLocator(
            items=[_FakeLocator(text=t) for t in (soi_rows or [])]
        ),
        recipe["filing_history_selector"]: _FakeLocator(
            items=[_FakeLocator(text=t) for t in (filing_rows or [])]
        ),
    }
    return _FakePage(selectors)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    sos.reset_for_tests()
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    yield
    sos.reset_for_tests()


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_returns_results_for_name_query(monkeypatch):
    rows = [
        _build_row(
            name="SBI BUILDERS, LLC",
            entity_number="201234567890",
            entity_type="Limited Liability Company",
            status="Active",
            formed="2012-03-15",
            filing_date="04/14/2012",
            registered_agent="Jane Q Agent",
        ),
        _build_row(
            name="SBI BUILDERS INC",
            entity_number="9876543",
            entity_type="Stock Corporation - CA - General",
            status="Dissolved",
            formed="1998-07-22",
            filing_date="07/22/1998",
            registered_agent="Bob Builder",
        ),
    ]
    page = _ca_search_page(rows)
    captured = _stub_browser(monkeypatch, page)

    results = await sos.search("SBI Builders", state="CA", max_results=5)

    assert captured["navigations"] == [sos._STATE_RECIPES["CA"]["search_url"]]
    assert len(results) == 2

    top = results[0]
    assert top.source_kind == "sos"
    # "Click to expand" hint and trailing parenthetical are stripped from the title.
    assert top.title == "SBI BUILDERS, LLC"
    # No anchor href in the live DOM; URL is a synthetic search-anchored link
    # keyed on the parsed entity number.
    assert top.url == (
        "https://bizfileonline.sos.ca.gov/search/business?q=201234567890"
    )
    assert top.extras["entity_number"] == "201234567890"
    assert top.extras["entity_type"] == "Limited Liability Company"
    assert top.extras["status"] == "Active"
    assert top.extras["formed_date"] == "2012-03-15"
    assert top.extras["filing_date"] == "04/14/2012"
    assert top.extras["registered_agent"] == "Jane Q Agent"
    assert top.extras["state"] == "CA"
    assert "Active" in top.snippet


async def test_search_handles_entity_number_query(monkeypatch):
    rows = [
        _build_row(
            name="SBI BUILDERS INC",
            entity_number="9876543",
            entity_type="Stock Corporation - CA - General",
            status="Active",
            formed="1998-07-22",
        ),
    ]
    page = _ca_search_page(rows)
    _stub_browser(monkeypatch, page)

    results = await sos.search("C9876543", state="CA", max_results=5)
    assert len(results) == 1
    # Entity number parsed from the cell-1 parenthetical, not the query.
    assert results[0].extras["entity_number"] == "9876543"
    # Query input still got the entity number string filled in.
    query_locator = page._selector_map[sos._STATE_RECIPES["CA"]["query_input"]]
    assert query_locator.fill_calls == ["C9876543"]


async def test_entity_number_regex_recognises_ca_formats():
    assert sos._looks_like_entity_number("C1234567")
    assert sos._looks_like_entity_number("201234567890")
    assert sos._looks_like_entity_number("2741233")  # 7-digit stock-corp number
    assert not sos._looks_like_entity_number("SBI Builders")
    assert not sos._looks_like_entity_number("")


async def test_split_name_and_number_strips_hint_and_parses_number():
    """Cell-1 reads "<NAME> (<entity_number>)\\nClick to expand" — title is
    cleaned and the trailing parenthetical is parsed into a separate field.
    """
    name, number = sos._split_name_and_number(
        "SBI BUILDERS, INC. (2741233)\nClick to expand"
    )
    assert name == "SBI BUILDERS, INC."
    assert number == "2741233"

    # No parens: name kept verbatim, number empty.
    name2, number2 = sos._split_name_and_number("PLAIN COMPANY NAME")
    assert name2 == "PLAIN COMPANY NAME"
    assert number2 == ""

    # Empty input: both empty.
    assert sos._split_name_and_number("") == ("", "")


async def test_search_returns_empty_for_unknown_state(monkeypatch, caplog):
    page = _ca_search_page([])
    _stub_browser(monkeypatch, page)

    with caplog.at_level(logging.WARNING, logger=sos.logger.name):
        results = await sos.search("anything", state="ZZ")
    assert results == []
    assert any("no recipe" in rec.message for rec in caplog.records)


@pytest.mark.parametrize("state", ["DE", "NV", "WY", "FL", "NY"])
async def test_search_returns_empty_for_stub_states(monkeypatch, caplog, state):
    page = _ca_search_page([])
    _stub_browser(monkeypatch, page)

    with caplog.at_level(logging.WARNING, logger=sos.logger.name):
        results = await sos.search("anything", state=state)
    assert results == []
    assert any("stub" in rec.message.lower() for rec in caplog.records)


async def test_search_empty_query_returns_empty(monkeypatch):
    page = _ca_search_page([])
    _stub_browser(monkeypatch, page)
    assert await sos.search("", state="CA") == []
    assert await sos.search("   ", state="CA") == []


async def test_search_selector_miss_returns_empty_and_logs(
    monkeypatch, caplog, tmp_path
):
    recipe = sos._STATE_RECIPES["CA"]
    page = _FakePage(
        {
            recipe["query_input"]: _FakeLocator(),
            recipe["submit_button"]: _FakeLocator(),
            recipe["row_selector"]: _FakeLocator(raise_on_all=True),
        }
    )
    _stub_browser(monkeypatch, page)
    monkeypatch.setattr(sos, "_DIAGNOSTICS_DIR", tmp_path / "diagnostics")

    with caplog.at_level(logging.WARNING, logger=sos.logger.name):
        results = await sos.search("anything", state="CA")

    assert results == []
    assert any("selector miss" in rec.message for rec in caplog.records)
    assert page.screenshots, "expected a diagnostic screenshot path"
    assert page.screenshots[0].endswith(".png")


async def test_search_respects_max_results(monkeypatch):
    rows = [
        _build_row(
            name=f"Company {i}",
            entity_number=f"{i:012d}",
            entity_type="LLC",
            status="Active",
            formed="2020-01-01",
        )
        for i in range(10)
    ]
    page = _ca_search_page(rows)
    _stub_browser(monkeypatch, page)

    results = await sos.search("anything", state="CA", max_results=3)
    assert len(results) == 3


async def test_search_returns_empty_when_rows_never_render(
    monkeypatch, caplog, tmp_path
):
    """If the React table never paints, ``wait_for`` raises and we bail with a
    diagnostic screenshot rather than parsing whatever stale DOM was there.
    """
    recipe = sos._STATE_RECIPES["CA"]
    page = _FakePage(
        {
            recipe["query_input"]: _FakeLocator(),
            recipe["submit_button"]: _FakeLocator(),
            recipe["row_selector"]: _FakeLocator(raise_on_wait_for=True),
        }
    )
    _stub_browser(monkeypatch, page)
    monkeypatch.setattr(sos, "_DIAGNOSTICS_DIR", tmp_path / "diagnostics")

    with caplog.at_level(logging.WARNING, logger=sos.logger.name):
        results = await sos.search("anything", state="CA")

    assert results == []
    assert any("did not render" in rec.message for rec in caplog.records)
    assert page.screenshots, "expected a diagnostic screenshot path"


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def test_fetch_builds_markdown_source(monkeypatch):
    page = _ca_profile_page(
        officers=["Alice Builder — Manager", "Bob Builder — Member"],
        soi_rows=["2024-03-12 — Statement of Information", "2023-03-08 — SOI"],
        filing_rows=["2024-03-12 — SI-LLC", "2012-03-15 — Articles of Organization"],
    )
    _stub_browser(monkeypatch, page)

    url = "https://bizfileonline.sos.ca.gov/business/201234567890"
    source = await sos.fetch(url)

    assert source is not None
    assert source.source_kind == "sos"
    assert source.title == "SBI BUILDERS, LLC"
    assert source.url == url

    body = source.cleaned_text
    assert "# SBI BUILDERS, LLC" in body
    assert "201234567890" in body
    assert "Active" in body
    assert "## Registered agent" in body
    assert "Jane Q Agent" in body
    assert "## Principal address" in body
    assert "1234 Main St" in body
    assert "## Officers" in body
    assert "Alice Builder" in body
    assert "## Statements of Information" in body
    assert "Statement of Information" in body
    assert "## Filing history" in body
    assert "Articles of Organization" in body

    md = source.metadata
    assert md["entity_number"] == "201234567890"
    assert md["entity_type"] == "Limited Liability Company"
    assert md["status"] == "Active"
    assert md["formed_date"] == "2012-03-15"
    assert "Jane Q Agent" in md["registered_agent"]
    assert "1234 Main St" in md["principal_address"]
    assert len(md["officers"]) == 2
    assert len(md["statements_of_information"]) == 2
    assert len(md["filings"]) == 2


async def test_fetch_rejects_unknown_host(monkeypatch):
    """Look-alike hosts must be rejected without opening a browser."""

    @asynccontextmanager
    async def _no_session(headful=None, block_media=True):
        raise AssertionError("should not open browser")
        yield  # pragma: no cover

    monkeypatch.setattr(sos.browser, "browser_session", _no_session)

    spoof = "https://bizfileonline.sos.ca.gov.attacker.example/business/1"
    assert await sos.fetch(spoof) is None
    assert await sos.fetch("https://opencorporates.com/companies/us_ca/1") is None


async def test_fetch_rejects_stub_state_hosts(monkeypatch):
    """DE / NV / WY / FL / NY are stubs — fetch must not open them."""

    @asynccontextmanager
    async def _no_session(headful=None, block_media=True):
        raise AssertionError("should not open browser")
        yield  # pragma: no cover

    monkeypatch.setattr(sos.browser, "browser_session", _no_session)

    # icis.corp.delaware.gov is in the DE recipe but flagged as a stub.
    assert await sos.fetch("https://icis.corp.delaware.gov/eCorp/EntitySearch") is None


async def test_fetch_returns_none_for_empty_url():
    assert await sos.fetch("") is None


async def test_fetch_handles_minimal_profile(monkeypatch):
    """A profile with only a title still rounds-trips through the markdown builder."""
    recipe = sos._STATE_RECIPES["CA"]
    page = _FakePage(
        {
            "h1": _FakeLocator(text="ACME LLC"),
            recipe["profile_entity_number_selector"]: _FakeLocator(),
            recipe["profile_type_selector"]: _FakeLocator(),
            recipe["profile_status_selector"]: _FakeLocator(),
            recipe["profile_formed_date_selector"]: _FakeLocator(),
            recipe["agent_selector"]: _FakeLocator(),
            recipe["principal_address_selector"]: _FakeLocator(),
            recipe["officers_selector"]: _FakeLocator(items=[]),
            recipe["soi_history_selector"]: _FakeLocator(items=[]),
            recipe["filing_history_selector"]: _FakeLocator(items=[]),
        }
    )
    _stub_browser(monkeypatch, page)

    url = "https://bizfileonline.sos.ca.gov/business/000"
    source = await sos.fetch(url)
    assert source is not None
    assert source.title == "ACME LLC"
    assert "# ACME LLC" in source.cleaned_text


# ---------------------------------------------------------------------------
# Source kind literal & smoke registration
# ---------------------------------------------------------------------------


def test_source_kind_literal():
    from research_agent.tools.models import SearchResult

    result = SearchResult(
        url="https://bizfileonline.sos.ca.gov/business/1",
        title="t",
        snippet="s",
        source_kind="sos",
    )
    assert result.source_kind == "sos"


def test_smoke_registry_includes_sos():
    from research_agent.tools import TOOL_REGISTRY

    assert "sos" in TOOL_REGISTRY
