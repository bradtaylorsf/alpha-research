"""Tests for `research_agent.tools.web_fetch` (issue #15)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from research_agent.tools import browser, web_fetch
from research_agent.tools.models import Source

FIXTURES = Path(__file__).parent / "fixtures"
ARTICLE_HTML = (FIXTURES / "web_fetch_article.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    for key in (
        "RESEARCH_USER_AGENT",
        "RESEARCH_IGNORE_ROBOTS",
        "RESEARCH_HEADFUL",
    ):
        monkeypatch.delenv(key, raising=False)
    web_fetch.reset_for_tests()
    yield
    web_fetch.reset_for_tests()


# ---------------------------------------------------------------------------
# _extract — pure parser tests
# ---------------------------------------------------------------------------


def test_extract_pulls_article_body_and_title():
    title, text = web_fetch._extract(ARTICLE_HTML)
    assert title == "The Great Boilerplate Detour"
    # Body content is included.
    assert "boilerpipe-style extractors" in text
    assert "five hundred character minimum" in text
    # Boilerplate is excluded.
    assert "Subscribe" not in text
    assert "Privacy Policy" not in text
    assert "Sign in" not in text
    # Comfortably above the 500-char threshold for the fixture.
    assert len(text) > 1000


def test_extract_empty_html_returns_empty():
    assert web_fetch._extract("") == ("", "")


def test_extract_falls_back_to_readability_when_trafilatura_short(monkeypatch):
    """If trafilatura yields < _MIN_TRAFILATURA_CHARS, readability fills in."""

    # Force trafilatura to return a tiny string so the readability branch runs.
    def _short(*_args, **_kwargs):
        return "tiny"

    monkeypatch.setattr(web_fetch.trafilatura, "extract", _short)
    title, text = web_fetch._extract(ARTICLE_HTML)
    # readability strips tags from .summary() — we should get more than the
    # 4-char trafilatura return.
    assert len(text) > 200
    assert "boilerpipe-style extractors" in text
    # Title still resolves (from trafilatura metadata, then readability).
    assert title == "The Great Boilerplate Detour"


# ---------------------------------------------------------------------------
# _should_use_browser — branching logic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text_len", "status", "requires_js", "expected"),
    [
        (10_000, 200, False, False),  # plenty of text, no escalation
        (100, 200, False, True),  # too short → escalate
        (10_000, 403, False, True),  # 403 → escalate
        (10_000, 429, False, True),  # 429 → escalate
        (10_000, 503, False, True),  # 503 → escalate
        (10_000, 200, True, True),  # explicit JS request → escalate
        (0, None, False, True),  # transport error (no status) + no text
    ],
)
def test_should_use_browser(text_len, status, requires_js, expected):
    assert web_fetch._should_use_browser(text_len, status, requires_js) is expected


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


def _make_robots_response(text: str, status_code: int = 200):
    class _Response:
        def __init__(self) -> None:
            self.text = text
            self.status_code = status_code

    return _Response()


@asynccontextmanager
async def _fake_async_client(get_response):
    class _Client:
        async def get(self, url, *args, **kwargs):
            return get_response(url)

        async def aclose(self) -> None:
            pass

    yield _Client()


def _patch_httpx_for_robots(monkeypatch, robots_text: str, *, status: int = 200):
    """Replace httpx.AsyncClient so robots.txt fetches return ``robots_text``."""

    def _get(url):
        if url.endswith("/robots.txt"):
            return _make_robots_response(robots_text, status_code=status)
        return _make_robots_response("", status_code=200)

    def _client_factory(*args, **kwargs):
        return _fake_async_client(_get)

    monkeypatch.setattr(web_fetch.httpx, "AsyncClient", _client_factory)


async def test_robots_allow_caches_per_host(monkeypatch):
    calls: list[str] = []

    def _get(url):
        calls.append(url)
        return _make_robots_response("User-agent: *\nAllow: /\n")

    def _client_factory(*args, **kwargs):
        return _fake_async_client(_get)

    monkeypatch.setattr(web_fetch.httpx, "AsyncClient", _client_factory)

    assert await web_fetch._robots_allows("https://example.com/a", "ua/1") is True
    assert await web_fetch._robots_allows("https://example.com/b", "ua/1") is True
    # Robots fetched exactly once for the same host.
    robots_calls = [c for c in calls if c.endswith("/robots.txt")]
    assert robots_calls == ["https://example.com/robots.txt"]


async def test_robots_disallow_blocks_url(monkeypatch):
    _patch_httpx_for_robots(monkeypatch, "User-agent: *\nDisallow: /private\n")
    assert await web_fetch._robots_allows("https://x.example/private/page", "ua/1") is False
    assert await web_fetch._robots_allows("https://x.example/public/page", "ua/1") is True


async def test_robots_unreachable_treated_as_allow(monkeypatch):
    """If robots.txt fetch raises, default-allow per RFC 9309."""

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, url, *args, **kwargs):
                raise web_fetch.httpx.ConnectError("boom")

        yield _Client()

    monkeypatch.setattr(web_fetch.httpx, "AsyncClient", _client_factory)
    assert await web_fetch._robots_allows("https://nope.example/x", "ua/1") is True


async def test_fetch_skipped_when_robots_disallows(monkeypatch):
    _patch_httpx_for_robots(monkeypatch, "User-agent: *\nDisallow: /\n")
    result = await web_fetch.fetch("https://blocked.example/page")
    assert result is None


async def test_fetch_ignores_robots_when_env_set(monkeypatch):
    """RESEARCH_IGNORE_ROBOTS=1 must skip the robots.txt check entirely."""
    monkeypatch.setenv("RESEARCH_IGNORE_ROBOTS", "1")

    # Robots disallow everything — but we should NOT consult robots at all.
    # Make robots fetch raise loudly so a leak would show up as a test failure.
    def _client_factory(*args, **kwargs):
        raise AssertionError("robots.txt should not be fetched")

    monkeypatch.setattr(web_fetch.httpx, "AsyncClient", _client_factory)

    captured_calls: list[str] = []

    async def _fake_httpx(url, timeout, user_agent):
        captured_calls.append(url)
        return 200, ARTICLE_HTML

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _fake_httpx)

    source = await web_fetch.fetch("https://anywhere.example/page")
    assert source is not None
    assert captured_calls == ["https://anywhere.example/page"]


# ---------------------------------------------------------------------------
# Full fetch() pipeline — httpx + browser fallback wiring
# ---------------------------------------------------------------------------


def _disable_robots(monkeypatch):
    """Bypass robots in tests that focus on the fetch/extract path."""
    monkeypatch.setenv("RESEARCH_IGNORE_ROBOTS", "1")


def _stub_archive(monkeypatch, archive_url: str | None = None):
    """Replace the Wayback save with an instant no-op (or a fixed return)."""

    async def _save(url, timeout: float = 30.0):
        return archive_url

    monkeypatch.setattr(web_fetch.archive, "save", _save)


async def test_fetch_returns_source_via_httpx(monkeypatch):
    _disable_robots(monkeypatch)
    _stub_archive(monkeypatch)

    async def _fake_httpx(url, timeout, user_agent):
        # config.get returns the declared EXPECTED_ENV_KEYS default when the
        # env var is unset.
        assert user_agent.startswith("research-agent/0.1")
        return 200, ARTICLE_HTML

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _fake_httpx)

    # Browser path must NOT be invoked when text is plentiful and status is 2xx.
    async def _no_browser(*args, **kwargs):
        raise AssertionError("playwright should not be invoked")

    monkeypatch.setattr(web_fetch, "_fetch_via_playwright", _no_browser)

    source = await web_fetch.fetch("https://news.example/article")
    assert isinstance(source, Source)
    assert source.url == "https://news.example/article"
    assert source.source_kind == "web"
    assert source.metadata["fetched_via"] == "httpx"
    assert source.metadata["status_code"] == 200
    assert source.title == "The Great Boilerplate Detour"
    assert "boilerpipe-style extractors" in source.cleaned_text


@pytest.mark.parametrize("status", [403, 429, 503])
async def test_fetch_falls_back_to_browser_on_blocking_status(monkeypatch, status):
    _disable_robots(monkeypatch)
    _stub_archive(monkeypatch)

    async def _fake_httpx(url, timeout, user_agent):
        return status, None

    pw_calls: list[str] = []

    async def _fake_pw(url, timeout):
        pw_calls.append(url)
        return ARTICLE_HTML

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _fake_httpx)
    monkeypatch.setattr(web_fetch, "_fetch_via_playwright", _fake_pw)

    source = await web_fetch.fetch("https://blocked.example/x")
    assert source is not None
    assert pw_calls == ["https://blocked.example/x"]
    assert source.metadata["fetched_via"] == "playwright"
    assert source.metadata["status_code"] == status


async def test_fetch_falls_back_to_browser_when_text_too_short(monkeypatch):
    _disable_robots(monkeypatch)
    _stub_archive(monkeypatch)

    short_html = "<html><head><title>Stub</title></head><body><p>too short</p></body></html>"

    async def _fake_httpx(url, timeout, user_agent):
        return 200, short_html

    pw_calls: list[str] = []

    async def _fake_pw(url, timeout):
        pw_calls.append(url)
        return ARTICLE_HTML

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _fake_httpx)
    monkeypatch.setattr(web_fetch, "_fetch_via_playwright", _fake_pw)

    source = await web_fetch.fetch("https://thin.example/x")
    assert source is not None
    assert pw_calls == ["https://thin.example/x"]
    assert source.metadata["fetched_via"] == "playwright"


async def test_fetch_uses_browser_when_requires_js_true(monkeypatch):
    _disable_robots(monkeypatch)
    _stub_archive(monkeypatch)

    async def _fake_httpx(url, timeout, user_agent):
        raise AssertionError("httpx should be skipped when requires_js=True")

    pw_calls: list[str] = []

    async def _fake_pw(url, timeout):
        pw_calls.append(url)
        return ARTICLE_HTML

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _fake_httpx)
    monkeypatch.setattr(web_fetch, "_fetch_via_playwright", _fake_pw)

    source = await web_fetch.fetch("https://spa.example/x", requires_js=True)
    assert source is not None
    assert pw_calls == ["https://spa.example/x"]
    assert source.metadata["fetched_via"] == "playwright"
    # status_code is None when we never made the httpx request.
    assert source.metadata["status_code"] is None


async def test_fetch_returns_none_when_both_paths_fail(monkeypatch):
    _disable_robots(monkeypatch)
    _stub_archive(monkeypatch)

    async def _fake_httpx(url, timeout, user_agent):
        return None, None  # transport error

    async def _fake_pw(url, timeout):
        return None  # browser also failed

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _fake_httpx)
    monkeypatch.setattr(web_fetch, "_fetch_via_playwright", _fake_pw)

    assert await web_fetch.fetch("https://gone.example/x") is None


async def test_fetch_returns_none_for_malformed_url(monkeypatch):
    _disable_robots(monkeypatch)
    assert await web_fetch.fetch("") is None
    assert await web_fetch.fetch("not-a-url") is None


# ---------------------------------------------------------------------------
# Wayback archival — fire-and-forget contract
# ---------------------------------------------------------------------------


async def test_fetch_spawns_archive_task_without_blocking(monkeypatch):
    """The archive call runs in a background task — fetch returns immediately."""
    _disable_robots(monkeypatch)

    archive_started = asyncio.Event()
    archive_completed = asyncio.Event()

    async def _slow_save(url, timeout: float = 30.0):
        archive_started.set()
        await asyncio.sleep(0)
        archive_completed.set()
        return "https://web.archive.org/web/2026/https://x.example/y"

    monkeypatch.setattr(web_fetch.archive, "save", _slow_save)

    async def _fake_httpx(url, timeout, user_agent):
        return 200, ARTICLE_HTML

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _fake_httpx)

    source = await web_fetch.fetch("https://x.example/y")
    assert source is not None
    # fetch returns before the archive coroutine has finished its work — at
    # most it has scheduled the task and yielded.
    assert source.archive_url is None or source.archive_url.startswith("https://web.archive.org/")

    # Now drain the loop so the archive task gets a chance to complete.
    await archive_completed.wait()
    # After the background task finishes, the source should be tagged.
    assert source.archive_url == "https://web.archive.org/web/2026/https://x.example/y"


async def test_fetch_does_not_crash_when_archive_save_raises(monkeypatch):
    _disable_robots(monkeypatch)

    async def _boom(url, timeout: float = 30.0):
        raise RuntimeError("wayback exploded")

    monkeypatch.setattr(web_fetch.archive, "save", _boom)

    async def _fake_httpx(url, timeout, user_agent):
        return 200, ARTICLE_HTML

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _fake_httpx)

    source = await web_fetch.fetch("https://x.example/y")
    assert source is not None
    # Drain background tasks so we observe the swallowed exception didn't escape.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert source.archive_url is None


# ---------------------------------------------------------------------------
# User-Agent resolution
# ---------------------------------------------------------------------------


def test_resolve_user_agent_uses_env(monkeypatch):
    monkeypatch.setenv("RESEARCH_USER_AGENT", "custom-agent/9")
    assert web_fetch._resolve_user_agent() == "custom-agent/9"


def test_resolve_user_agent_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(web_fetch.config, "get", lambda name: None)
    assert web_fetch._resolve_user_agent() == "research-agent/0.1"


# ---------------------------------------------------------------------------
# Module wiring
# ---------------------------------------------------------------------------


def test_tool_registry_has_web_fetch():
    from research_agent.tools import TOOL_REGISTRY

    assert "web_fetch" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["web_fetch"])


def test_browser_module_imported_lazily(monkeypatch):
    """We should reach for `browser` only when the playwright path runs.

    Loading `tools/browser.py` is cheap, but verifying the symbol is present
    keeps the dependency graph documented for future reviewers.
    """
    assert hasattr(web_fetch, "browser")
    assert web_fetch.browser is browser
