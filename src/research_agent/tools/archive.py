"""Wayback Save Page Now (best-effort).

`web_fetch` (issue #15) fires a non-blocking call to :func:`save` after every
successful retrieval so we have a durable snapshot of the page at the time of
research. The Save Page Now endpoint is famously flaky: timeouts, 429s, 500s,
and silent capture failures are all routine. We treat it as fire-and-forget
and never raise — the worst case is ``archive_url=None`` on the returned
:class:`Source`.

Issue #16 layers in two contracts on top of issue #15's hand-rolled call:

* Per-process rate limiting — Wayback's SPN is sensitive to bursts, so all
  callers in this process serialise through a shared lock that enforces at
  least ``_RATE_LIMIT_INTERVAL`` seconds between submissions. Concurrent
  ``save()`` calls queue rather than overlap.
* Tenacity-driven retries with exponential backoff for transient failures
  (network errors, timeouts, 408/425/429/5xx) — bounded to three attempts
  total so a degraded SPN doesn't pin a worker forever.

Why a hand-rolled httpx call over ``waybackpy.WaybackMachineSaveAPI``: the
latter is synchronous and retry-heavy (8 tries by default) which would block
the event loop for tens of seconds when SPN is degraded. A single async GET
with explicit retry/backoff fits the "best-effort, never block long" contract.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
import tenacity

from research_agent import config

_DEFAULT_USER_AGENT = "research-agent/0.1"
_WAYBACK_BASE = "https://web.archive.org"

_RATE_LIMIT_INTERVAL = 5.0
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

logger = logging.getLogger(__name__)

_rate_lock = asyncio.Lock()
_last_save_monotonic: float | None = None


class _TransientError(Exception):
    """Internal marker for failures the retry policy should consider retryable."""


def _resolve_user_agent() -> str:
    return config.get("RESEARCH_USER_AGENT") or _DEFAULT_USER_AGENT


def _absolutise(location: str) -> str:
    """Wayback returns ``Content-Location: /web/<ts>/<url>`` — prepend host."""
    if location.startswith(("http://", "https://")):
        return location
    if not location.startswith("/"):
        location = "/" + location
    return _WAYBACK_BASE + location


async def _rate_limit_gate() -> None:
    """Block until at least ``_RATE_LIMIT_INTERVAL`` has passed since the last save.

    Acquired serially via ``_rate_lock`` so concurrent callers queue instead
    of all firing simultaneously and tripping SPN's per-IP limits.
    """
    global _last_save_monotonic
    async with _rate_lock:
        if _last_save_monotonic is not None:
            elapsed = time.monotonic() - _last_save_monotonic
            wait = _RATE_LIMIT_INTERVAL - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
        _last_save_monotonic = time.monotonic()


async def _attempt(url: str, headers: dict[str, str], timeout: float) -> str | None:
    """Single SPN submission. Raises ``_TransientError`` on retryable failures."""
    save_url = f"{_WAYBACK_BASE}/save/{url}"
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:
            response = await client.get(save_url)
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise _TransientError(f"transport error: {exc}") from exc
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("wayback save failed for %s: %s", url, exc)
        return None

    if response.status_code in _RETRYABLE_STATUSES:
        raise _TransientError(f"wayback HTTP {response.status_code}")

    if response.status_code >= 400:
        logger.warning(
            "wayback save returned HTTP %s for %s",
            response.status_code,
            url,
        )
        return None

    # SPN reports the archive URL in either Content-Location or Location;
    # if it followed redirects, the final response.url is the canonical
    # ``/web/<timestamp>/<url>`` page. Try them in priority order.
    for header in ("Content-Location", "Location"):
        value = response.headers.get(header)
        if value:
            return _absolutise(value)

    final_url = str(response.url)
    if "/web/" in final_url and final_url != save_url:
        return final_url

    return None


async def save(url: str, timeout: float = 30.0) -> str | None:
    """Submit ``url`` to Wayback Save Page Now and return the archive URL.

    Returns None on any error/timeout/non-2xx after retries are exhausted.
    Never raises — this is invoked as a background task and a crash here
    would surface as an unhandled exception in the daemon's event loop.
    """
    if not url:
        return None

    headers = {"User-Agent": _resolve_user_agent()}

    await _rate_limit_gate()

    try:
        async for attempt in tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_exponential(multiplier=1, min=1, max=8),
            retry=tenacity.retry_if_exception_type(_TransientError),
            reraise=False,
        ):
            with attempt:
                return await _attempt(url, headers, timeout)
    except tenacity.RetryError as exc:
        underlying: BaseException | tenacity.RetryError = exc
        if exc.last_attempt is not None and exc.last_attempt.failed:
            underlying = exc.last_attempt.exception() or exc
        logger.warning("wayback save failed after retries for %s: %s", url, underlying)

    return None


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_save_monotonic, _rate_lock
    _last_save_monotonic = None
    _rate_lock = asyncio.Lock()


__all__ = ["save", "reset_for_tests"]
