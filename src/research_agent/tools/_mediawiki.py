"""Shared helpers for MediaWiki-backed public connectors.

The Commons and Wikisource connectors both use Wikimedia-hosted Action API
endpoints. Keep the Wikimedia User-Agent, JSON fetch path, light HTML cleanup,
and host-level 1 RPS limiter in one place so sibling connectors coordinate
instead of accidentally doubling traffic.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from research_agent import config

logger = logging.getLogger(__name__)

PROJECT_URL = "https://github.com/bradtaylorsf/muckwire"
RATE_LIMIT_INTERVAL = 1.0

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_EMAIL_RE = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+")

_rate_lock = asyncio.Lock()
_last_call_by_host: dict[str, float] = {}


def _contact_from_user_agent() -> str:
    raw = config.get("RESEARCH_USER_AGENT") or ""
    match = _EMAIL_RE.search(raw)
    return match.group(0) if match else "unset"


def user_agent() -> str:
    """Return a Wikimedia-policy-friendly project-identifying User-Agent."""
    return (
        "research-agent/0.1 "
        f"(+{PROJECT_URL}; contact: {_contact_from_user_agent()})"
    )


def headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": user_agent(),
    }


def _rate_key(host: str) -> str:
    """Coordinate calls by Wikimedia host family.

    AC-X1 asks sibling MediaWiki connectors to share a limiter for Wikimedia
    traffic. Grouping Wikimedia subdomains under the same key is conservative
    and keeps Commons imageinfo/file lookups from racing future siblings.
    """
    normalized = host.lower().strip()
    for suffix in ("wikimedia.org", "wikisource.org", "wikipedia.org"):
        if normalized == suffix or normalized.endswith(f".{suffix}"):
            return suffix
    return normalized


async def rate_limit(url: str) -> None:
    """Wait until this host family has been idle for at least 1 second."""
    global _last_call_by_host
    parsed = urlparse(url)
    key = _rate_key(parsed.hostname or "")
    async with _rate_lock:
        last = _last_call_by_host.get(key)
        if last is not None:
            wait = RATE_LIMIT_INTERVAL - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
        _last_call_by_host[key] = time.monotonic()


async def request_json(
    url: str,
    *,
    params: dict[str, Any],
    timeout: float = 15.0,
) -> dict[str, Any] | None:
    """GET a MediaWiki JSON endpoint; return ``None`` on connector failures."""
    await rate_limit(url)
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers(),
        ) as client:
            response = await client.get(url, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("mediawiki request failed for %s: %s", url, exc)
        return None

    if response.status_code < 200 or response.status_code >= 300:
        logger.warning("mediawiki request returned HTTP %s for %s", response.status_code, url)
        return None
    try:
        payload = response.json()
    except ValueError:
        logger.warning("mediawiki request returned non-JSON response for %s", url)
        return None
    if not isinstance(payload, dict):
        logger.warning("mediawiki JSON root was %s for %s", type(payload).__name__, url)
        return None
    return payload


def clean_text(value: Any) -> str:
    """Strip lightweight MediaWiki/snippet HTML and collapse whitespace."""
    if value is None:
        return ""
    text = str(value)
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def extmetadata_text(extmetadata: dict[str, Any], *keys: str) -> str:
    """Return the first non-empty cleaned ``extmetadata[<key>].value``."""
    for key in keys:
        entry = extmetadata.get(key)
        if isinstance(entry, dict):
            text = clean_text(entry.get("value"))
        else:
            text = clean_text(entry)
        if text:
            return text
    return ""


def reset_for_tests() -> None:
    """Clear per-process limiter state. Test-only."""
    global _rate_lock, _last_call_by_host
    _rate_lock = asyncio.Lock()
    _last_call_by_host = {}


__all__ = [
    "RATE_LIMIT_INTERVAL",
    "clean_text",
    "extmetadata_text",
    "headers",
    "rate_limit",
    "request_json",
    "reset_for_tests",
    "user_agent",
]
