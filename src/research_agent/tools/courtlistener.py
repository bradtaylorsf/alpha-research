"""CourtListener / RECAP connector (issue #93).

Public surface:

* ``async def search(query, *, kind="opinions", max_results=20) -> list[SearchResult]``
  hits the REST v3 ``search/`` endpoint for opinions, dockets (RECAP), or oral
  arguments. ``kind`` maps to the API's ``type`` code (``o``/``r``/``oa``).
* ``async def fetch(url, timeout=30.0) -> Source | None`` opens an opinion or
  docket page and returns markdown of the opinion text or docket entries.

CourtListener offers ~5,000 req/hr with an API token (free w/ signup) and
heavily rate-limits anonymous traffic — we treat the token as required and
fail loudly when absent. Per-host bucket gates calls to ≈1 RPS to stay polite.

Filings and opinions are immutable, so the API JSON for a given resource is
cached at ``corpus/.cache/courtlistener/<id>.json``.

For dockets where RECAP doesn't have the document, the entry is still rendered
with a ``(no RECAP document available — surface to synthesizer)`` line; we
never auto-trigger a paid PACER fetch on the user's behalf.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura

from research_agent import config
from research_agent.tools._errors import MissingCredentialError
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.courtlistener.com/api/rest/v3/"
_SITE_BASE = "https://www.courtlistener.com"
# 5000 req/hr ≈ 1.4 RPS; we throttle to 1 RPS to leave headroom.
_RATE_LIMIT_INTERVAL = 1.0
_CACHE_DIR = Path("corpus/.cache/courtlistener")

_KIND_TO_TYPE = {
    "opinions": "o",
    "dockets": "r",
    "oral_arguments": "oa",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_OPINION_ID_RE = re.compile(r"/opinion/(\d+)(?:/|$)")
_DOCKET_ID_RE = re.compile(r"/docket/(\d+)(?:/|$)")

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


# ---------------------------------------------------------------------------
# Config / rate-limit helpers
# ---------------------------------------------------------------------------


def _resolve_token() -> str:
    """Return the configured API token. Raise RuntimeError if missing.

    Anonymous CourtListener traffic is throttled to the point of unusability
    per the issue notes, so a missing token is a configuration error — fail
    loudly here rather than silently degrade to broken-by-default behavior.
    """
    token = config.get("COURTLISTENER_API_TOKEN") or ""
    if not token.strip():
        raise MissingCredentialError(
            "CourtListener requires an API token. Sign up at "
            "https://www.courtlistener.com/help/api/rest/ and set "
            "COURTLISTENER_API_TOKEN in your .env."
        )
    return token.strip()


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Token {_resolve_token()}",
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


def _parse_iso_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    # CourtListener returns dates in ISO-8601 (``YYYY-MM-DD`` or full ts).
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except ValueError:
            continue
    # Last-ditch: split on T and try just the date part.
    head = text.split("T", 1)[0]
    try:
        return datetime.strptime(head, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _best_citation(hit: dict[str, Any]) -> str:
    citations = hit.get("citation")
    if isinstance(citations, list):
        for c in citations:
            if isinstance(c, str) and c.strip():
                return c.strip()
    elif isinstance(citations, str) and citations.strip():
        return citations.strip()
    for key in ("lexisCite", "neutralCite", "lexis_cite", "neutral_cite"):
        value = hit.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _absolute_url(rel: str) -> str:
    if not rel:
        return ""
    if rel.startswith("http://") or rel.startswith("https://"):
        return rel
    return urljoin(_SITE_BASE, rel)


def _build_search_result(
    hit: dict[str, Any], *, kind: str
) -> SearchResult | None:
    rel = hit.get("absolute_url") or ""
    if not rel:
        return None
    url = _absolute_url(rel)

    title = (
        hit.get("caseName")
        or hit.get("case_name")
        or hit.get("caseNameShort")
        or hit.get("case_name_short")
        or url
    )

    snippet_raw = hit.get("snippet") or ""
    if not snippet_raw:
        text = hit.get("text") or ""
        if isinstance(text, str):
            snippet_raw = text[:300]
    if not snippet_raw:
        snippet_raw = title

    snippet = _strip_html(str(snippet_raw))

    published_at = _parse_iso_date(
        hit.get("dateFiled") or hit.get("date_filed")
    )

    extras: dict[str, Any] = {
        "court": hit.get("court") or hit.get("court_id") or "",
        "citation": _best_citation(hit),
        "docket_number": hit.get("docketNumber") or hit.get("docket_number") or "",
        "kind": kind,
    }

    return SearchResult(
        url=url,
        title=str(title),
        snippet=snippet,
        published_at=published_at,
        source_kind="courtlistener",
        extras=extras,
    )


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def search(
    query: str,
    *,
    kind: str = "opinions",
    max_results: int = 20,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Run a CourtListener REST search and return up to ``max_results`` hits.

    ``kind`` selects the index — ``opinions`` (case law), ``dockets`` (RECAP
    PACER mirror), or ``oral_arguments``. Returns ``[]`` on transport / HTTP
    error / non-JSON body — connector failures must never crash the planner.
    """
    if kind not in _KIND_TO_TYPE:
        raise ValueError(
            f"unknown kind {kind!r}; expected one of {sorted(_KIND_TO_TYPE)}"
        )

    params = {"q": query, "type": _KIND_TO_TYPE[kind]}
    headers = _auth_headers()

    await _rate_limit_gate()

    url = urljoin(_BASE_URL, "search/")
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:
            response = await client.get(url, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("courtlistener search failed for %r: %s", query, exc)
        return []

    if response.status_code != 200:
        logger.warning(
            "courtlistener search returned HTTP %s for %r",
            response.status_code,
            query,
        )
        return []

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning(
            "courtlistener search returned non-JSON for %r: %s", query, exc
        )
        return []

    raw_hits = payload.get("results") or []
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


def _classify_url(url: str) -> tuple[str | None, str | None]:
    """Return ``(resource, id)`` where resource ∈ {"opinion","docket"}.

    Returns ``(None, None)`` when the URL doesn't point at a recognised
    CourtListener resource page.
    """
    parsed = urlparse(url)
    if "courtlistener.com" not in (parsed.netloc or "").lower():
        return None, None
    path = parsed.path or ""
    m = _OPINION_ID_RE.search(path)
    if m:
        return "opinion", m.group(1)
    m = _DOCKET_ID_RE.search(path)
    if m:
        return "docket", m.group(1)
    return None, None


def _cache_path(prefix: str, resource_id: str) -> Path:
    safe = resource_id.replace("/", "_")
    return _CACHE_DIR / f"{prefix}-{safe}.json"


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def _load_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _http_get_json(
    url: str, timeout: float, *, params: dict[str, Any] | None = None
) -> tuple[int | None, dict[str, Any] | None]:
    headers = _auth_headers()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:
            response = await client.get(url, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("courtlistener fetch failed for %s: %s", url, exc)
        return None, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


def _opinion_text(payload: dict[str, Any]) -> str:
    plain = payload.get("plain_text")
    if isinstance(plain, str) and plain.strip():
        return plain.strip()
    for key in ("html_with_citations", "html", "html_lawbox"):
        html = payload.get(key)
        if isinstance(html, str) and html.strip():
            extracted = _extract_html_text(html)
            if extracted:
                return extracted
    return ""


def _has_recap_doc(entry: dict[str, Any]) -> bool:
    docs = entry.get("recap_documents")
    if not isinstance(docs, list):
        return False
    for doc in docs:
        if isinstance(doc, dict):
            # Either a populated filepath_local or a is_available flag indicates
            # RECAP has the actual document on file.
            if doc.get("filepath_local") or doc.get("is_available"):
                return True
    return False


def _render_docket_entries(entries: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        number = entry.get("entry_number")
        date_filed = entry.get("date_filed") or ""
        description = (entry.get("description") or "").strip()
        header = f"## Entry {number if number is not None else '?'} — {date_filed}"
        lines.append(header)
        if description:
            lines.append(description)
        if not _has_recap_doc(entry):
            lines.append(
                "(no RECAP document available — surface to synthesizer)"
            )
        lines.append("")
    return "\n".join(lines).strip()


async def _fetch_opinion(
    opinion_id: str, source_url: str, timeout: float
) -> Source | None:
    cache = _cache_path("opinion", opinion_id)
    payload = _load_cache(cache)
    if payload is None:
        await _rate_limit_gate()
        api_url = urljoin(_BASE_URL, f"opinions/{opinion_id}/")
        status, payload = await _http_get_json(api_url, timeout)
        if status is None or status >= 400 or not isinstance(payload, dict):
            if status is not None and status >= 400:
                logger.warning(
                    "courtlistener opinion HTTP %s for %s", status, api_url
                )
            return None
        _write_cache(cache, payload)

    cleaned_text = _opinion_text(payload)
    if not cleaned_text:
        return None

    title = (
        payload.get("case_name")
        or payload.get("caseName")
        or payload.get("cluster_id")
        or source_url
    )

    metadata: dict[str, Any] = {
        "court": payload.get("court") or payload.get("court_id") or "",
        "docket_number": payload.get("docket_number") or "",
        "citation": payload.get("citation") or "",
        "date_filed": payload.get("date_filed") or "",
        "recap_available": False,
        "opinion_id": opinion_id,
    }

    return Source(
        url=source_url,
        title=str(title),
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="courtlistener",
        metadata=metadata,
    )


async def _fetch_docket(
    docket_id: str, source_url: str, timeout: float
) -> Source | None:
    docket_cache = _cache_path("docket", docket_id)
    docket_payload = _load_cache(docket_cache)
    if docket_payload is None:
        await _rate_limit_gate()
        api_url = urljoin(_BASE_URL, f"dockets/{docket_id}/")
        status, docket_payload = await _http_get_json(api_url, timeout)
        if (
            status is None
            or status >= 400
            or not isinstance(docket_payload, dict)
        ):
            if status is not None and status >= 400:
                logger.warning(
                    "courtlistener docket HTTP %s for %s", status, api_url
                )
            return None
        _write_cache(docket_cache, docket_payload)

    entries_cache = _cache_path("docket-entries", docket_id)
    entries_payload = _load_cache(entries_cache)
    if entries_payload is None:
        await _rate_limit_gate()
        entries_url = urljoin(_BASE_URL, "docket-entries/")
        status, entries_payload = await _http_get_json(
            entries_url,
            timeout,
            params={"docket": docket_id, "order_by": "entry_number"},
        )
        if (
            status is None
            or status >= 400
            or not isinstance(entries_payload, dict)
        ):
            entries_payload = {"results": []}
        else:
            _write_cache(entries_cache, entries_payload)

    raw_entries = entries_payload.get("results") or []
    # Keep payload bounded — first page / ~100 entries is plenty for synth.
    entries = [e for e in raw_entries[:100] if isinstance(e, dict)]
    body = _render_docket_entries(entries)

    case_name = (
        docket_payload.get("case_name")
        or docket_payload.get("caseName")
        or source_url
    )
    docket_number = docket_payload.get("docket_number") or ""

    header_parts = [str(case_name)]
    if docket_number:
        header_parts.append(f"Docket No. {docket_number}")
    header = " — ".join(header_parts)
    cleaned_text = f"# {header}\n\n{body}".strip() if body else f"# {header}".strip()

    if not cleaned_text:
        return None

    recap_available = any(_has_recap_doc(e) for e in entries)

    metadata: dict[str, Any] = {
        "court": docket_payload.get("court") or docket_payload.get("court_id") or "",
        "docket_number": docket_number,
        "citation": "",
        "date_filed": docket_payload.get("date_filed") or "",
        "recap_available": recap_available,
        "docket_id": docket_id,
        "entry_count": len(entries),
    }

    return Source(
        url=source_url,
        title=str(case_name),
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="courtlistener",
        metadata=metadata,
    )


async def fetch(url: str, timeout: float = 30.0) -> Source | None:
    """Open a CourtListener opinion or docket page and return a :class:`Source`.

    The URL is classified by path: ``/opinion/<id>/...`` routes through the
    ``opinions/<id>/`` endpoint and prefers ``plain_text`` over the various
    HTML fields. ``/docket/<id>/...`` fetches the docket plus its first page
    of entries via ``docket-entries/?docket=<id>&order_by=entry_number``.

    Returns ``None`` for unrecognised URLs and for any transport / HTTP /
    parse failure.
    """
    if not url:
        return None
    resource, resource_id = _classify_url(url)
    if not resource or not resource_id:
        return None

    if resource == "opinion":
        return await _fetch_opinion(resource_id, url, timeout)
    if resource == "docket":
        return await _fetch_docket(resource_id, url, timeout)
    return None


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


__all__ = ["fetch", "reset_for_tests", "search"]
