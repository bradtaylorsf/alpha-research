"""Tests for `research_agent.tools.web_search` (issue #14)."""

from __future__ import annotations

import json
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


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _FakeResp:
    def __init__(self, status_code: int, payload: dict, *, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload)

    def json(self) -> dict:
        return self._payload


def _patch_brave_httpx(monkeypatch: pytest.MonkeyPatch, *, payload: dict):
    captured: dict[str, list] = {
        "urls": [],
        "headers": [],
        "params": [],
    }

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, url, *, headers=None, params=None):
                captured["urls"].append(url)
                captured["headers"].append(headers or {})
                captured["params"].append(params or {})
                return _FakeResp(200, payload)

        yield _Client()

    monkeypatch.setattr(web_search.httpx, "AsyncClient", _client_factory)
    return captured


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


async def test_search_browser_session_failure_returns_empty(monkeypatch, caplog):
    @asynccontextmanager
    async def _broken_session(headful=None, block_media=True):
        raise web_search.PlaywrightError("launch denied")
        yield

    monkeypatch.setattr(browser, "browser_session", _broken_session)

    caplog.set_level(logging.WARNING, logger="research_agent.tools.web_search")
    results = await web_search.search("openai gpt-5", engine="ddg")

    assert results == []
    assert any(
        "browser session failed" in record.getMessage()
        for record in caplog.records
    )


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
# Brave language targeting
# ---------------------------------------------------------------------------


async def test_search_brave_includes_search_lang_when_set(monkeypatch):
    payload = _load_json("web_search/lang-fr.json")
    captured = _patch_brave_httpx(monkeypatch, payload=payload)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")

    results = await web_search.search(
        "guerre d'Algerie",
        max_results=1,
        engine="brave",
        lang=" fr ",
    )

    assert len(results) == 1
    assert results[0].url == "https://gallica.bnf.fr/ark:/12148/bpt6k1234567"
    assert results[0].extras["source_engine"] == "brave"
    assert captured["urls"] == [web_search.BRAVE_SEARCH_URL]
    assert captured["headers"][0]["X-Subscription-Token"] == "test-key"
    assert captured["params"][0]["q"] == "guerre d'Algerie"
    assert captured["params"][0]["count"] == "1"
    assert captured["params"][0]["search_lang"] == "fr"


async def test_search_brave_omits_search_lang_when_unset(monkeypatch):
    payload = _load_json("web_search/lang-fr.json")
    captured = _patch_brave_httpx(monkeypatch, payload=payload)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")

    results = await web_search.search("guerre d'Algerie", max_results=5, engine="brave")

    assert len(results) == 1
    assert captured["params"][0]["q"] == "guerre d'Algerie"
    assert captured["params"][0]["count"] == "5"
    assert "search_lang" not in captured["params"][0]


async def test_search_auto_brave_includes_lang_when_key_set(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    payload = _load_json("web_search/lang-fr.json")
    captured = _patch_brave_httpx(monkeypatch, payload=payload)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")

    results = await web_search.search("gallica presse", max_results=1, lang="fr")

    assert len(results) == 1
    assert results[0].extras["source_engine"] == "brave"
    assert captured["params"][0]["search_lang"] == "fr"


async def test_search_auto_ddg_fallback_logs_lang_ignored_and_proceeds(
    monkeypatch,
    caplog,
):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    page = FakePage(_load("web_search_ddg.html"))
    _patch_browser_session(monkeypatch, page)

    caplog.set_level(logging.INFO, logger="research_agent.tools.web_search")
    results = await web_search.search("openai gpt-5", max_results=2, lang="fr")

    assert len(results) == 2
    assert all(hit.extras["source_engine"] == "ddg" for hit in results)
    assert page.goto_calls
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "lang='fr' ignored" in message and "engine=ddg" in message
        for message in messages
    )


async def test_search_auto_ddg_fallback_with_lang_zero_results_is_quiet(
    monkeypatch,
    tmp_path,
    caplog,
):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    page = FakePage("<html><body>no matches here</body></html>")
    _patch_browser_session(monkeypatch, page)

    caplog.set_level(logging.INFO, logger="research_agent.tools.web_search")
    results = await web_search.search("test", max_results=1, lang="fr")

    assert results == []
    assert page.goto_calls
    assert not list((tmp_path / "data" / "diagnostics" / "web_search").glob("*.png"))
    messages = [record.getMessage() for record in caplog.records]
    assert any("lang='fr' ignored" in message for message in messages)
    assert not any("returned 0 results" in message for message in messages)
    assert not any(
        record.levelno >= logging.WARNING and "selector drift" in record.getMessage()
        for record in caplog.records
    )


async def test_search_auto_ddg_fallback_with_lang_session_failure_is_quiet(
    monkeypatch,
    caplog,
):
    @asynccontextmanager
    async def _broken_session(headful=None, block_media=True):
        raise web_search.PlaywrightError("launch denied")
        yield

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.setattr(browser, "browser_session", _broken_session)

    caplog.set_level(logging.INFO, logger="research_agent.tools.web_search")
    results = await web_search.search("test", max_results=1, lang="fr")

    assert results == []
    messages = [record.getMessage() for record in caplog.records]
    assert any("lang='fr' ignored" in message for message in messages)
    assert not any("browser session failed" in message for message in messages)
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


# ---------------------------------------------------------------------------
# Tavily engine tests
# ---------------------------------------------------------------------------


async def test_search_tavily_maps_results_with_score(monkeypatch):
    """Successful Tavily search maps results to SearchResult with tavily_score."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")

    fake_response = {
        "results": [
            {
                "url": "https://example.com/tavily-hit",
                "title": "Tavily Hit One",
                "content": "Snippet from Tavily",
                "score": 0.95,
            },
            {
                "url": "https://example.com/tavily-hit-2",
                "title": "Tavily Hit Two",
                "content": "Another snippet",
                "score": 0.80,
            },
        ]
    }

    class _FakeAsyncTavilyClient:
        def __init__(self, api_key):
            self._api_key = api_key

        async def search(self, *, query, max_results, search_depth):
            return fake_response

    monkeypatch.setattr(
        "tavily.AsyncTavilyClient", _FakeAsyncTavilyClient, raising=False
    )
    # Force re-import so the lazy import inside _search_tavily picks up the mock.
    import tavily

    monkeypatch.setattr(tavily, "AsyncTavilyClient", _FakeAsyncTavilyClient)

    results = await web_search.search("test query", max_results=5, engine="tavily")

    assert len(results) == 2
    assert all(isinstance(r, SearchResult) for r in results)
    assert results[0].url == "https://example.com/tavily-hit"
    assert results[0].title == "Tavily Hit One"
    assert results[0].snippet == "Snippet from Tavily"
    assert results[0].source_kind == "web"
    assert results[0].extras["source_engine"] == "tavily"
    assert results[0].extras["tavily_score"] == 0.95
    assert results[1].extras["tavily_score"] == 0.80


async def test_search_tavily_no_key_returns_empty(monkeypatch, caplog):
    """Missing TAVILY_API_KEY → warning + empty list."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    caplog.set_level(logging.WARNING, logger="research_agent.tools.web_search")
    results = await web_search.search("test", engine="tavily")

    assert results == []
    assert any("TAVILY_API_KEY not set" in r.getMessage() for r in caplog.records)


async def test_search_tavily_exception_returns_empty(monkeypatch, caplog):
    """Network/auth errors in Tavily → warning + empty list, no crash."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")

    class _BrokenClient:
        def __init__(self, api_key):
            pass

        async def search(self, **kwargs):
            raise RuntimeError("connection refused")

    import tavily

    monkeypatch.setattr(tavily, "AsyncTavilyClient", _BrokenClient)

    caplog.set_level(logging.WARNING, logger="research_agent.tools.web_search")
    results = await web_search.search("test", engine="tavily")

    assert results == []
    assert any("tavily search failed" in r.getMessage() for r in caplog.records)


async def test_search_tavily_import_error_returns_empty(monkeypatch, caplog):
    """If tavily-python is not installed, _search_tavily warns and returns []."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")

    import builtins

    real_import = builtins.__import__

    def _block_tavily(name, *args, **kwargs):
        if name == "tavily":
            raise ImportError("no module named 'tavily'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_tavily)

    caplog.set_level(logging.WARNING, logger="research_agent.tools.web_search")
    results = await web_search.search("test", engine="tavily")

    assert results == []
    assert any("tavily-python is not installed" in r.getMessage() for r in caplog.records)


async def test_search_tavily_logs_lang_ignored(monkeypatch, caplog):
    """Tavily does not support lang; passing it logs an info message like DDG/Google."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")

    class _FakeAsyncTavilyClient:
        def __init__(self, api_key):
            pass

        async def search(self, **kwargs):
            return {"results": [{"url": "https://example.com/x", "title": "X", "content": "c", "score": 0.5}]}

    import tavily

    monkeypatch.setattr(tavily, "AsyncTavilyClient", _FakeAsyncTavilyClient)

    caplog.set_level(logging.INFO, logger="research_agent.tools.web_search")
    results = await web_search.search("test", engine="tavily", lang="fr")

    assert len(results) == 1
    messages = [r.getMessage() for r in caplog.records]
    assert any("lang='fr' ignored" in m and "engine=tavily" in m for m in messages)


async def test_search_auto_prefers_tavily_when_key_set(monkeypatch):
    """engine='auto' with TAVILY_API_KEY set routes to Tavily."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key-also-set")

    fake_response = {
        "results": [
            {
                "url": "https://example.com/auto-tavily",
                "title": "Auto Tavily",
                "content": "Found via auto",
                "score": 0.9,
            },
        ]
    }

    class _FakeAsyncTavilyClient:
        def __init__(self, api_key):
            pass

        async def search(self, **kwargs):
            return fake_response

    import tavily

    monkeypatch.setattr(tavily, "AsyncTavilyClient", _FakeAsyncTavilyClient)

    results = await web_search.search("test", max_results=5)

    assert len(results) == 1
    assert results[0].extras["source_engine"] == "tavily"


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
    # No API keys → label should say ddg-fallback when results are empty.
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
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

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")

    fake_hit = SearchResult(
        url="https://example.com/p2025",
        title="Project 2025 implementation",
        snippet="Heritage Foundation policy blueprint",
        source_kind="web",
        extras={"source_engine": "brave"},
    )

    async def _fake_brave(query, max_results, *, lang=None):
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
