"""Shared Playwright session manager for browser-driven connectors.

A single Chromium instance is launched lazily on first use and reused across
all callers (web_search, fetch fallbacks, news, reddit, …). Every request is
gated by a per-host token bucket so we don't hammer a single SERP/site even
when multiple connectors run concurrently.

Why a module-level singleton: launching Chromium is ~hundreds of ms and the
daemon is single-process; opening a fresh browser per ``search()`` call would
dominate latency for short queries. The lock around init makes concurrent
``browser_session()`` callers wait for the first to finish bootstrapping.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    async_playwright,
)

from research_agent import config

_DEFAULT_USER_AGENT = "research-agent/0.1"
_BLOCKED_RESOURCE_TYPES = {"font", "image", "media"}
_DEFAULT_HOST_RPS = 1.0
_NAV_TIMEOUT_MS = 30_000


class _HostBucket:
    """Simple per-host pace limiter.

    Not a true leaky bucket — just enforces a minimum gap between requests
    to a given host. Sufficient for politeness; the goal is to avoid bursts
    that look bot-like to public SERPs, not to model bandwidth.
    """

    __slots__ = ("rps", "_next_allowed_at", "_lock")

    def __init__(self, rps: float) -> None:
        self.rps = rps
        self._next_allowed_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            min_gap = 1.0 / self.rps if self.rps > 0 else 0.0
            wait = self._next_allowed_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_allowed_at = now + min_gap


_playwright: Playwright | None = None
_browser: Browser | None = None
_context: BrowserContext | None = None
_init_lock = asyncio.Lock()
_host_buckets: dict[str, _HostBucket] = {}
_buckets_lock = asyncio.Lock()
_atexit_registered = False


def _resolve_headful(headful: bool | None) -> bool:
    if headful is not None:
        return headful
    raw = os.environ.get("RESEARCH_HEADFUL") or config.get("RESEARCH_HEADFUL")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_user_agent() -> str:
    return config.get("RESEARCH_USER_AGENT") or _DEFAULT_USER_AGENT


def set_host_rate(netloc: str, rps: float) -> None:
    """Override the per-host rate (requests per second).

    Connectors call this at module import or first use to register a stricter
    limit (e.g. 0.5 rps for SERPs). Subsequent ``throttle()`` calls for the
    same netloc respect the new rate.
    """
    bucket = _host_buckets.get(netloc)
    if bucket is None:
        _host_buckets[netloc] = _HostBucket(rps)
    else:
        bucket.rps = rps


async def _get_bucket(netloc: str) -> _HostBucket:
    async with _buckets_lock:
        bucket = _host_buckets.get(netloc)
        if bucket is None:
            bucket = _HostBucket(_DEFAULT_HOST_RPS)
            _host_buckets[netloc] = bucket
        return bucket


async def throttle(url: str) -> None:
    """Wait until a request to ``url``'s host is permitted by its bucket."""
    netloc = urlparse(url).netloc
    bucket = await _get_bucket(netloc)
    await bucket.acquire()


async def navigate(page: Page, url: str, *, timeout_ms: int = _NAV_TIMEOUT_MS) -> None:
    """Throttle, then navigate. The standard way connectors should call ``page.goto``."""
    await throttle(url)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)


def _make_route_blocker():
    async def _block(route: Route) -> None:
        if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()

    return _block


async def _ensure_context(*, headful: bool, block_media: bool) -> BrowserContext:
    """Launch (or return cached) shared browser + context."""
    global _playwright, _browser, _context, _atexit_registered
    async with _init_lock:
        if _context is not None:
            return _context

        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=not headful)
        _context = await _browser.new_context(user_agent=_resolve_user_agent())

        if block_media:
            await _context.route("**/*", _make_route_blocker())

        if not _atexit_registered:
            atexit.register(_atexit_shutdown)
            _atexit_registered = True

        return _context


async def shutdown() -> None:
    """Close the shared browser/context. Safe to call multiple times."""
    global _playwright, _browser, _context
    async with _init_lock:
        if _context is not None:
            try:
                await _context.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
            _context = None
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:  # noqa: BLE001
                pass
            _browser = None
        if _playwright is not None:
            try:
                await _playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            _playwright = None


def _atexit_shutdown() -> None:
    """atexit hook — schedules ``shutdown()`` if an event loop is reachable."""
    if _context is None and _browser is None and _playwright is None:
        return
    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(shutdown())
        finally:
            loop.close()
    except Exception:  # noqa: BLE001 — never let atexit raise
        pass


@asynccontextmanager
async def browser_session(
    headful: bool | None = None,
    block_media: bool = True,
) -> AsyncIterator[BrowserContext]:
    """Yield the shared :class:`BrowserContext`, lazy-launching if needed.

    The same context is yielded to every caller for the life of the process —
    not closed on context-manager exit. Call :func:`shutdown` from the daemon's
    teardown to release Chromium.
    """
    resolved_headful = _resolve_headful(headful)
    context = await _ensure_context(headful=resolved_headful, block_media=block_media)
    yield context


def reset_for_tests() -> None:
    """Reset module state so tests can re-init cleanly. Test-only."""
    global _playwright, _browser, _context, _atexit_registered
    _playwright = None
    _browser = None
    _context = None
    _atexit_registered = False
    _host_buckets.clear()


__all__ = [
    "browser_session",
    "navigate",
    "reset_for_tests",
    "set_host_rate",
    "shutdown",
    "throttle",
]
