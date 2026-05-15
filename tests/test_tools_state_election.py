"""Tests for official state-election candidate roster connector."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from research_agent.tools import state_election


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, text_by_url: dict[str, str]) -> None:
    class _Response:
        status_code = 200

        def __init__(self, text: str) -> None:
            self.text = text

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, url: str, **_kwargs: Any) -> _Response:
                return _Response(text_by_url[url])

        yield _Client()

    monkeypatch.setattr(state_election.httpx, "AsyncClient", _client_factory)


def _set_recipe(monkeypatch: pytest.MonkeyPatch, state: str, recipe: dict[str, Any]) -> None:
    recipes = dict(state_election._RECIPES)
    recipes[state] = recipe
    monkeypatch.setattr(state_election, "_RECIPES", recipes)


@pytest.mark.asyncio
async def test_static_csv_parses_co_candidate_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://example.test/co.csv"
    csv_text = (
        "Candidate Name,Office,Party,Status,District,Source URL\n"
        "Jane Example,State House District 12,DEM,Active,12,https://co.example/jane\n"
    )
    _set_recipe(
        monkeypatch,
        "CO",
        {
            "source_url": url,
            "source_type": "csv",
            "retrieval_method": "static_fetch",
            "cycle_coverage": [2026],
        },
    )
    _patch_httpx(monkeypatch, {url: csv_text})

    results = await state_election.search("Jane", state="CO", cycle=2026)

    assert len(results) == 1
    row = results[0]
    assert row.source_kind == "state_election"
    assert row.extras["state"] == "CO"
    assert row.extras["candidate_name"] == "Jane Example"
    assert row.extras["party"] == "DEM"
    assert row.extras["chamber"] == "House"
    assert row.extras["district_or_seat"] == "12"
    assert row.extras["status"] == "Active"
    assert row.extras["source_url"] == "https://co.example/jane"
    assert row.extras["confidence"] > 0


@pytest.mark.asyncio
async def test_static_csv_parses_md_candidate_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://example.test/md.csv"
    csv_text = (
        "Candidate,Contest Name,Political Party,Filing Status\n"
        "Robert Example,U.S. Senate,Republican,Filed\n"
    )
    _set_recipe(
        monkeypatch,
        "MD",
        {
            "source_url": url,
            "source_type": "csv",
            "retrieval_method": "static_fetch",
            "cycle_coverage": [2026],
        },
    )
    _patch_httpx(monkeypatch, {url: csv_text})

    results = await state_election.search("Robert", state="MD", cycle=2026)

    assert len(results) == 1
    row = results[0].extras
    assert row["state"] == "MD"
    assert row["candidate_name"] == "Robert Example"
    assert row["party"] == "Republican"
    assert row["chamber"] == "Senate"
    assert row["status"] == "Filed"


@pytest.mark.asyncio
async def test_static_html_parses_nc_candidate_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://example.test/nc.html"
    html = """
    <table>
      <thead><tr><th>Candidate Name</th><th>Office</th><th>Party</th><th>Status</th></tr></thead>
      <tbody>
        <tr>
          <td>Ana Candidate</td><td>US House District 4</td>
          <td>DEM</td><td>Qualified</td>
        </tr>
      </tbody>
    </table>
    """
    _set_recipe(
        monkeypatch,
        "NC",
        {"source_url": url, "source_type": "html", "retrieval_method": "static_fetch"},
    )
    _patch_httpx(monkeypatch, {url: html})

    results = await state_election.search("Ana", state="NC", office="House")

    assert len(results) == 1
    assert results[0].extras["candidate_name"] == "Ana Candidate"
    assert results[0].extras["district_or_seat"] == "4"
    assert results[0].extras["status"] == "Qualified"


@pytest.mark.asyncio
async def test_static_html_parses_ok_candidate_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://example.test/ok.html"
    html = """
    <table>
      <tr><th>Name</th><th>Office Sought</th><th>Party</th><th>Candidate Status</th></tr>
      <tr><td>Chris Filing</td><td>State Senate District 8</td><td>LIB</td><td>Filed</td></tr>
    </table>
    """
    _set_recipe(
        monkeypatch,
        "OK",
        {"source_url": url, "source_type": "html", "retrieval_method": "static_fetch"},
    )
    _patch_httpx(monkeypatch, {url: html})

    results = await state_election.search("Chris", state="OK")

    assert len(results) == 1
    assert results[0].extras["candidate_name"] == "Chris Filing"
    assert results[0].extras["chamber"] == "Senate"
    assert results[0].extras["district_or_seat"] == "8"


class _FakeLocator:
    def __init__(
        self,
        *,
        text: str = "",
        items: list[_FakeLocator] | None = None,
        children: dict[str, _FakeLocator] | None = None,
    ) -> None:
        self._text = text
        self._items = items or []
        self._children = children or {}
        self.filled: list[str] = []
        self.clicks = 0

    @property
    def first(self) -> _FakeLocator:
        return self

    async def wait_for(self, *, timeout: int = 0) -> None:  # noqa: ARG002
        return None

    async def all(self) -> list[_FakeLocator]:
        return self._items

    async def inner_text(self) -> str:
        return self._text

    async def fill(self, value: str) -> None:
        self.filled.append(value)

    async def click(self) -> None:
        self.clicks += 1

    def locator(self, selector: str) -> _FakeLocator:
        return self._children.get(selector, _FakeLocator())


class _FakePage:
    def __init__(self, selector_map: dict[str, _FakeLocator]) -> None:
        self.selector_map = selector_map
        self.closed = False
        self.screenshots: list[str] = []

    def locator(self, selector: str) -> _FakeLocator:
        return self.selector_map.get(selector, _FakeLocator())

    async def screenshot(self, *, path: str) -> None:
        self.screenshots.append(path)

    async def content(self) -> str:
        return "<html><table></table></html>"

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.page = page

    async def new_page(self) -> _FakePage:
        return self.page


@pytest.mark.asyncio
async def test_playwright_portal_fixture_parses_candidate_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selectors = {
        "query_input": "#candidate",
        "submit_button": "#submit",
        "row_selector": "table tbody tr",
        "name_selector": ".name",
        "party_selector": ".party",
        "office_selector": ".office",
        "status_selector": ".status",
    }
    row = _FakeLocator(
        children={
            ".name": _FakeLocator(text="Portal Candidate"),
            ".party": _FakeLocator(text="DEM"),
            ".office": _FakeLocator(text="US House District 2"),
            ".status": _FakeLocator(text="Qualified"),
        }
    )
    page = _FakePage(
        {
            "#candidate": _FakeLocator(),
            "#submit": _FakeLocator(),
            "table tbody tr": _FakeLocator(items=[row]),
        }
    )
    captured: dict[str, str] = {}

    @asynccontextmanager
    async def _session(*args, **kwargs):
        yield _FakeContext(page)

    async def _navigate(_page: _FakePage, url: str, **kwargs: Any) -> None:
        captured["url"] = url

    monkeypatch.setattr(state_election.browser, "browser_session", _session)
    monkeypatch.setattr(state_election.browser, "navigate", _navigate)
    _set_recipe(
        monkeypatch,
        "SC",
        {
            "source_url": "https://example.test/portal",
            "source_type": "search_portal",
            "retrieval_method": "playwright_form",
            "selectors": selectors,
        },
    )

    results = await state_election.search("Portal", state="SC")

    assert captured["url"] == "https://example.test/portal"
    assert page.closed is True
    assert len(results) == 1
    assert results[0].extras["candidate_name"] == "Portal Candidate"
    assert results[0].extras["state"] == "SC"
    assert results[0].extras["district_or_seat"] == "2"
