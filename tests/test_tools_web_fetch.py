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
        return 200, ARTICLE_HTML, ARTICLE_HTML.encode("utf-8"), "text/html"

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
    """Replace Wayback save (and the archive.today fallback) with no-ops.

    Both saves are stubbed because :func:`_spawn_archive_task` falls through
    to archive.today when Wayback returns None — without a stub the test
    would hit the real archive.today endpoint.
    """

    async def _save(url, timeout: float = 30.0):
        return archive_url

    async def _archive_today_save(url, timeout: float = 30.0):
        return None

    monkeypatch.setattr(web_fetch.archive, "save", _save)
    monkeypatch.setattr(web_fetch.archive, "archive_today_save", _archive_today_save)


async def test_fetch_returns_source_via_httpx(monkeypatch):
    _disable_robots(monkeypatch)
    _stub_archive(monkeypatch)

    async def _fake_httpx(url, timeout, user_agent):
        # config.get returns the declared EXPECTED_ENV_KEYS default when the
        # env var is unset.
        assert user_agent.startswith("research-agent/0.1")
        return 200, ARTICLE_HTML, ARTICLE_HTML.encode("utf-8"), "text/html"

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
        return status, None, None, "text/html"

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
        return 200, short_html, short_html.encode("utf-8"), "text/html"

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
        return None, None, None, None  # transport error

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
        return 200, ARTICLE_HTML, ARTICLE_HTML.encode("utf-8"), "text/html"

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

    async def _archive_today_none(url, timeout: float = 30.0):
        return None

    monkeypatch.setattr(web_fetch.archive, "save", _boom)
    monkeypatch.setattr(web_fetch.archive, "archive_today_save", _archive_today_none)

    async def _fake_httpx(url, timeout, user_agent):
        return 200, ARTICLE_HTML, ARTICLE_HTML.encode("utf-8"), "text/html"

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _fake_httpx)

    source = await web_fetch.fetch("https://x.example/y")
    assert source is not None
    # Drain background tasks so we observe the swallowed exception didn't escape.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert source.archive_url is None


async def test_fetch_falls_back_to_archive_today_when_wayback_fails(monkeypatch):
    """Wayback returning None must trigger the archive.today fallback, with
    the archive.today URL written back onto the Source."""
    _disable_robots(monkeypatch)

    archive_today_completed = asyncio.Event()

    async def _wayback_save(url, timeout: float = 30.0):
        return None  # Wayback refuses (404, robots-blocked, etc.)

    async def _archive_today_save(url, timeout: float = 30.0):
        archive_today_completed.set()
        return "https://archive.today/abc12"

    monkeypatch.setattr(web_fetch.archive, "save", _wayback_save)
    monkeypatch.setattr(web_fetch.archive, "archive_today_save", _archive_today_save)

    async def _fake_httpx(url, timeout, user_agent):
        return 200, ARTICLE_HTML, ARTICLE_HTML.encode("utf-8"), "text/html"

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _fake_httpx)

    source = await web_fetch.fetch("https://x.example/y")
    assert source is not None
    await archive_today_completed.wait()
    await asyncio.sleep(0)
    assert source.archive_url == "https://archive.today/abc12"


async def test_fetch_logs_warning_when_both_archives_fail(monkeypatch, caplog):
    """When both Wayback and archive.today return None, we surface a WARN
    with both errors so the operator can see the archive_failed signal."""
    import logging

    _disable_robots(monkeypatch)

    failures_complete = asyncio.Event()

    async def _wayback_save(url, timeout: float = 30.0):
        return None

    async def _archive_today_save(url, timeout: float = 30.0):
        failures_complete.set()
        return None

    monkeypatch.setattr(web_fetch.archive, "save", _wayback_save)
    monkeypatch.setattr(web_fetch.archive, "archive_today_save", _archive_today_save)

    async def _fake_httpx(url, timeout, user_agent):
        return 200, ARTICLE_HTML, ARTICLE_HTML.encode("utf-8"), "text/html"

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _fake_httpx)

    with caplog.at_level(logging.WARNING, logger=web_fetch.logger.name):
        source = await web_fetch.fetch("https://x.example/y")
        assert source is not None
        await failures_complete.wait()
        await asyncio.sleep(0)

    assert source.archive_url is None
    assert any(
        "archive_failed" in rec.getMessage()
        and "wayback=failed" in rec.getMessage()
        and "archive_today=failed" in rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    )


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


# ---------------------------------------------------------------------------
# Connector host-dispatch (issue #174)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "module_name"),
    [
        ("https://www.congress.gov/bill/118th-congress/h-r/1", "congress"),
        ("https://www.fec.gov/data/candidate/P00000001/", "fec"),
        ("https://www.sec.gov/Archives/edgar/data/123/000012345.htm", "edgar"),
        ("https://www.federalregister.gov/documents/2024/01/01/x", "fedregister"),
        ("https://www.courtlistener.com/opinion/12345/foo/", "courtlistener"),
        ("https://lda.senate.gov/filings/public/filing/abcd/", "lda"),
        ("https://www.usaspending.gov/award/CONT_AWD_X/", "usaspending"),
        ("https://littlesis.org/entity/123-Peter-Thiel", "littlesis"),
        (
            "https://projects.propublica.org/nonprofits/organizations/123456789",
            "nonprofits",
        ),
        (
            "https://sanctionssearch.ofac.treas.gov/Details.aspx?id=42",
            "sanctions",
        ),
        ("https://powersearch.sos.ca.gov/results.aspx?id=42", "calaccess"),
        (
            "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/LicenseDetail.aspx?LicNum=42",
            "licensing",
        ),
        (
            "https://bizfileonline.sos.ca.gov/search/business/2741233",
            "sos",
        ),
        ("https://www.bbb.org/us/ca/santa-clara/profile/general-contractor/sbi", "bbb"),
        ("https://trove.nla.gov.au/newspaper/article/18342701", "trove"),
        ("https://nla.gov.au/nla.news-article18342701", "trove"),
        ("https://en.wikisource.org/wiki/Treaty_of_Versailles", "wikisource"),
        ("https://fr.wikisource.org/wiki/La_Marseillaise", "wikisource"),
        ("https://catalog.hathitrust.org/Record/000578050", "hathitrust"),
    ],
)
async def test_fetch_dispatches_to_connector_by_host(monkeypatch, url, module_name):
    """A `site:`-scoped query that lands on a connector domain must reach
    that connector — not be eaten by the generic httpx + trafilatura path.

    Issue #174: planner-emitted ``site:congress.gov`` etc. only routes to
    the right connector if the host-dispatch table covers it.
    """
    _disable_robots(monkeypatch)
    _stub_archive(monkeypatch)

    # Make the generic paths blow up — if dispatch is missing, the test fails.
    async def _no_httpx(*args, **kwargs):
        raise AssertionError(
            f"web_fetch fell through to httpx for {url}; expected dispatch to {module_name}"
        )

    async def _no_browser(*args, **kwargs):
        raise AssertionError(
            f"web_fetch fell through to playwright for {url}; expected dispatch to {module_name}"
        )

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _no_httpx)
    monkeypatch.setattr(web_fetch, "_fetch_via_playwright", _no_browser)

    captured: list[str] = []

    async def _fake_connector_fetch(target_url, *args, **kwargs):
        captured.append(target_url)
        return None  # contract: connector decides; None is a valid answer

    # Patch the connector module's fetch so we don't actually hit the network.
    # The dispatch imports lazily (`from research_agent.tools import <name>`),
    # so we patch at the package level.
    import importlib

    module = importlib.import_module(f"research_agent.tools.{module_name}")
    monkeypatch.setattr(module, "fetch", _fake_connector_fetch)

    result = await web_fetch.fetch(url)
    assert captured == [url], (
        f"expected {module_name}.fetch to receive {url}, got {captured}"
    )
    assert result is None  # connector returned None; dispatch must not fall back


@pytest.mark.parametrize(
    ("url", "module_name"),
    [
        # Bare-domain forms — search engines occasionally return URLs without
        # the canonical ``www.`` prefix. Before the connector host-gates were
        # widened to accept bare hosts, dispatch routed these to the connector
        # which silently returned None, dropping the page entirely (regression
        # vs. the pre-dispatch generic httpx fallback).
        ("https://congress.gov/bill/118th-congress/h-r/1", "congress"),
        ("https://fec.gov/data/candidate/P00000001/", "fec"),
        ("https://bbb.org/us/ca/santa-clara/profile/general-contractor/sbi", "bbb"),
        ("https://www.lda.gov/filings/public/filing/abcd/", "lda"),
    ],
)
async def test_fetch_dispatches_to_connector_for_bare_domain(
    monkeypatch, url, module_name
):
    """Bare-domain URLs must reach the connector, not be silently dropped.

    The four connectors with stricter internal host-gates than the dispatch
    table (issue #174 follow-up): congress, fec, bbb, lda. If the connector
    rejects the bare host, web_fetch returns None and the page is lost — a
    regression vs. the pre-dispatch generic httpx fallback. Each connector
    now accepts both ``www.<domain>`` and the bare form.
    """
    _disable_robots(monkeypatch)
    _stub_archive(monkeypatch)

    async def _no_httpx(*args, **kwargs):
        raise AssertionError(
            f"web_fetch fell through to httpx for {url}; expected dispatch to {module_name}"
        )

    async def _no_browser(*args, **kwargs):
        raise AssertionError(
            f"web_fetch fell through to playwright for {url}; expected dispatch to {module_name}"
        )

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _no_httpx)
    monkeypatch.setattr(web_fetch, "_fetch_via_playwright", _no_browser)

    captured: list[str] = []

    async def _fake_connector_fetch(target_url, *args, **kwargs):
        captured.append(target_url)
        return None

    import importlib

    module = importlib.import_module(f"research_agent.tools.{module_name}")
    monkeypatch.setattr(module, "fetch", _fake_connector_fetch)

    result = await web_fetch.fetch(url)
    assert captured == [url], (
        f"expected {module_name}.fetch to receive {url}, got {captured}"
    )
    assert result is None


async def test_fetch_falls_through_for_congress_bill_text_url(monkeypatch):
    """Issue #193: bill-text URLs on ``www.congress.gov`` (path
    ``/<congress>/bills/<slug>/BILLS-...``) are raw HTML/XML bodies that
    ``congress.fetch`` doesn't handle — its URL classifier only matches the
    canonical ``/bill/<congress>/<chamber>/<n>`` permalink. Without the
    carve-out, the bill-text fan-out hits ``congress.fetch`` → ``None`` →
    ``_persist_fetched_source`` FatalErrors. This test pins the regression:
    bill-text URLs must skip the connector and reach the generic httpx
    extractor.
    """
    _disable_robots(monkeypatch)
    _stub_archive(monkeypatch)

    from research_agent.tools import congress

    async def _should_not_dispatch(*args, **kwargs):
        raise AssertionError(
            "congress.fetch was called for a bill-text URL — should fall through"
        )

    monkeypatch.setattr(congress, "fetch", _should_not_dispatch)

    httpx_calls: list[str] = []

    async def _fake_httpx(url, timeout, user_agent):
        httpx_calls.append(url)
        return 200, ARTICLE_HTML, ARTICLE_HTML.encode("utf-8"), "text/html"

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _fake_httpx)

    bill_text_url = (
        "https://www.congress.gov/117/bills/hr5376/BILLS-117hr5376enr.htm"
    )
    source = await web_fetch.fetch(bill_text_url)
    assert httpx_calls == [bill_text_url]
    assert source is not None
    assert source.metadata["fetched_via"] == "httpx"


async def test_fetch_canonical_bill_url_still_dispatches_to_congress(monkeypatch):
    """Counterpart to the bill-text carve-out: the canonical
    ``/bill/<congress>/<chamber>/<n>`` permalink must still route to
    ``congress.fetch`` (issue #174 contract). The carve-out only covers the
    ``/<congress>/bills/.../BILLS-...`` content-URL shape.
    """
    _disable_robots(monkeypatch)
    _stub_archive(monkeypatch)

    from research_agent.tools import congress

    async def _no_httpx(*args, **kwargs):
        raise AssertionError(
            "web_fetch fell through to httpx for a canonical bill URL"
        )

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _no_httpx)

    captured: list[str] = []

    async def _fake_congress_fetch(target_url, *args, **kwargs):
        captured.append(target_url)
        return None

    monkeypatch.setattr(congress, "fetch", _fake_congress_fetch)

    canonical = "https://www.congress.gov/bill/117th-congress/house-bill/5376"
    result = await web_fetch.fetch(canonical)
    assert captured == [canonical]
    assert result is None


async def test_fetch_falls_through_to_generic_for_propublica_non_nonprofits(
    monkeypatch,
):
    """`projects.propublica.org` hosts multiple ProPublica projects.

    Only the ``/nonprofits/`` path is owned by the nonprofits connector —
    ``/electionland/``, ``/dollars-for-docs/``, etc. should fall through to
    the generic httpx path so we don't break those URLs.
    """
    _disable_robots(monkeypatch)
    _stub_archive(monkeypatch)

    from research_agent.tools import nonprofits

    async def _should_not_dispatch(*args, **kwargs):
        raise AssertionError(
            "nonprofits.fetch was called for a non-/nonprofits/ ProPublica URL"
        )

    monkeypatch.setattr(nonprofits, "fetch", _should_not_dispatch)

    httpx_calls: list[str] = []

    async def _fake_httpx(url, timeout, user_agent):
        httpx_calls.append(url)
        return 200, ARTICLE_HTML, ARTICLE_HTML.encode("utf-8"), "text/html"

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _fake_httpx)

    source = await web_fetch.fetch(
        "https://projects.propublica.org/electionland/"
    )
    assert httpx_calls == ["https://projects.propublica.org/electionland/"]
    assert source is not None
    assert source.metadata["fetched_via"] == "httpx"
