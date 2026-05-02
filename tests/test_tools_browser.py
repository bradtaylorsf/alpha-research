"""Tests for the shared Playwright session manager (`tools/browser.py`)."""

from __future__ import annotations

from typing import Any

import pytest

from research_agent.tools import browser

# ---------------------------------------------------------------------------
# Fakes — enough surface area for browser.py to drive without real Chromium.
# ---------------------------------------------------------------------------


class FakeRoute:
    def __init__(self, request: FakeRequest) -> None:
        self.request = request
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class FakeRequest:
    def __init__(self, resource_type: str) -> None:
        self.resource_type = resource_type


class FakeContext:
    def __init__(self, ua: str) -> None:
        self.ua = ua
        self.routes: list[tuple[str, Any]] = []
        self.closed = False
        self.pages_opened = 0

    async def route(self, pattern: str, handler) -> None:
        self.routes.append((pattern, handler))

    async def new_page(self):
        self.pages_opened += 1
        return object()

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts_created: list[FakeContext] = []
        self.closed = False

    async def new_context(self, *, user_agent: str) -> FakeContext:
        ctx = FakeContext(user_agent)
        self.contexts_created.append(ctx)
        return ctx

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self) -> None:
        self.launches: list[dict[str, Any]] = []
        self.browser = FakeBrowser()

    async def launch(self, *, headless: bool = True):
        self.launches.append({"headless": headless})
        return self.browser


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakePlaywrightStarter:
    """Mimics the object returned by ``async_playwright()``."""

    def __init__(self) -> None:
        self.pw = FakePlaywright()

    async def start(self) -> FakePlaywright:
        return self.pw


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_browser_module(monkeypatch):
    browser.reset_for_tests()
    monkeypatch.delenv("RESEARCH_HEADFUL", raising=False)
    monkeypatch.delenv("RESEARCH_USER_AGENT", raising=False)
    yield
    browser.reset_for_tests()


@pytest.fixture
def fake_playwright(monkeypatch):
    starter = FakePlaywrightStarter()

    def _factory():
        return starter

    monkeypatch.setattr(browser, "async_playwright", _factory)
    return starter


# ---------------------------------------------------------------------------
# Session reuse
# ---------------------------------------------------------------------------


async def test_browser_session_reuses_context_across_calls(fake_playwright):
    async with browser.browser_session() as ctx1:
        pass
    async with browser.browser_session() as ctx2:
        pass
    assert ctx1 is ctx2
    # Single launch, single context.
    assert len(fake_playwright.pw.chromium.launches) == 1
    assert len(fake_playwright.pw.chromium.browser.contexts_created) == 1


async def test_browser_session_blocks_media_by_default(fake_playwright):
    async with browser.browser_session() as ctx:
        pass
    # One route was registered.
    assert len(ctx.routes) == 1
    pattern, handler = ctx.routes[0]
    assert pattern == "**/*"

    for resource_type in ("font", "image", "media"):
        route = FakeRoute(FakeRequest(resource_type))
        await handler(route)
        assert route.aborted, f"{resource_type} should be aborted"
        assert not route.continued

    for resource_type in ("document", "script", "xhr"):
        route = FakeRoute(FakeRequest(resource_type))
        await handler(route)
        assert not route.aborted, f"{resource_type} should pass"
        assert route.continued


async def test_browser_session_block_media_false_skips_route(fake_playwright):
    async with browser.browser_session(block_media=False) as ctx:
        pass
    assert ctx.routes == []


async def test_browser_session_honors_research_headful_env(fake_playwright, monkeypatch):
    monkeypatch.setenv("RESEARCH_HEADFUL", "1")
    async with browser.browser_session() as _ctx:
        pass
    assert fake_playwright.pw.chromium.launches[0]["headless"] is False


async def test_browser_session_default_is_headless(fake_playwright):
    async with browser.browser_session() as _ctx:
        pass
    assert fake_playwright.pw.chromium.launches[0]["headless"] is True


async def test_browser_session_explicit_headful_overrides_env(fake_playwright, monkeypatch):
    monkeypatch.setenv("RESEARCH_HEADFUL", "0")
    async with browser.browser_session(headful=True) as _ctx:
        pass
    assert fake_playwright.pw.chromium.launches[0]["headless"] is False


async def test_browser_session_user_agent_falls_back_to_default(fake_playwright, monkeypatch):
    # Strip both env and config default by monkeypatching config.get.
    monkeypatch.setattr(browser.config, "get", lambda name: None)
    async with browser.browser_session() as ctx:
        pass
    assert ctx.ua == "research-agent/0.1"


async def test_browser_session_uses_research_user_agent_env(fake_playwright, monkeypatch):
    monkeypatch.setenv("RESEARCH_USER_AGENT", "my-custom-ua/2.0")
    # config.get reads from os.environ first.
    async with browser.browser_session() as ctx:
        pass
    assert ctx.ua == "my-custom-ua/2.0"


# ---------------------------------------------------------------------------
# Per-host throttling
# ---------------------------------------------------------------------------


async def test_throttle_enforces_minimum_spacing_per_host(monkeypatch):
    """Two calls in quick succession must result in a sleep of ~1/rps seconds."""
    sleeps: list[float] = []
    fake_now = [1000.0]

    def _monotonic():
        return fake_now[0]

    async def _sleep(d):
        sleeps.append(d)
        fake_now[0] += d

    monkeypatch.setattr(browser.time, "monotonic", _monotonic)
    monkeypatch.setattr(browser.asyncio, "sleep", _sleep)

    browser.set_host_rate("example.com", 1.0)

    await browser.throttle("https://example.com/a")
    await browser.throttle("https://example.com/b")

    # First call: no wait. Second call: ~1.0 seconds.
    assert sleeps == pytest.approx([1.0])


async def test_throttle_independent_hosts_do_not_block_each_other(monkeypatch):
    sleeps: list[float] = []
    fake_now = [2000.0]

    def _monotonic():
        return fake_now[0]

    async def _sleep(d):
        sleeps.append(d)
        fake_now[0] += d

    monkeypatch.setattr(browser.time, "monotonic", _monotonic)
    monkeypatch.setattr(browser.asyncio, "sleep", _sleep)

    browser.set_host_rate("a.example", 1.0)
    browser.set_host_rate("b.example", 1.0)

    await browser.throttle("https://a.example/x")
    await browser.throttle("https://b.example/y")

    # Different hosts share no bucket → no waits.
    assert sleeps == []


async def test_throttle_uses_default_rate_for_unregistered_host(monkeypatch):
    sleeps: list[float] = []
    fake_now = [3000.0]
    monkeypatch.setattr(browser.time, "monotonic", lambda: fake_now[0])

    async def _sleep(d):
        sleeps.append(d)
        fake_now[0] += d

    monkeypatch.setattr(browser.asyncio, "sleep", _sleep)

    await browser.throttle("https://unknown.host/x")
    await browser.throttle("https://unknown.host/y")

    # Default is 1 rps → second call waits ~1s.
    assert sleeps == pytest.approx([1.0])


async def test_set_host_rate_updates_existing_bucket(monkeypatch):
    sleeps: list[float] = []
    fake_now = [4000.0]
    monkeypatch.setattr(browser.time, "monotonic", lambda: fake_now[0])

    async def _sleep(d):
        sleeps.append(d)
        fake_now[0] += d

    monkeypatch.setattr(browser.asyncio, "sleep", _sleep)

    # Start with 1 rps, tighten to 0.5 rps (one per 2s) before the first use,
    # then verify subsequent gaps reflect the tighter rate.
    browser.set_host_rate("rate.example", 1.0)
    browser.set_host_rate("rate.example", 0.5)

    await browser.throttle("https://rate.example/x")
    await browser.throttle("https://rate.example/y")
    await browser.throttle("https://rate.example/z")

    assert sleeps == pytest.approx([2.0, 2.0])


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def test_shutdown_closes_browser_and_context(fake_playwright):
    async with browser.browser_session() as ctx:
        pass
    await browser.shutdown()
    assert ctx.closed
    assert fake_playwright.pw.chromium.browser.closed
    assert fake_playwright.pw.stopped


async def test_shutdown_is_idempotent(fake_playwright):
    async with browser.browser_session() as _ctx:
        pass
    await browser.shutdown()
    # Second call should not raise even though everything's already torn down.
    await browser.shutdown()
