"""Tests for `research_agent.tools.web_search` (issue #14)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from research_agent.tools import browser, web_search
from research_agent.tools.models import SearchResult

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixture loaders + parser-level tests (no Playwright)
# ---------------------------------------------------------------------------


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_ddg_extracts_three_results_and_unwraps_redirect():
    rows = web_search._parse_ddg(_load("web_search_ddg.html"))
    assert len(rows) == 3
    assert rows[0]["url"] == "https://openai.com/blog/example-one"
    assert rows[0]["title"] == "First Example Result"
    assert "First snippet" in rows[0]["snippet"]
    assert "OpenAI" in rows[0]["snippet"]
    # Plain hrefs (no /l/?uddg=) are passed through.
    assert rows[2]["url"] == "https://plain.example/three"


def test_parse_google_drops_ads_and_keeps_organic():
    rows = web_search._parse_google(_load("web_search_google.html"))
    assert [r["url"] for r in rows] == [
        "https://example.com/one",
        "https://anothersite.example/page-two",
    ]
    assert rows[0]["title"] == "Organic Result One"
    assert "organic result one" in rows[0]["snippet"].lower()


def test_parse_ddg_empty_html_returns_empty_list():
    assert web_search._parse_ddg("<html><body>nothing</body></html>") == []


def test_parse_google_empty_html_returns_empty_list():
    assert web_search._parse_google("<html><body>nothing</body></html>") == []


def test_unwrap_ddg_href_handles_plain_url():
    assert web_search._unwrap_ddg_href("https://plain.example/x") == "https://plain.example/x"


def test_unwrap_ddg_href_unwraps_redirect():
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Ftarget.example%2Fpath&rut=foo"
    assert web_search._unwrap_ddg_href(href) == "https://target.example/path"


# ---------------------------------------------------------------------------
# Full search() flow with a stub browser session
# ---------------------------------------------------------------------------


class FakePage:
    def __init__(self, html: str, *, screenshot_path_holder: list[Path] | None = None) -> None:
        self._html = html
        self.closed = False
        self.goto_calls: list[str] = []
        self.screenshot_calls: list[Path] = []
        self._sp_holder = screenshot_path_holder

    async def goto(self, url: str, *, wait_until: str = "load", timeout: int = 30000) -> None:
        self.goto_calls.append(url)

    async def content(self) -> str:
        return self._html

    async def screenshot(self, *, path: str) -> None:
        p = Path(path)
        self.screenshot_calls.append(p)
        # Touch the file so the diagnostics dir exists with the marker file.
        p.write_bytes(b"\x89PNG-fake")
        if self._sp_holder is not None:
            self._sp_holder.append(p)

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self._page = page
        self.pages_handed_out: list[FakePage] = []

    async def new_page(self):
        self.pages_handed_out.append(self._page)
        return self._page


def _patch_browser_session(monkeypatch, page: FakePage) -> FakeContext:
    fake_ctx = FakeContext(page)

    @asynccontextmanager
    async def _fake_session(headful=None, block_media=True):
        yield fake_ctx

    monkeypatch.setattr(browser, "browser_session", _fake_session)

    # Skip throttling — tests should not actually sleep.
    async def _no_throttle(url: str) -> None:
        return None

    async def _fake_navigate(p, url, *, timeout_ms=30000):
        await p.goto(url)

    monkeypatch.setattr(browser, "throttle", _no_throttle)
    monkeypatch.setattr(browser, "navigate", _fake_navigate)
    return fake_ctx


async def test_search_ddg_returns_search_results_with_engine_extras(monkeypatch):
    page = FakePage(_load("web_search_ddg.html"))
    _patch_browser_session(monkeypatch, page)

    results = await web_search.search("openai gpt-5", max_results=10, engine="ddg")
    assert len(results) == 3
    assert all(isinstance(r, SearchResult) for r in results)
    assert all(r.source_kind == "web" for r in results)
    assert all(r.extras.get("source_engine") == "ddg" for r in results)
    assert results[0].url == "https://openai.com/blog/example-one"
    assert results[0].title == "First Example Result"
    assert results[0].score is None
    assert results[0].published_at is None
    # Page was closed after use.
    assert page.closed


async def test_search_ddg_truncates_to_max_results(monkeypatch):
    page = FakePage(_load("web_search_ddg.html"))
    _patch_browser_session(monkeypatch, page)

    results = await web_search.search("foo", max_results=2, engine="ddg")
    assert len(results) == 2


async def test_search_google_drops_ads(monkeypatch):
    page = FakePage(_load("web_search_google.html"))
    _patch_browser_session(monkeypatch, page)

    results = await web_search.search("widgets", max_results=10, engine="google")
    urls = [r.url for r in results]
    assert urls == ["https://example.com/one", "https://anothersite.example/page-two"]
    assert all(r.extras["source_engine"] == "google" for r in results)


async def test_search_empty_query_returns_empty_without_navigation(monkeypatch):
    page = FakePage("(should not be read)")
    _patch_browser_session(monkeypatch, page)

    results = await web_search.search("   ", engine="ddg")
    assert results == []
    assert page.goto_calls == []


async def test_search_zero_results_writes_screenshot_and_warns(monkeypatch, tmp_path, caplog):
    """Selector drift fail-soft: 0 results for a non-empty query → screenshot + WARN."""
    monkeypatch.chdir(tmp_path)
    page = FakePage("<html><body>no matches here</body></html>")
    _patch_browser_session(monkeypatch, page)

    caplog.set_level(logging.WARNING, logger="research_agent.tools.web_search")
    results = await web_search.search("openai gpt-5", engine="ddg")
    assert results == []
    # Screenshot saved under data/diagnostics/web_search/.
    diag_dir = tmp_path / "data" / "diagnostics" / "web_search"
    saved = list(diag_dir.glob("*.png"))
    assert len(saved) == 1
    assert "ddg" in saved[0].name
    # WARN logged once with engine + path.
    matches = [r for r in caplog.records if "selector drift" in r.getMessage()]
    assert len(matches) == 1
    assert "ddg" in matches[0].getMessage()
    # Path appears in the WARN message (relative form, since DIAGNOSTICS_DIR
    # is a relative Path).
    assert saved[0].name in matches[0].getMessage()


async def test_search_navigation_url_includes_query(monkeypatch):
    page = FakePage(_load("web_search_ddg.html"))
    _patch_browser_session(monkeypatch, page)

    await web_search.search("hello world", engine="ddg")
    assert page.goto_calls
    assert "html.duckduckgo.com/html/?q=hello+world" in page.goto_calls[0]


async def test_search_google_navigation_url_includes_hl(monkeypatch):
    page = FakePage(_load("web_search_google.html"))
    _patch_browser_session(monkeypatch, page)

    await web_search.search("hello", engine="google")
    assert "google.com/search?q=hello&hl=en" in page.goto_calls[0]


# ---------------------------------------------------------------------------
# Module wiring
# ---------------------------------------------------------------------------


def test_tool_registry_has_web_search():
    from research_agent.tools import TOOL_REGISTRY

    assert "web_search" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["web_search"])


def test_smoke_web_search_invokes_auto_engine_only(monkeypatch):
    """Issue #192: smoke must mirror the orchestrator (engine='auto'), not
    hand-roll separate ddg/google scrapes."""
    from research_agent.tools import TOOL_REGISTRY

    calls: list[dict] = []

    async def _fake_search(query, max_results=10, engine="auto"):
        calls.append({"query": query, "max_results": max_results, "engine": engine})
        return []

    monkeypatch.setattr(web_search, "search", _fake_search)
    # No Brave key → label should say ddg-fallback when results are empty.
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)

    output = TOOL_REGISTRY["web_search"]("project 2025")

    assert len(calls) == 1, "smoke must call web_search.search exactly once"
    assert calls[0]["engine"] == "auto"
    assert calls[0]["query"] == "project 2025"
    # When auto resolves to ddg with zero hits, the header flags the fallback
    # and selector drift so operators can tell it apart from a Brave miss.
    assert "engine=ddg" in output
    assert "selector drift" in output


def test_smoke_web_search_brave_path_labels_engine(monkeypatch):
    """With BRAVE_SEARCH_API_KEY set, auto routes to Brave and the smoke
    output labels the path so operators can see which engine ran."""
    from research_agent.tools import TOOL_REGISTRY
    from research_agent.tools.models import SearchResult

    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")

    fake_hit = SearchResult(
        url="https://example.com/p2025",
        title="Project 2025 implementation",
        snippet="Heritage Foundation policy blueprint",
        source_kind="web",
        extras={"source_engine": "brave"},
    )

    async def _fake_brave(query, max_results):
        return [fake_hit]

    monkeypatch.setattr(web_search, "_search_brave", _fake_brave)

    output = TOOL_REGISTRY["web_search"]("project 2025")

    assert "engine=brave" in output
    assert "returned 1 hits" in output
    assert "https://example.com/p2025" in output
    assert "Project 2025 implementation" in output


def test_serp_rate_buckets_set_to_one_per_two_seconds():
    """Every ``search()`` call applies the 0.5 rps SERP rate (1 query/2s)."""
    browser.reset_for_tests()
    web_search._ensure_serp_rates()
    ddg_bucket = browser._host_buckets.get(web_search.DDG_HOST)
    google_bucket = browser._host_buckets.get(web_search.GOOGLE_HOST)
    assert ddg_bucket is not None
    assert google_bucket is not None
    assert ddg_bucket.rps == pytest.approx(0.5)
    assert google_bucket.rps == pytest.approx(0.5)
