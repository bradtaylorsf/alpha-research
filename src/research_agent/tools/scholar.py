"""Google Scholar connector via SERPAPI (issue #114).

Public surface:

* ``async def search(query, *, kind="case_law", max_results=20)`` — hits
  SERPAPI's Google Scholar engine. ``kind`` selects ``case_law`` (court
  opinions, ``as_sdt=2006``) vs ``articles`` (papers).
* ``async def fetch(url, timeout=30.0)`` — opens a Scholar result. PDFs are
  routed through :mod:`research_agent.tools.pdf`; HTML pages are extracted
  with trafilatura. Returns a :class:`Source` with ``source_kind="scholar"``.

SERPAPI bills per call (per-query ≈ $0.015 against the 5k/mo $75 plan), so
the operator sees the spend trajectory via ``research doctor`` before
launching a goal. ``SERPAPI_KEY`` is required and the connector raises
``RuntimeError`` with a signup pointer when missing.

A polite 1 RPS per-process gate sits in front of every call — SERPAPI itself
doesn't throttle on the per-query axis but burning through the monthly
quota in a tight loop is the same outcome with extra steps.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
import trafilatura

from research_agent import config
from research_agent.tools import pdf as pdf_tool
from research_agent.tools._errors import MissingCredentialError
from research_agent.tools._registry import (
    BaseSearchPayload as _BaseSearchPayload,
    register_kind as _register_kind,
)
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_BASE_URL = "https://serpapi.com/search.json"
# Only ``case_law`` adds ``as_sdt`` — articles search omits it.
_KIND_TO_SDT: dict[str, str] = {"case_law": "2006"}
_VALID_KINDS = frozenset({"case_law", "articles"})

_RATE_LIMIT_INTERVAL = 1.0

# Per-query cost on SERPAPI's $75/mo / 5k-search plan. Surface via doctor.py
# so an operator sees the spend trajectory before launching a goal.
_SCHOLAR_COST_USD = 0.015

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


# ---------------------------------------------------------------------------
# Config / rate-limit helpers
# ---------------------------------------------------------------------------


def _resolve_key() -> str:
    """Return the configured SERPAPI key. Raise ``RuntimeError`` if missing.

    SERPAPI is the only path to Google Scholar in this connector — a missing
    key is a configuration error, not something to silently degrade.
    """
    key = config.get("SERPAPI_KEY") or ""
    if not key.strip():
        raise MissingCredentialError(
            "Google Scholar connector requires a SERPAPI key. Sign up at "
            "https://serpapi.com/ and set SERPAPI_KEY in your .env."
        )
    return key.strip()


def _user_agent() -> str:
    return config.get("RESEARCH_USER_AGENT") or (
        "research-agent/0.1 (+local; contact unset)"
    )


async def _rate_limit_gate() -> None:
    """Block until ``_RATE_LIMIT_INTERVAL`` seconds have elapsed since last call."""
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


def _strip_html(html: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()


def _extract_html_text(html: str) -> str:
    if not html:
        return ""
    try:
        extracted = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        )
    except Exception:  # noqa: BLE001 — never crash on extractor errors
        extracted = None
    if extracted and extracted.strip():
        return extracted.strip()
    return _strip_html(html)


def _parse_year(value: Any) -> datetime | None:
    """Pull a 4-digit year out of ``value`` and return Jan 1 of that year UTC.

    SERPAPI's ``publication_info.summary`` looks like
    ``"Supreme Court, 2018 - scholar.google.com"`` for case law and
    ``"J Smith, A Jones - Nature, 2021"`` for articles — both expose the
    year, but the leading authors / court / journal vary, so a regex on the
    whole string is the path of least resistance.
    """
    if not value:
        return None
    match = _YEAR_RE.search(str(value))
    if not match:
        return None
    return datetime(int(match.group(1)), 1, 1, tzinfo=UTC)


def _extract_title_from_html(html: str) -> str:
    """Best-effort title extraction: ``og:title`` then ``<title>``."""
    if not html:
        return ""
    og = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if og:
        return _strip_html(og.group(1))
    title = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if title:
        return _strip_html(title.group(1))
    return ""


def _build_search_result(
    item: dict[str, Any], *, kind: str
) -> SearchResult | None:
    url = item.get("link") or ""
    if not isinstance(url, str) or not url:
        return None

    title = item.get("title") or url
    snippet_raw = item.get("snippet") or ""
    snippet = _strip_html(str(snippet_raw))

    pub_info = item.get("publication_info") or {}
    summary = ""
    if isinstance(pub_info, dict):
        summary = str(pub_info.get("summary") or "")

    published_at = _parse_year(summary)

    inline_links = item.get("inline_links") or {}
    cited_by_total: int = 0
    if isinstance(inline_links, dict):
        cited_by = inline_links.get("cited_by")
        if isinstance(cited_by, dict):
            total = cited_by.get("total")
            if isinstance(total, int):
                cited_by_total = total
            elif isinstance(total, str) and total.isdigit():
                cited_by_total = int(total)

    raw_resources = item.get("resources") or []
    resources: list[dict[str, Any]] = []
    if isinstance(raw_resources, list):
        for r in raw_resources:
            if not isinstance(r, dict):
                continue
            resources.append(
                {
                    "title": r.get("title") or "",
                    "link": r.get("link") or "",
                    "file_format": r.get("file_format") or "",
                }
            )

    extras: dict[str, Any] = {
        "kind": kind,
        "court_or_journal": summary,
        "citation": cited_by_total,
        "result_id": item.get("result_id") or "",
        "resources": resources,
    }

    return SearchResult(
        url=url,
        title=str(title),
        snippet=snippet,
        published_at=published_at,
        source_kind="scholar",
        extras=extras,
    )


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def search(
    query: str,
    *,
    kind: str = "case_law",
    max_results: int = 20,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Run a Google Scholar search via SERPAPI and return up to ``max_results`` hits.

    ``kind="case_law"`` adds ``as_sdt=2006`` to scope to court opinions;
    ``kind="articles"`` is the unfiltered Scholar search. Returns ``[]`` on
    transport / HTTP / JSON errors — connector failures must never crash the
    planner.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(
            f"unknown kind {kind!r}; expected one of {sorted(_VALID_KINDS)}"
        )

    api_key = _resolve_key()
    params: dict[str, str] = {
        "engine": "google_scholar",
        "q": query,
        "api_key": api_key,
        # SERPAPI's google_scholar engine maxes out at 20 results per call.
        "num": str(min(max(max_results, 1), 20)),
    }
    sdt = _KIND_TO_SDT.get(kind)
    if sdt is not None:
        params["as_sdt"] = sdt

    headers = {
        "Accept": "application/json",
        "User-Agent": _user_agent(),
    }

    await _rate_limit_gate()

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:
            response = await client.get(_BASE_URL, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("scholar search failed for %r: %s", query, exc)
        return []

    if response.status_code != 200:
        logger.warning(
            "scholar search returned HTTP %s for %r",
            response.status_code,
            query,
        )
        return []

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("scholar search returned non-JSON for %r: %s", query, exc)
        return []

    raw_hits = payload.get("organic_results") or []
    out: list[SearchResult] = []
    for hit in raw_hits[:max_results]:
        if not isinstance(hit, dict):
            continue
        result = _build_search_result(hit, kind=kind)
        if result is not None:
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def _looks_like_pdf(url: str, content_type: str | None) -> bool:
    if content_type and "application/pdf" in content_type.lower():
        return True
    parsed = urlparse(url)
    return parsed.path.lower().endswith(".pdf")


async def _probe_content_type(
    client: httpx.AsyncClient, url: str
) -> str | None:
    """HEAD ``url`` to read ``Content-Type``. Falls back to a small GET on 405/403.

    Some hosts (notably JSTOR mirrors) reject HEAD outright — when the response
    isn't usable we let the caller fall through to a normal GET below.
    """
    try:
        head = await client.head(url)
    except (httpx.HTTPError, OSError) as exc:
        logger.debug("scholar HEAD failed for %s: %s", url, exc)
        return None
    if head.status_code in (403, 405, 501):
        return None
    if head.status_code >= 400:
        return None
    return head.headers.get("content-type")


async def fetch(url: str, timeout: float = 30.0) -> Source | None:
    """Open a Google Scholar result and return a :class:`Source`.

    PDF results route through :func:`research_agent.tools.pdf.extract` (which
    handles the layered text → tables → OCR → VLM pipeline) so we don't
    duplicate caching / OCR logic here. HTML results go through trafilatura.

    Returns ``None`` on any transport / HTTP / parse failure.
    """
    if not url:
        return None

    headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/pdf;q=0.8,*/*;q=0.5"
        ),
        "User-Agent": _user_agent(),
    }

    await _rate_limit_gate()

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:
            content_type = await _probe_content_type(client, url)
            if _looks_like_pdf(url, content_type):
                cleaned_text = await pdf_tool.extract(url)
                if not cleaned_text:
                    return None
                return Source(
                    url=url,
                    title=url.rsplit("/", 1)[-1] or url,
                    cleaned_text=cleaned_text,
                    raw_html=None,
                    fetched_at=datetime.now(UTC),
                    source_kind="scholar",
                    metadata={"content_type": "application/pdf"},
                )

            response = await client.get(url)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("scholar fetch failed for %s: %s", url, exc)
        return None

    if response.status_code >= 400:
        logger.warning(
            "scholar fetch returned HTTP %s for %s", response.status_code, url
        )
        return None

    response_ct = response.headers.get("content-type") or ""
    if _looks_like_pdf(url, response_ct):
        # Server didn't expose Content-Type on HEAD but the GET body is a PDF;
        # hand the raw bytes to the pdf module so we don't re-download.
        cleaned_text = pdf_tool.extract_from_bytes(
            response.content, source_label=url
        )
        if not cleaned_text:
            return None
        return Source(
            url=url,
            title=url.rsplit("/", 1)[-1] or url,
            cleaned_text=cleaned_text,
            raw_html=None,
            fetched_at=datetime.now(UTC),
            source_kind="scholar",
            metadata={"content_type": "application/pdf"},
        )

    html = response.text or ""
    cleaned_text = _extract_html_text(html)
    if not cleaned_text:
        return None

    title = _extract_title_from_html(html) or url

    return Source(
        url=url,
        title=title,
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="scholar",
        metadata={"content_type": response_ct},
    )


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


KIND = "scholar_search"


class _PayloadSchema(_BaseSearchPayload):
    kind: str | None = None
    max_results: int | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=("scholar.google.com",),
    skill_name=None,
    description="Google Scholar via SerpAPI — requires `SERPAPI_KEY`",
    optional_payload_knobs="`kind: case_law\\|articles`",
    example_query="Section 230 appellate",
    module_name="scholar",
)


__all__ = ["KIND", "fetch", "reset_for_tests", "search"]
