"""Tests for `research_agent.tools.licensing` (issue #91)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from research_agent.tools import licensing

# ---------------------------------------------------------------------------
# Fakes — Playwright surface area sufficient to exercise licensing.py.
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
        raise_on_click: bool = False,
        raise_on_inner_text: bool = False,
    ) -> None:
        self._text = text
        self._attrs = attrs or {}
        self._children = children or {}
        self._items = items or []
        self._on_fill = on_fill
        self._on_click = on_click
        self._raise_on_locator = raise_on_locator
        self._raise_on_all = raise_on_all
        self._raise_on_click = raise_on_click
        self._raise_on_inner_text = raise_on_inner_text
        self.fill_calls: list[str] = []
        self.click_calls: int = 0

    @property
    def first(self) -> _FakeLocator:
        return self

    async def all(self) -> list[_FakeLocator]:
        if self._raise_on_all:
            raise RuntimeError("selector drift")
        return list(self._items)

    async def inner_text(self) -> str:
        if self._raise_on_inner_text:
            raise RuntimeError("selector miss")
        return self._text

    async def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)

    async def fill(self, value: str) -> None:
        self.fill_calls.append(value)
        if self._on_fill is not None:
            self._on_fill(value)

    async def click(self) -> None:
        self.click_calls += 1
        if self._raise_on_click:
            raise RuntimeError("click failed")
        if self._on_click is not None:
            self._on_click()

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
        self.click_order: list[str] = []

    def locator(self, selector: str) -> _FakeLocator:
        self.locator_calls.append(selector)
        loc = self._selector_map.get(selector, _FakeLocator())
        # Track which selectors are clicked, in order, by hooking each click.
        original = loc._on_click

        def _on_click() -> None:
            self.click_order.append(selector)
            if original is not None:
                original()

        loc._on_click = _on_click
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

    monkeypatch.setattr(licensing.browser, "browser_session", _session)
    monkeypatch.setattr(licensing.browser, "navigate", _navigate)
    return captured


def _build_row(
    *,
    name: str,
    href: str = "",
    license_number: str = "",
    status: str = "",
    classification: str = "",
    expiration: str = "",
) -> _FakeLocator:
    recipe = licensing._STATE_RECIPES["CA"]
    return _FakeLocator(
        children={
            recipe["name_selector"]: _FakeLocator(text=name),
            recipe["link_selector"]: _FakeLocator(text=name, attrs={"href": href}),
            recipe["license_number_selector"]: _FakeLocator(text=license_number),
            recipe["status_selector"]: _FakeLocator(text=status),
            recipe["classification_selector"]: _FakeLocator(text=classification),
            recipe["expiration_selector"]: _FakeLocator(text=expiration),
        }
    )


def _ca_search_page(
    rows: list[_FakeLocator],
    *,
    kind: str = "name",
    tab_locators: dict[str, _FakeLocator] | None = None,
    submit_locator: _FakeLocator | None = None,
) -> _FakePage:
    """Build a fake CSLB search page wired for the ``kind`` tab.

    ``kind`` is "name" (business name) or "number" (license number) and
    determines which input/submit selector the fake exposes. ``tab_locators``
    optionally maps ``"number"``/``"name"`` to per-tab fake locators so a
    test can assert that the right tab button was clicked.
    """
    recipe = licensing._STATE_RECIPES["CA"]
    input_selector = recipe["query_inputs_by_kind"][kind]
    submit_selector = recipe["submit_buttons_by_kind"][kind]
    selectors: dict[str, _FakeLocator] = {
        input_selector: _FakeLocator(),
        submit_selector: submit_locator or _FakeLocator(),
        recipe["row_selector"]: _FakeLocator(items=rows),
    }
    if tab_locators:
        for kind_key, locator in tab_locators.items():
            selectors[recipe["tab_buttons_by_kind"][kind_key]] = locator
    return _FakePage(selectors)


def _ca_profile_page(
    *,
    title: str = "ACME CONSTRUCTION INC",
    license_number: str = "1234567",
    status: str = "Active",
    classification: str = "B - General Building",
    expiration: str = "2026-12-31",
    personnel_text: str = "John Smith — Owner/Officer",
    workers_comp_text: str = "Carrier: State Fund — Policy: WC-9000",
    bonds_text: str = "Contractor's Bond: $25,000 — Surety: ABC Surety",
    disciplinary_text: str = "No disciplinary actions on file.",
    raise_on_personnel_click: bool = False,
    raise_on_disciplinary_inner_text: bool = False,
) -> _FakePage:
    recipe = licensing._STATE_RECIPES["CA"]
    disciplinary_loc = _FakeLocator(
        text=disciplinary_text,
        raise_on_inner_text=raise_on_disciplinary_inner_text,
    )
    selectors: dict[str, _FakeLocator] = {
        "h1": _FakeLocator(text=title),
        recipe["profile_license_number_selector"]: _FakeLocator(text=license_number),
        recipe["profile_status_selector"]: _FakeLocator(text=status),
        recipe["profile_classification_selector"]: _FakeLocator(text=classification),
        recipe["profile_expiration_selector"]: _FakeLocator(text=expiration),
        recipe["personnel_tab_button"]: _FakeLocator(
            raise_on_click=raise_on_personnel_click
        ),
        recipe["personnel_section"]: _FakeLocator(text=personnel_text),
        recipe["workers_comp_tab_button"]: _FakeLocator(),
        recipe["workers_comp_section"]: _FakeLocator(text=workers_comp_text),
        recipe["bonds_tab_button"]: _FakeLocator(),
        recipe["bonds_section"]: _FakeLocator(text=bonds_text),
        recipe["disciplinary_tab_button"]: _FakeLocator(),
        recipe["disciplinary_section"]: disciplinary_loc,
    }
    return _FakePage(selectors)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    licensing.reset_for_tests()
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    yield
    licensing.reset_for_tests()


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_returns_results_for_name_query(monkeypatch):
    rows = [
        _build_row(
            name="ACME CONSTRUCTION INC",
            license_number="1234567",
            status="Active",
            classification="B - General Building",
            expiration="2026-12-31",
        ),
        _build_row(
            name="ACME ROOFING LLC",
            license_number="9876543",
            status="Expired",
            classification="C-39 - Roofing",
            expiration="2023-08-01",
        ),
    ]
    page = _ca_search_page(rows)
    captured = _stub_browser(monkeypatch, page)

    results = await licensing.search("Acme Construction", state="CA", max_results=5)

    assert captured["navigations"] == [
        licensing._STATE_RECIPES["CA"]["search_url"]
    ]
    assert len(results) == 2

    top = results[0]
    assert top.source_kind == "licensing"
    assert top.title == "ACME CONSTRUCTION INC"
    # No href in fake row → URL is search-anchored on the license number.
    assert top.url == (
        "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/"
        "CheckLicense.aspx?LicNum=1234567"
    )
    assert top.extras["license_number"] == "1234567"
    assert top.extras["status"] == "Active"
    assert top.extras["classification"] == "B - General Building"
    assert top.extras["expiration"] == "2026-12-31"
    assert top.extras["state"] == "CA"
    assert "Active" in top.snippet
    assert "B - General Building" in top.snippet


async def test_search_toggles_query_kind_for_license_number(monkeypatch):
    """A numeric query should click the license-number tab, not the name tab."""
    lic_tab = _FakeLocator()
    bus_tab = _FakeLocator()
    page = _ca_search_page(
        [
            _build_row(
                name="ACME CONSTRUCTION INC",
                license_number="1234567",
                status="Active",
                classification="B",
                expiration="2026-12-31",
            )
        ],
        kind="number",
        tab_locators={"number": lic_tab, "name": bus_tab},
    )
    _stub_browser(monkeypatch, page)

    await licensing.search("1234567", state="CA", max_results=5)

    assert lic_tab.click_calls == 1
    assert bus_tab.click_calls == 0


async def test_search_toggles_query_kind_for_business_name(monkeypatch):
    """A non-numeric query should click the business-name tab, not the license-number tab."""
    lic_tab = _FakeLocator()
    bus_tab = _FakeLocator()
    page = _ca_search_page(
        [
            _build_row(
                name="ACME CONSTRUCTION INC",
                license_number="1234567",
                status="Active",
                classification="B",
                expiration="2026-12-31",
            )
        ],
        kind="name",
        tab_locators={"number": lic_tab, "name": bus_tab},
    )
    _stub_browser(monkeypatch, page)

    await licensing.search("Acme Construction", state="CA")

    assert bus_tab.click_calls == 1
    assert lic_tab.click_calls == 0


async def test_license_number_regex_recognises_cslb_format():
    assert licensing._looks_like_license_number("1234567")  # 7 digits
    assert licensing._looks_like_license_number("123456")  # 6 digits
    assert licensing._looks_like_license_number("12345678")  # 8 digits
    assert not licensing._looks_like_license_number("12345")  # too short
    assert not licensing._looks_like_license_number("123456789")  # too long
    assert not licensing._looks_like_license_number("Acme Construction")
    assert not licensing._looks_like_license_number("")


async def test_search_returns_empty_for_unknown_state(monkeypatch, caplog):
    page = _ca_search_page([])
    _stub_browser(monkeypatch, page)

    with caplog.at_level(logging.WARNING, logger=licensing.logger.name):
        results = await licensing.search("anything", state="ZZ")
    assert results == []
    assert any("no recipe" in rec.message for rec in caplog.records)


@pytest.mark.parametrize("state", ["TX", "FL", "NY"])
async def test_search_returns_empty_for_stub_states(monkeypatch, caplog, state):
    page = _ca_search_page([])
    _stub_browser(monkeypatch, page)

    with caplog.at_level(logging.WARNING, logger=licensing.logger.name):
        results = await licensing.search("anything", state=state)
    assert results == []
    assert any("stub" in rec.message.lower() for rec in caplog.records)


async def test_search_empty_query_returns_empty(monkeypatch):
    page = _ca_search_page([])
    _stub_browser(monkeypatch, page)
    assert await licensing.search("", state="CA") == []
    assert await licensing.search("   ", state="CA") == []


async def test_search_selector_miss_saves_diagnostic(monkeypatch, caplog, tmp_path):
    recipe = licensing._STATE_RECIPES["CA"]
    # "anything" is a non-numeric query, so the connector takes the name-tab path.
    page = _FakePage(
        {
            recipe["query_inputs_by_kind"]["name"]: _FakeLocator(),
            recipe["submit_buttons_by_kind"]["name"]: _FakeLocator(),
            recipe["row_selector"]: _FakeLocator(raise_on_all=True),
        }
    )
    _stub_browser(monkeypatch, page)
    monkeypatch.setattr(licensing, "_DIAGNOSTICS_DIR", tmp_path / "diagnostics")

    with caplog.at_level(logging.WARNING, logger=licensing.logger.name):
        results = await licensing.search("anything", state="CA")

    assert results == []
    assert any("selector miss" in rec.message for rec in caplog.records)
    assert page.screenshots, "expected a diagnostic screenshot path"
    assert page.screenshots[0].endswith(".png")


async def test_search_submit_failure_saves_diagnostic(monkeypatch, caplog, tmp_path):
    """If the submit button click raises, the connector bails with a screenshot."""
    recipe = licensing._STATE_RECIPES["CA"]
    page = _ca_search_page(
        [],
        submit_locator=_FakeLocator(raise_on_click=True),
    )
    _stub_browser(monkeypatch, page)
    monkeypatch.setattr(licensing, "_DIAGNOSTICS_DIR", tmp_path / "diagnostics")

    with caplog.at_level(logging.WARNING, logger=licensing.logger.name):
        results = await licensing.search("Acme Construction", state="CA")

    assert results == []
    assert any("submit failed" in rec.message for rec in caplog.records)
    assert page.screenshots, "expected a diagnostic screenshot path"
    # Sanity-check we exercised the recipe's row selector but never reached row parsing.
    assert recipe["row_selector"] not in page.locator_calls


async def test_search_respects_max_results(monkeypatch):
    rows = [
        _build_row(
            name=f"Company {i}",
            license_number=f"{i:07d}",
            status="Active",
            classification="B",
            expiration="2026-12-31",
        )
        for i in range(10)
    ]
    page = _ca_search_page(rows)
    _stub_browser(monkeypatch, page)

    results = await licensing.search("anything", state="CA", max_results=3)
    assert len(results) == 3


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def test_fetch_clicks_all_four_tabs_and_rolls_markdown(monkeypatch):
    page = _ca_profile_page()
    _stub_browser(monkeypatch, page)

    url = "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/LicenseDetail.aspx?LicNum=1234567"
    source = await licensing.fetch(url)

    assert source is not None
    assert source.source_kind == "licensing"
    assert source.title == "ACME CONSTRUCTION INC"
    assert source.url == url

    recipe = licensing._STATE_RECIPES["CA"]
    # Clicked all four tab buttons in the order Personnel → WC → Bonds → Disciplinary.
    assert recipe["personnel_tab_button"] in page.click_order
    assert recipe["workers_comp_tab_button"] in page.click_order
    assert recipe["bonds_tab_button"] in page.click_order
    assert recipe["disciplinary_tab_button"] in page.click_order
    tab_order = [
        s
        for s in page.click_order
        if s
        in {
            recipe["personnel_tab_button"],
            recipe["workers_comp_tab_button"],
            recipe["bonds_tab_button"],
            recipe["disciplinary_tab_button"],
        }
    ]
    assert tab_order == [
        recipe["personnel_tab_button"],
        recipe["workers_comp_tab_button"],
        recipe["bonds_tab_button"],
        recipe["disciplinary_tab_button"],
    ]
    # Each tab fake recorded exactly one click.
    assert page._selector_map[recipe["personnel_tab_button"]].click_calls == 1
    assert page._selector_map[recipe["workers_comp_tab_button"]].click_calls == 1
    assert page._selector_map[recipe["bonds_tab_button"]].click_calls == 1
    assert page._selector_map[recipe["disciplinary_tab_button"]].click_calls == 1

    body = source.cleaned_text
    assert "# ACME CONSTRUCTION INC" in body
    assert "1234567" in body
    assert "Active" in body
    assert "## Personnel" in body
    assert "John Smith" in body
    assert "## Workers' Compensation" in body
    assert "State Fund" in body
    assert "## Bonds" in body
    assert "Contractor's Bond" in body
    assert "## Disciplinary History" in body
    assert "No disciplinary actions" in body

    md = source.metadata
    assert md["license_number"] == "1234567"
    assert md["status"] == "Active"
    assert md["classification"] == "B - General Building"
    assert md["expiration"] == "2026-12-31"
    assert "John Smith" in md["personnel"]
    assert "State Fund" in md["workers_comp"]
    assert "Contractor's Bond" in md["bonds"]
    assert "No disciplinary actions" in md["disciplinary_history"]


async def test_fetch_rejects_unknown_host(monkeypatch):
    """Look-alike hosts must be rejected without opening a browser."""

    @asynccontextmanager
    async def _no_session(headful=None, block_media=True):
        raise AssertionError("should not open browser")
        yield  # pragma: no cover

    monkeypatch.setattr(licensing.browser, "browser_session", _no_session)

    spoof = "https://www.cslb.ca.gov.attacker.example/license/1"
    assert await licensing.fetch(spoof) is None
    assert await licensing.fetch("https://example.com/license/1") is None


async def test_fetch_rejects_stub_state_hosts(monkeypatch):
    """TX / FL / NY stubs must not open the browser even on a host match."""

    @asynccontextmanager
    async def _no_session(headful=None, block_media=True):
        raise AssertionError("should not open browser")
        yield  # pragma: no cover

    monkeypatch.setattr(licensing.browser, "browser_session", _no_session)

    assert await licensing.fetch("https://www.tdlr.texas.gov/LicenseSearch/") is None
    assert await licensing.fetch("https://www.myfloridalicense.com/wl11.asp") is None
    assert await licensing.fetch("https://www.dos.ny.gov/licensing/search.html") is None


async def test_fetch_returns_none_for_empty_url():
    assert await licensing.fetch("") is None


async def test_fetch_tolerates_missing_tabs_and_selector_misses(monkeypatch):
    """A profile where one tab fails to click and another section fails to read
    should still produce a Source — partial coverage, not a raise."""
    page = _ca_profile_page(
        raise_on_personnel_click=True,
        raise_on_disciplinary_inner_text=True,
    )
    _stub_browser(monkeypatch, page)

    url = "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/LicenseDetail.aspx?LicNum=1234567"
    source = await licensing.fetch(url)

    assert source is not None
    body = source.cleaned_text
    # Section headings still present even where data was missing.
    assert "## Personnel" in body
    assert "## Workers' Compensation" in body
    assert "## Bonds" in body
    assert "## Disciplinary History" in body
    # Sections that failed to read fall back to "(not available)".
    assert "(not available)" in body
    # Sections that succeeded still carry their data.
    assert "State Fund" in body
    assert "Contractor's Bond" in body


async def test_fetch_handles_minimal_profile(monkeypatch):
    """A profile with only a title still rounds-trips through the markdown builder."""
    recipe = licensing._STATE_RECIPES["CA"]
    page = _FakePage(
        {
            "h1": _FakeLocator(text="ACME LLC"),
            recipe["profile_license_number_selector"]: _FakeLocator(),
            recipe["profile_status_selector"]: _FakeLocator(),
            recipe["profile_classification_selector"]: _FakeLocator(),
            recipe["profile_expiration_selector"]: _FakeLocator(),
            recipe["personnel_tab_button"]: _FakeLocator(),
            recipe["personnel_section"]: _FakeLocator(),
            recipe["workers_comp_tab_button"]: _FakeLocator(),
            recipe["workers_comp_section"]: _FakeLocator(),
            recipe["bonds_tab_button"]: _FakeLocator(),
            recipe["bonds_section"]: _FakeLocator(),
            recipe["disciplinary_tab_button"]: _FakeLocator(),
            recipe["disciplinary_section"]: _FakeLocator(),
        }
    )
    _stub_browser(monkeypatch, page)

    url = "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/LicenseDetail.aspx?LicNum=000"
    source = await licensing.fetch(url)
    assert source is not None
    assert source.title == "ACME LLC"
    assert "# ACME LLC" in source.cleaned_text


# ---------------------------------------------------------------------------
# Source kind literal & smoke registration
# ---------------------------------------------------------------------------


def test_source_kind_literal():
    from research_agent.tools.models import SearchResult

    result = SearchResult(
        url="https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/CheckLicense.aspx",
        title="t",
        snippet="s",
        source_kind="licensing",
    )
    assert result.source_kind == "licensing"


def test_smoke_registry_includes_licensing():
    from research_agent.tools import TOOL_REGISTRY

    assert "licensing" in TOOL_REGISTRY
