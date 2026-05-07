"""Tests for `research_agent.tools.licensing` (issues #91 and #155)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from research_agent.tools import licensing

# ---------------------------------------------------------------------------
# Fakes — Playwright surface area sufficient to exercise licensing.py.
#
# search() and fetch() now both call BS4 parsers against page.content(), so
# the fake page only needs to: (a) record click/fill/screenshot calls so we
# can assert orchestration, and (b) hand back a fixed HTML string.
# ---------------------------------------------------------------------------


class _FakeLocator:
    def __init__(
        self,
        page: _FakePage | None = None,
        selector: str = "",
        *,
        on_click: Any = None,
        on_fill: Any = None,
        raise_on_click: bool = False,
        raise_on_fill: bool = False,
    ) -> None:
        self._page = page
        self._selector = selector
        self._on_click = on_click
        self._on_fill = on_fill
        self._raise_on_click = raise_on_click
        self._raise_on_fill = raise_on_fill
        self.click_calls: int = 0
        self.fill_calls: list[str] = []

    @property
    def first(self) -> _FakeLocator:
        return self

    async def click(self) -> None:
        self.click_calls += 1
        if self._page is not None:
            self._page.click_order.append(self._selector)
        if self._raise_on_click:
            raise RuntimeError("click failed")
        if self._on_click is not None:
            self._on_click()

    async def fill(self, value: str) -> None:
        self.fill_calls.append(value)
        if self._page is not None:
            self._page.fill_calls.append((self._selector, value))
        if self._raise_on_fill:
            raise RuntimeError("fill failed")
        if self._on_fill is not None:
            self._on_fill(value)


class _FakePage:
    """Minimal stand-in for ``playwright.async_api.Page``.

    ``html`` is what ``page.content()`` returns. Tests preload either the
    real CSLB results-page HTML (for parser end-to-end coverage) or a
    minimal hand-rolled snippet (for sentinel-status coverage like
    page-error / no-hits).
    """

    def __init__(
        self,
        html: str = "",
        *,
        locator_overrides: dict[str, _FakeLocator] | None = None,
        raise_on_content: bool = False,
    ) -> None:
        self._html = html
        self._locator_overrides = locator_overrides or {}
        self._raise_on_content = raise_on_content
        self.screenshots: list[str] = []
        self.fill_calls: list[tuple[str, str]] = []
        self.click_order: list[str] = []
        self.wait_for_load_state_calls: list[tuple[str, int | None]] = []
        self.extra_http_headers: list[dict[str, str]] = []
        self.content_calls: int = 0
        self.closed: bool = False

    async def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        self.extra_http_headers.append(dict(headers))

    def locator(self, selector: str) -> _FakeLocator:
        loc = self._locator_overrides.get(selector)
        if loc is None:
            loc = _FakeLocator(self, selector)
            # Cache so the same selector returns the same locator —
            # useful when a test wants to assert click/fill counts on a
            # selector it didn't pre-register.
            self._locator_overrides[selector] = loc
        else:
            # Bind the selector + page on registered locators so they
            # show up in ``click_order`` etc.
            loc._page = self
            loc._selector = selector
        return loc

    async def content(self) -> str:
        self.content_calls += 1
        if self._raise_on_content:
            raise RuntimeError("content failed")
        return self._html

    async def wait_for_load_state(
        self, state: str, *, timeout: int | None = None
    ) -> None:
        self.wait_for_load_state_calls.append((state, timeout))

    async def screenshot(self, *, path: str) -> None:
        self.screenshots.append(path)

    async def close(self) -> None:
        self.closed = True


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


# ---------------------------------------------------------------------------
# Fixture HTML helpers — small inline builders for synthetic CSLB pages.
# ---------------------------------------------------------------------------


def _build_results_html(
    rows: list[dict[str, str]],
    *,
    table_id: str = "MainContent_dlMain",
    omit_table: bool = False,
) -> str:
    """Build CSLB-shaped results HTML.

    Each row: ``{name, license_number, status, city, name_type, href}``.
    Mirrors the live ``MainContent_dlMain_<field>_<index>`` ID convention.
    """
    if omit_table:
        return "<html><body><h1>Search</h1></body></html>"
    body_rows: list[str] = []
    for idx, row in enumerate(rows):
        href = row.get("href") or ""
        body_rows.append(
            f"""
            <tr><td><table><tbody>
              <tr>
                <td>Contractor Name</td>
                <td><span id="{table_id}_lblName_{idx}">{row.get('name', '')}</span></td>
              </tr>
              <tr>
                <td>Name Type</td>
                <td><span id="{table_id}_lblType_{idx}">{row.get('name_type', '')}</span></td>
              </tr>
              <tr>
                <td>License</td>
                <td><a id="{table_id}_hlLicense_{idx}" href="{href}">
                    {row.get('license_number', '')}</a></td>
              </tr>
              <tr>
                <td>City</td>
                <td><span id="{table_id}_lblCity_{idx}">{row.get('city', '')}</span></td>
              </tr>
              <tr>
                <td>Status</td>
                <td><span id="{table_id}_lblLicenseStatus_{idx}">{row.get('status', '')}</span></td>
              </tr>
            </tbody></table></td></tr>
            """
        )
    return f"""<html><body><h1>Contractor Name Search Results</h1>
        <table id="{table_id}"><tbody>{''.join(body_rows)}</tbody></table>
        </body></html>"""


def _build_profile_html(
    *,
    title: str = "Contractor's License Detail for License # 1234567",
    license_number: str = "1234567",
    status: str = "This license is current and active.",
    classifications: str = "B - GENERAL BUILDING",
    expiration: str = "06/30/2027",
    business_info: str = "ACME CONSTRUCTION INC<br>123 MAIN ST",
    bonding: str = "Contractor's Bond — $25,000",
    workers_comp: str = "Carrier: STATE FUND",
    other: str = "",
    include_disclosure_link: bool = False,
) -> str:
    # Mirror the live CSLB structure: there's ALWAYS a disclaimer "here"
    # link to the public-complaint definition page; the *real* disclosure
    # link only renders when the contractor has actionable items, and it
    # lives outside the disclaimer ul.
    disclaimer = (
        '<ul id="disclaimer">'
        '<li>Click <a href="PublicComplaintDisclosure.aspx">here</a> for'
        ' a definition.</li></ul>'
    )
    real_disclosure = (
        '<div class="disclosure-section">'
        '<a href="PublicComplaintDisclosure.aspx?LicNum=999">'
        'Public complaint disclosure on file</a></div>'
        if include_disclosure_link
        else ""
    )
    return f"""<html><body>
        {disclaimer}
        {real_disclosure}
        <h1>{title}</h1>
        <span id="MainContent_Header2Detail">{license_number}</span>
        <table>
        <tr><td id="MainContent_BusInfo">{business_info}</td></tr>
        <tr><td id="MainContent_ExpDt">{expiration}</td></tr>
        <tr><td id="MainContent_Status">{status}</td></tr>
        <tr><td id="MainContent_ClassCellTable">{classifications}</td></tr>
        <tr><td id="MainContent_BondingCellTable">{bonding}</td></tr>
        <tr><td id="MainContent_WCStatus">{workers_comp}</td></tr>
        <tr><td id="MainContent_MultiLicDisplay">{other}</td></tr>
        </table>
        </body></html>"""


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
# _parse_search_results — pure parser (BS4)
# ---------------------------------------------------------------------------


def test_parse_search_results_extracts_rows():
    html = _build_results_html(
        [
            {
                "name": "ACME CONSTRUCTION INC",
                "license_number": "1234567",
                "status": "Active",
                "city": "SAN JOSE",
                "name_type": "DBA",
                "href": "/OnlineServices/CheckLicenseII/LicenseDetail.aspx?LicNum=1234567",
            },
            {
                "name": "ACME ROOFING LLC",
                "license_number": "9876543",
                "status": "Expired",
                "city": "OAKLAND",
                "name_type": "Previous",
                "href": "/OnlineServices/CheckLicenseII/LicenseDetail.aspx?LicNum=9876543",
            },
        ]
    )
    recipe = licensing._STATE_RECIPES["CA"]
    results, status = licensing._parse_search_results(
        html,
        recipe=recipe,
        search_url=recipe["search_url"],
        state="CA",
    )
    assert status == "ok"
    assert len(results) == 2
    top = results[0]
    assert top.title == "ACME CONSTRUCTION INC"
    assert top.url == (
        "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/"
        "LicenseDetail.aspx?LicNum=1234567"
    )
    assert top.extras["license_number"] == "1234567"
    assert top.extras["status"] == "Active"
    assert top.extras["city"] == "SAN JOSE"
    assert top.extras["name_type"] == "DBA"
    assert top.extras["state"] == "CA"
    # Search-results page has no classification/expiration; fetch() fills them.
    assert top.extras["classification"] == ""
    assert top.extras["expiration"] == ""


def test_parse_search_results_returns_no_hits_when_table_empty():
    html = """<html><body><table id="MainContent_dlMain"><tbody></tbody></table></body></html>"""
    recipe = licensing._STATE_RECIPES["CA"]
    results, status = licensing._parse_search_results(
        html, recipe=recipe, search_url=recipe["search_url"], state="CA"
    )
    assert results == []
    assert status == "no-hits"


def test_parse_search_results_returns_page_error_when_table_missing():
    """If the results table itself is absent (e.g. user bounced back to the
    search form), distinguish that from a clean 0-hits page."""
    html = "<html><body><h1>Check a License</h1><form>...</form></body></html>"
    recipe = licensing._STATE_RECIPES["CA"]
    results, status = licensing._parse_search_results(
        html, recipe=recipe, search_url=recipe["search_url"], state="CA"
    )
    assert results == []
    assert status == "page-error"


def test_parse_search_results_respects_max_results():
    rows = [
        {
            "name": f"COMPANY {i}",
            "license_number": f"{i:07d}",
            "status": "Active",
            "city": "PALO ALTO",
            "name_type": "DBA",
            "href": f"/OnlineServices/CheckLicenseII/LicenseDetail.aspx?LicNum={i:07d}",
        }
        for i in range(10)
    ]
    html = _build_results_html(rows)
    recipe = licensing._STATE_RECIPES["CA"]
    results, status = licensing._parse_search_results(
        html, recipe=recipe, search_url=recipe["search_url"], state="CA", max_results=3
    )
    assert status == "ok"
    assert len(results) == 3


def test_parse_search_results_skips_rows_with_blank_names():
    """Defensive guard: malformed CSLB rows shouldn't surface as titles."""
    html = _build_results_html(
        [
            {"name": "", "license_number": "111", "status": "Active"},
            {"name": "VALID INC", "license_number": "222", "status": "Active"},
        ]
    )
    recipe = licensing._STATE_RECIPES["CA"]
    results, status = licensing._parse_search_results(
        html, recipe=recipe, search_url=recipe["search_url"], state="CA"
    )
    # Skipping the blank name leaves one usable row → still "ok".
    assert status == "ok"
    assert len(results) == 1
    assert results[0].title == "VALID INC"


# ---------------------------------------------------------------------------
# Real-fixture regression test — load the captured CSLB SBI Builders page.
#
# This is the regression bar for issue #155. If CSLB drifts again, this
# test will fail (and the operator updates the fixture + recipe).
# ---------------------------------------------------------------------------


def test_parse_search_results_handles_real_cslb_fixture():
    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "cslb"
        / "sbi_builders_results.html"
    )
    html = fixture.read_text(encoding="utf-8")
    recipe = licensing._STATE_RECIPES["CA"]
    results, status = licensing._parse_search_results(
        html,
        recipe=recipe,
        search_url=recipe["search_url"],
        state="CA",
        max_results=50,
    )
    assert status == "ok"
    assert len(results) >= 1
    # The first SBI Builders entry on the live fixture: license 860997, Active.
    sbi = next(
        (r for r in results if "SBI BUILDERS" in r.title), None
    )
    assert sbi is not None, "expected at least one SBI BUILDERS row"
    assert sbi.extras["license_number"] == "860997"
    assert sbi.extras["status"] == "Active"
    assert sbi.url.endswith("LicNum=860997")


def test_parse_profile_handles_real_cslb_fixture():
    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "cslb"
        / "license_detail_860997.html"
    )
    html = fixture.read_text(encoding="utf-8")
    recipe = licensing._STATE_RECIPES["CA"]
    url = (
        "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/"
        "LicenseDetail.aspx?LicNum=860997"
    )
    source = licensing._parse_profile(html, url, recipe=recipe)
    assert source is not None
    assert source.metadata["license_number"] == "860997"
    assert "active" in source.metadata["status"].lower()
    assert source.metadata["expiration"] == "06/30/2027"
    assert "B - GENERAL BUILDING" in source.metadata["classification"]
    assert "MERCHANTS BONDING" in source.metadata["bonding"].upper()
    assert "## Bonding Information" in source.cleaned_text
    assert "## Disciplinary History" in source.cleaned_text


# ---------------------------------------------------------------------------
# search() — integration through the fake page
# ---------------------------------------------------------------------------


async def test_search_returns_results_for_name_query(monkeypatch):
    html = _build_results_html(
        [
            {
                "name": "ACME CONSTRUCTION INC",
                "license_number": "1234567",
                "status": "Active",
                "city": "SAN JOSE",
                "name_type": "DBA",
                "href": "/OnlineServices/CheckLicenseII/LicenseDetail.aspx?LicNum=1234567",
            }
        ]
    )
    page = _FakePage(html)
    captured = _stub_browser(monkeypatch, page)

    results = await licensing.search("Acme Construction", state="CA", max_results=5)

    assert captured["navigations"] == [licensing._STATE_RECIPES["CA"]["search_url"]]
    assert len(results) == 1
    top = results[0]
    assert top.title == "ACME CONSTRUCTION INC"
    assert top.extras["license_number"] == "1234567"
    assert top.extras["status"] == "Active"
    # search() called the browser-side wait helper before reading content.
    assert page.wait_for_load_state_calls
    assert page.content_calls == 1


async def test_search_returns_diagnostic_status_when_requested(monkeypatch):
    html = _build_results_html(
        [
            {
                "name": "ACME",
                "license_number": "1234567",
                "status": "Active",
                "name_type": "DBA",
                "city": "SAN JOSE",
                "href": "/foo",
            }
        ]
    )
    page = _FakePage(html)
    _stub_browser(monkeypatch, page)

    results, status = await licensing.search(
        "Acme", state="CA", return_diagnostic=True
    )
    assert status == "ok"
    assert len(results) == 1


async def test_search_toggles_query_kind_for_license_number(monkeypatch):
    """Numeric query → click license-number tab, fill license-number input."""
    recipe = licensing._STATE_RECIPES["CA"]
    page = _FakePage(_build_results_html([]))
    _stub_browser(monkeypatch, page)

    await licensing.search("1234567", state="CA")

    assert recipe["tab_buttons_by_kind"]["number"] in page.click_order
    assert recipe["tab_buttons_by_kind"]["name"] not in page.click_order
    filled_selectors = [sel for sel, _ in page.fill_calls]
    assert recipe["query_inputs_by_kind"]["number"] in filled_selectors
    assert recipe["query_inputs_by_kind"]["name"] not in filled_selectors


async def test_search_toggles_query_kind_for_business_name(monkeypatch):
    recipe = licensing._STATE_RECIPES["CA"]
    page = _FakePage(_build_results_html([]))
    _stub_browser(monkeypatch, page)

    await licensing.search("Acme Construction", state="CA")

    assert recipe["tab_buttons_by_kind"]["name"] in page.click_order
    assert recipe["tab_buttons_by_kind"]["number"] not in page.click_order
    filled_selectors = [sel for sel, _ in page.fill_calls]
    assert recipe["query_inputs_by_kind"]["name"] in filled_selectors


def test_license_number_regex_recognises_cslb_format():
    assert licensing._looks_like_license_number("1234567")
    assert licensing._looks_like_license_number("123456")
    assert licensing._looks_like_license_number("12345678")
    assert not licensing._looks_like_license_number("12345")
    assert not licensing._looks_like_license_number("123456789")
    assert not licensing._looks_like_license_number("Acme Construction")
    assert not licensing._looks_like_license_number("")


async def test_search_returns_empty_for_unknown_state(monkeypatch, caplog):
    page = _FakePage(_build_results_html([]))
    _stub_browser(monkeypatch, page)

    with caplog.at_level(logging.WARNING, logger=licensing.logger.name):
        results = await licensing.search("anything", state="ZZ")
    assert results == []
    assert any("no recipe" in rec.message for rec in caplog.records)


@pytest.mark.parametrize("state", ["TX", "FL", "NY"])
async def test_search_returns_empty_for_stub_states(monkeypatch, caplog, state):
    page = _FakePage(_build_results_html([]))
    _stub_browser(monkeypatch, page)

    with caplog.at_level(logging.WARNING, logger=licensing.logger.name):
        results = await licensing.search("anything", state=state)
    assert results == []
    assert any("stub" in rec.message.lower() for rec in caplog.records)


async def test_search_empty_query_returns_empty(monkeypatch):
    page = _FakePage(_build_results_html([]))
    _stub_browser(monkeypatch, page)
    assert await licensing.search("", state="CA") == []
    assert await licensing.search("   ", state="CA") == []


async def test_search_parser_miss_saves_diagnostic(monkeypatch, caplog, tmp_path):
    """If the table is present but rows don't extract, dump diagnostics."""
    # Table exists but no lblName_<N> spans → parser-miss.
    html = """<html><body>
        <table id="MainContent_dlMain"><tbody>
        <tr><td><table><tbody>
            <tr><td>Contractor</td><td><span id="weird_id_0">Bad</span></td></tr>
        </tbody></table></td></tr>
        </tbody></table>
        </body></html>"""
    page = _FakePage(html)
    _stub_browser(monkeypatch, page)
    monkeypatch.setattr(licensing, "_DIAGNOSTICS_DIR", tmp_path / "diagnostics")

    with caplog.at_level(logging.WARNING, logger=licensing.logger.name):
        results, status = await licensing.search(
            "anything", state="CA", return_diagnostic=True
        )

    assert results == []
    assert status == "no-hits"  # No name_spans → no-hits, not parser-miss.
    assert page.content_calls >= 1


async def test_search_page_error_dumps_html_and_screenshot(
    monkeypatch, caplog, tmp_path
):
    """If the results table itself is missing, status='page-error' and we
    persist BOTH a screenshot and the HTML dump under data/diagnostics/cslb/."""
    page = _FakePage("<html><body><h1>Error</h1></body></html>")
    _stub_browser(monkeypatch, page)
    diag_dir = tmp_path / "diagnostics"
    monkeypatch.setattr(licensing, "_DIAGNOSTICS_DIR", diag_dir)

    with caplog.at_level(logging.WARNING, logger=licensing.logger.name):
        results, status = await licensing.search(
            "anything", state="CA", return_diagnostic=True
        )

    assert results == []
    assert status == "page-error"
    assert any("page-error" in rec.message for rec in caplog.records)
    # Screenshot was captured and HTML dump written.
    assert page.screenshots, "expected a diagnostic screenshot"
    assert page.screenshots[0].endswith(".png")
    html_dumps = list(diag_dir.glob("*.html"))
    assert html_dumps, "expected an HTML diagnostic dump"


async def test_search_submit_failure_saves_diagnostic(monkeypatch, caplog, tmp_path):
    """If the submit button click raises, the connector bails with a screenshot+html dump."""
    recipe = licensing._STATE_RECIPES["CA"]
    submit_selector = recipe["submit_buttons_by_kind"]["name"]
    page = _FakePage(
        _build_results_html([]),
        locator_overrides={
            submit_selector: _FakeLocator(raise_on_click=True),
        },
    )
    _stub_browser(monkeypatch, page)
    diag_dir = tmp_path / "diagnostics"
    monkeypatch.setattr(licensing, "_DIAGNOSTICS_DIR", diag_dir)

    with caplog.at_level(logging.WARNING, logger=licensing.logger.name):
        results, status = await licensing.search(
            "Acme Construction", state="CA", return_diagnostic=True
        )

    assert results == []
    assert status == "submit-failed"
    assert any("submit failed" in rec.message for rec in caplog.records)
    assert page.screenshots
    html_dumps = list(diag_dir.glob("*.html"))
    assert html_dumps


async def test_search_respects_max_results(monkeypatch):
    rows = [
        {
            "name": f"Company {i}",
            "license_number": f"{i:07d}",
            "status": "Active",
            "name_type": "DBA",
            "city": "PALO ALTO",
            "href": f"/x{i}",
        }
        for i in range(10)
    ]
    page = _FakePage(_build_results_html(rows))
    _stub_browser(monkeypatch, page)

    results = await licensing.search("anything", state="CA", max_results=3)
    assert len(results) == 3


# ---------------------------------------------------------------------------
# fetch() — integration through the fake page
# ---------------------------------------------------------------------------


async def test_fetch_returns_source_with_metadata(monkeypatch):
    page = _FakePage(
        _build_profile_html(
            license_number="1234567",
            status="This license is current and active.",
            classifications="B - GENERAL BUILDING",
            expiration="06/30/2027",
        )
    )
    _stub_browser(monkeypatch, page)

    url = (
        "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/"
        "LicenseDetail.aspx?LicNum=1234567"
    )
    source = await licensing.fetch(url)

    assert source is not None
    assert source.source_kind == "licensing"
    assert source.url == url
    assert source.metadata["license_number"] == "1234567"
    assert source.metadata["expiration"] == "06/30/2027"
    assert "B - GENERAL BUILDING" in source.metadata["classification"]
    assert "active" in source.metadata["status"].lower()
    body = source.cleaned_text
    assert "## Business Information" in body
    assert "## Bonding Information" in body
    assert "## Workers' Compensation" in body
    assert "## Disciplinary History" in body
    # Default disciplinary path: no disclosure link → reassuring note.
    assert "No disclosable actions" in source.metadata["disciplinary_history"]


async def test_fetch_sets_referer_header_to_search_url(monkeypatch):
    """CSLB's LicenseDetail.aspx redirects back to the search form when the
    request lacks a same-origin referer. fetch() must set one before
    navigating or it silently parses the search form into ``None``."""
    page = _FakePage(_build_profile_html())
    _stub_browser(monkeypatch, page)

    url = (
        "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/"
        "LicenseDetail.aspx?LicNum=1234567"
    )
    await licensing.fetch(url)
    assert page.extra_http_headers, "expected fetch() to set extra HTTP headers"
    assert (
        page.extra_http_headers[0].get("Referer")
        == licensing._STATE_RECIPES["CA"]["search_url"]
    )


async def test_fetch_surfaces_disciplinary_disclosure_link(monkeypatch):
    """When the contractor has a PublicComplaintDisclosure link, it shows up
    in the disciplinary_history metadata key (the smoke wrapper's primary
    due-diligence signal)."""
    page = _FakePage(_build_profile_html(include_disclosure_link=True))
    _stub_browser(monkeypatch, page)

    url = (
        "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/"
        "LicenseDetail.aspx?LicNum=1234567"
    )
    source = await licensing.fetch(url)
    assert source is not None
    assert "PublicComplaintDisclosure" in source.metadata["disciplinary_history"]


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


async def test_fetch_returns_none_when_page_lacks_signal(monkeypatch):
    """A page with no license fields and no disclosure link is not a profile."""
    page = _FakePage("<html><body><h1>Some other page</h1></body></html>")
    _stub_browser(monkeypatch, page)
    url = (
        "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/"
        "LicenseDetail.aspx?LicNum=000"
    )
    source = await licensing.fetch(url)
    assert source is None


async def test_fetch_handles_minimal_profile(monkeypatch):
    """A profile with only a title + license number still rounds-trips."""
    html = """<html><body>
        <h1>Contractor's License Detail for License # 99</h1>
        <span id="MainContent_Header2Detail">99</span>
        </body></html>"""
    page = _FakePage(html)
    _stub_browser(monkeypatch, page)

    url = (
        "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/"
        "LicenseDetail.aspx?LicNum=99"
    )
    source = await licensing.fetch(url)
    assert source is not None
    assert source.metadata["license_number"] == "99"
    assert "Contractor's License Detail" in source.cleaned_text


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
