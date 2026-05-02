"""Wayback Save Page Now (best-effort).

Issue #15 — `web_fetch` fires a non-blocking call to :func:`save` after every
successful retrieval so we have a durable snapshot of the page at the time of
research. The Save Page Now endpoint is famously flaky: timeouts, 429s, 500s,
and silent capture failures are all routine. We treat it as fire-and-forget
and never raise — the worst case is ``archive_url=None`` on the returned
:class:`Source`.

Why a hand-rolled httpx call over ``waybackpy.WaybackMachineSaveAPI``: the
latter is synchronous and retry-heavy (8 tries by default) which would block
the event loop for tens of seconds when SPN is degraded. A single async POST
with a tight timeout fits the "best-effort, never block" contract.
"""

from __future__ import annotations

import logging

import httpx

from research_agent import config

_DEFAULT_USER_AGENT = "research-agent/0.1"
_WAYBACK_BASE = "https://web.archive.org"

logger = logging.getLogger(__name__)


def _resolve_user_agent() -> str:
    return config.get("RESEARCH_USER_AGENT") or _DEFAULT_USER_AGENT


def _absolutise(location: str) -> str:
    """Wayback returns ``Content-Location: /web/<ts>/<url>`` — prepend host."""
    if location.startswith(("http://", "https://")):
        return location
    if not location.startswith("/"):
        location = "/" + location
    return _WAYBACK_BASE + location


async def save(url: str, timeout: float = 30.0) -> str | None:
    """Submit ``url`` to Wayback Save Page Now and return the archive URL.

    Returns None on any error/timeout/non-2xx. Never raises — this is invoked
    as a background task and a crash here would surface as an unhandled
    exception in the daemon's event loop.
    """
    if not url:
        return None

    user_agent = _resolve_user_agent()
    save_url = f"{_WAYBACK_BASE}/save/{url}"
    headers = {"User-Agent": user_agent}

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:
            response = await client.get(save_url)
    except (httpx.HTTPError, OSError) as exc:
        logger.debug("wayback save failed for %s: %s", url, exc)
        return None

    if response.status_code >= 400:
        logger.debug(
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


__all__ = ["save"]
