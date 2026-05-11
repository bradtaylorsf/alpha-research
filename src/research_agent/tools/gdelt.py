"""GDELT 2.0 DOC API connector (issue #105).

Public surface:

* ``async def search(query, *, since=None, language="english", max_results=20) -> list[SearchResult]``
  hits the DOC API ``ArtList`` mode for global news / broadcast TV transcripts.
* ``async def fetch(url) -> Source | None`` — no-op delegate to ``web_fetch.fetch``.
  GDELT is purely an index; article bodies live on the source's own domain.
* ``async def tone_timeline(query, *, since=None, language="english") -> list[dict]``
  returns a sentiment-over-time series via the ``TimelineTone`` mode. This is
  GDELT's distinctive feature — mention velocity + tone deltas surface
  coverage waves (and anomalies) before any single outlet's RSS does.

Auth: none required. The DOC API is public and free.

Refresh cadence: 15 minutes — GDELT 2.0 reindexes the global news web roughly
every 15 minutes, which is the killer feature for ambient anomaly detection
relative to outlet-specific RSS. Coverage spans the open web *and* broadcast
TV transcripts (the GDELT Global Knowledge Graph ingests closed-caption
streams from CNN, MSNBC, BBC News, etc.).

Per AC, the per-host gate is 1.0s (1 RPS) to be polite to the public endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from research_agent import config
from research_agent.tools._registry import (
    BaseSearchPayload as _BaseSearchPayload,
    register_kind as _register_kind,
)
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
# AC: per-host 1 RPS.
_RATE_LIMIT_INTERVAL = 1.0
_DEFAULT_TIMESPAN = "1d"

# GDELT expects 3-letter ISO 639-2/B codes via the ``sourcelang:<code>``
# query token. A small map covers the common cases callers reach for; any
# value not in the map is passed through verbatim so callers can opt into
# the full ISO-3 set without an API surface change.
_LANGUAGE_MAP: dict[str, str] = {
    "english": "eng",
    "spanish": "spa",
    "french": "fra",
    "german": "deu",
    "italian": "ita",
    "portuguese": "por",
    "russian": "rus",
    "chinese": "zho",
    "japanese": "jpn",
    "arabic": "ara",
}

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


# ---------------------------------------------------------------------------
# Config / rate-limit helpers
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": config.get("RESEARCH_USER_AGENT")
        or "research-agent/0.1 (+local; contact unset)",
    }


async def _rate_limit_gate() -> None:
    """Block until at least ``_RATE_LIMIT_INTERVAL`` has passed since the last call."""
    global _last_call_monotonic
    async with _rate_lock:
        if _last_call_monotonic is not None:
            elapsed = time.monotonic() - _last_call_monotonic
            wait = _RATE_LIMIT_INTERVAL - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
        _last_call_monotonic = time.monotonic()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_timespan(since: datetime | None) -> str | None:
    """Convert a tz-aware datetime to GDELT's ``timespan`` token.

    GDELT accepts forms like ``15min``, ``1h``, ``1d``, ``2w``, ``1m``. We map
    ``now - since`` to the smallest unit that still fits cleanly: minutes
    under an hour, hours under a day, days otherwise. ``None`` returns
    ``None`` so callers can omit the param and let GDELT apply its default.
    """
    if since is None:
        return None
    now = datetime.now(UTC)
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    delta = now - since
    if delta <= timedelta(0):
        return "15min"
    seconds = int(delta.total_seconds())
    if seconds < 3600:
        minutes = max(15, seconds // 60)
        return f"{minutes}min"
    if seconds < 86400:
        return f"{max(1, seconds // 3600)}h"
    return f"{max(1, seconds // 86400)}d"


def _parse_gdelt_dt(seendate: Any) -> datetime | None:
    """Parse GDELT's ``YYYYMMDDTHHMMSSZ`` timestamp format."""
    if not seendate:
        return None
    text = str(seendate).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _language_token(language: str | None) -> str | None:
    if not language:
        return None
    key = language.strip().lower()
    if not key:
        return None
    return _LANGUAGE_MAP.get(key, key)


def _build_query(query: str, language: str | None) -> str:
    token = _language_token(language)
    if token:
        return f"{query} sourcelang:{token}"
    return query


# ---------------------------------------------------------------------------
# GET helper
# ---------------------------------------------------------------------------


async def _get(
    params: dict[str, Any], timeout: float
) -> tuple[int | None, dict[str, Any] | None]:
    """GET ``_BASE_URL`` with ``params`` after the rate gate.

    Returns ``(status_code, payload)``. GDELT occasionally responds with an
    HTML error page (rate-limited bot detection, malformed query) — those
    surface as ``(200, None)`` after the JSON parse fails, not as a raise.
    """
    await _rate_limit_gate()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(_BASE_URL, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("gdelt GET failed: %s", exc)
        return None, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        return response.status_code, response.json()
    except ValueError as exc:
        logger.warning("gdelt GET returned non-JSON body: %s", exc)
        return response.status_code, None


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def search(
    query: str,
    *,
    since: datetime | None = None,
    language: str = "english",
    max_results: int = 20,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Run a GDELT DOC ``ArtList`` query and return up to ``max_results`` hits.

    ``since`` narrows the search window. When ``None``, GDELT applies its
    default (typically the last few days). ``language`` is mapped to a
    ``sourcelang:<iso3>`` token in the query — pass an unknown value to opt
    into the raw ISO-3 code without changing the connector.

    Returns ``[]`` on transport / HTTP error / non-JSON body — connector
    failures must never crash the planner.
    """
    params: dict[str, Any] = {
        "query": _build_query(query, language),
        "mode": "ArtList",
        "format": "json",
        "maxrecords": max_results,
        "sort": "datedesc",
    }
    timespan = _to_timespan(since)
    if timespan is not None:
        params["timespan"] = timespan

    status, payload = await _get(params, timeout)
    if status is None or status != 200 or not isinstance(payload, dict):
        if status is not None and status != 200:
            logger.warning("gdelt search returned HTTP %s for %r", status, query)
        return []

    raw = payload.get("articles") or []
    out: list[SearchResult] = []
    for art in raw[:max_results]:
        if not isinstance(art, dict):
            continue
        url = (art.get("url") or "").strip()
        title = (art.get("title") or "").strip()
        if not url or not title:
            continue
        seendate = art.get("seendate") or ""
        domain = (art.get("domain") or "").strip()
        snippet_bits = [str(b) for b in (seendate, domain) if b]
        snippet = " — ".join(snippet_bits)
        out.append(
            SearchResult(
                url=url,
                title=title,
                snippet=snippet,
                published_at=_parse_gdelt_dt(seendate),
                source_kind="gdelt",
                extras={
                    "domain": domain,
                    "language": (art.get("language") or "").strip() or None,
                    "sourcecountry": (art.get("sourcecountry") or "").strip() or None,
                    "socialimage": (art.get("socialimage") or "").strip() or None,
                    "seendate": seendate,
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def fetch(url: str) -> Source | None:
    """Delegate to :func:`web_fetch.fetch`.

    GDELT only indexes article URLs — bodies live on the source's own
    domain — so the connector's ``fetch`` is a thin pass-through. The
    central ``web_fetch`` pipeline handles robots/UA/Playwright fallback
    and Wayback archiving uniformly with the rest of the agent's web layer.
    """
    if not url:
        return None
    from research_agent.tools import web_fetch

    return await web_fetch.fetch(url)


# ---------------------------------------------------------------------------
# tone_timeline()
# ---------------------------------------------------------------------------


async def tone_timeline(
    query: str,
    *,
    since: datetime | None = None,
    language: str = "english",
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """Return a GDELT ``TimelineTone`` series as ``[{datetime, value}, ...]``.

    Tone is GDELT's average sentiment score across the matching mention
    population, sampled at GDELT's native cadence. Mention-velocity spikes
    and tone deltas together are what make this useful for anomaly
    detection — outlet RSS only catches a wave once a single source picks
    it up; the tone timeline reflects the *aggregate* shift.

    Returns ``[]`` on transport / HTTP error / non-JSON body / unexpected
    payload shape.
    """
    params: dict[str, Any] = {
        "query": _build_query(query, language),
        "mode": "TimelineTone",
        "format": "json",
    }
    timespan = _to_timespan(since)
    if timespan is not None:
        params["timespan"] = timespan

    status, payload = await _get(params, timeout)
    if status is None or status != 200 or not isinstance(payload, dict):
        if status is not None and status != 200:
            logger.warning("gdelt tone_timeline HTTP %s for %r", status, query)
        return []

    timeline = payload.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        return []
    first = timeline[0]
    if not isinstance(first, dict):
        return []
    data = first.get("data")
    if not isinstance(data, list):
        return []

    out: list[dict[str, Any]] = []
    for point in data:
        if not isinstance(point, dict):
            continue
        dt = _parse_gdelt_dt(point.get("date"))
        raw_val = point.get("value")
        if dt is None or raw_val is None:
            continue
        try:
            value = float(raw_val)
        except (TypeError, ValueError):
            continue
        out.append({"datetime": dt, "value": value})
    return out


# ---------------------------------------------------------------------------
# Test hooks
# ---------------------------------------------------------------------------


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


KIND = "gdelt_search"


class _PayloadSchema(_BaseSearchPayload):
    since: str | None = None
    language: str | None = None
    max_results: int | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=("api.gdeltproject.org",),
    skill_name=None,
    description=(
        "GDELT — Global news event aggregator, no `site:` operator (no auth)"
    ),
    optional_payload_knobs="`since: YYYY-MM-DD`, `language: english`",
    example_query="Project 2025 mainstream coverage",
    module_name="gdelt",
)


__all__ = ["KIND", "fetch", "reset_for_tests", "search", "tone_timeline"]
