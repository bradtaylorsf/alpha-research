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
import re
import time
from urllib.parse import urlparse

import httpx
import tenacity

from research_agent import config

_DEFAULT_USER_AGENT = "research-agent/0.1"
_WAYBACK_BASE = "https://web.archive.org"

_RATE_LIMIT_INTERVAL = 5.0
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# archive.today is a small, easily-irritated service. The issue caps us at
# 0.2 RPS per host (5s between submissions), with a separate gate from
# Wayback so a slow archive.today doesn't starve Wayback saves.
_ARCHIVE_TODAY_BASE = "https://archive.today"
_ARCHIVE_TODAY_HOSTS = frozenset({"archive.today", "archive.ph"})
_ARCHIVE_TODAY_RATE_INTERVAL = 5.0

# Captcha pages on archive.today come back as HTML with an HTTP 429 status —
# the body is the giveaway, not the status code. Sniffing the body lets us
# bail before tenacity burns three retries on a challenge that only humans
# can solve.
_CAPTCHA_RE = re.compile(
    r"\b(captcha|verify you are human|hcaptcha|recaptcha)\b",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)

_rate_lock = asyncio.Lock()
_last_save_monotonic: float | None = None

_archive_today_lock = asyncio.Lock()
_last_archive_today_monotonic: float | None = None


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


async def _archive_today_rate_limit_gate() -> None:
    """Block until at least ``_ARCHIVE_TODAY_RATE_INTERVAL`` has passed since
    the last archive.today submission. Separate from Wayback's gate so the
    two hosts don't share a queue.
    """
    global _last_archive_today_monotonic
    async with _archive_today_lock:
        if _last_archive_today_monotonic is not None:
            elapsed = time.monotonic() - _last_archive_today_monotonic
            wait = _ARCHIVE_TODAY_RATE_INTERVAL - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
        _last_archive_today_monotonic = time.monotonic()


def _parse_refresh_header(value: str) -> str | None:
    """Pull the URL out of an HTTP ``Refresh`` header.

    archive.today replies with ``Refresh: 0; url=https://archive.today/<id>``
    when a fresh save is finished. Format is ``<seconds>; url=<target>`` per
    the de-facto WHATWG spec; tolerate quoting and whitespace.
    """
    if not value:
        return None
    parts = value.split(";", 1)
    if len(parts) != 2:
        return None
    target = parts[1].strip()
    if target.lower().startswith("url="):
        target = target[4:].strip()
    return target.strip("\"'") or None


def _normalize_archive_today_url(url: str) -> str | None:
    """Return ``https://archive.today/<id>`` for any archive.today/.ph link.

    archive.ph and archive.today are the same service behind different
    hostnames; the issue requires we surface the canonical archive.today
    form. Returns None for non-archive hosts or for the bare ``/submit/``
    form (which is what we POST'ed to, not an archive landing page).
    """
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.hostname not in _ARCHIVE_TODAY_HOSTS:
        return None
    path = parsed.path.strip("/")
    if not path or path.startswith("submit"):
        return None
    return f"{_ARCHIVE_TODAY_BASE}/{path}"


async def _attempt_archive_today(
    url: str, headers: dict[str, str], timeout: float
) -> str | None:
    """Single archive.today submission. Raises ``_TransientError`` on retryable failures."""
    submit_url = f"{_ARCHIVE_TODAY_BASE}/submit/"
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:
            # Form-encoded body — verified with curl per issue note.
            # Query-string variant gets ignored by the server.
            response = await client.post(submit_url, data={"url": url})
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise _TransientError(f"transport error: {exc}") from exc
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("archive.today save failed for %s: %s", url, exc)
        return None

    # Captcha sniff comes BEFORE the retryable-status check: archive.today
    # serves its hcaptcha challenge with an HTTP 429, and re-submitting won't
    # make a human appear. Skip the retry loop and surface a WARN.
    body = response.text or ""
    if _CAPTCHA_RE.search(body):
        logger.warning("archive.today captcha; skipping for %s", url)
        return None

    if response.status_code in _RETRYABLE_STATUSES:
        raise _TransientError(f"archive.today HTTP {response.status_code}")

    if response.status_code >= 400:
        logger.warning(
            "archive.today save returned HTTP %s for %s",
            response.status_code,
            url,
        )
        return None

    refresh = response.headers.get("Refresh")
    if refresh:
        target = _parse_refresh_header(refresh)
        if target:
            normalized = _normalize_archive_today_url(target)
            if normalized:
                return normalized

    location = response.headers.get("Location")
    if location:
        normalized = _normalize_archive_today_url(location)
        if normalized:
            return normalized

    final_url = str(response.url)
    if final_url and final_url != submit_url:
        normalized = _normalize_archive_today_url(final_url)
        if normalized:
            return normalized

    return None


async def archive_today_save(url: str, timeout: float = 30.0) -> str | None:
    """Submit ``url`` to archive.today and return the canonical archive URL.

    Used as a Wayback fallback for paywalled / JS-heavy / robots-blocked
    sites where Save Page Now returns a 404 or refuses. Mirrors :func:`save`'s
    contract: never raises, returns None on any error after retries.
    Rate-limited at 0.2 RPS per host (separately from Wayback).
    """
    if not url:
        return None

    headers = {"User-Agent": _resolve_user_agent()}

    await _archive_today_rate_limit_gate()

    try:
        async for attempt in tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_exponential(multiplier=1, min=1, max=8),
            retry=tenacity.retry_if_exception_type(_TransientError),
            reraise=False,
        ):
            with attempt:
                return await _attempt_archive_today(url, headers, timeout)
    except tenacity.RetryError as exc:
        underlying: BaseException | tenacity.RetryError = exc
        if exc.last_attempt is not None and exc.last_attempt.failed:
            underlying = exc.last_attempt.exception() or exc
        logger.warning(
            "archive.today save failed after retries for %s: %s", url, underlying
        )

    return None


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_save_monotonic, _rate_lock
    global _last_archive_today_monotonic, _archive_today_lock
    _last_save_monotonic = None
    _rate_lock = asyncio.Lock()
    _last_archive_today_monotonic = None
    _archive_today_lock = asyncio.Lock()


__all__ = ["save", "archive_today_save", "reset_for_tests"]
