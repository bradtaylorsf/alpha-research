"""Federal Register API connector (issue #102).

Public surface:

* ``async def search(query, *, since=None, agencies=None) -> list[SearchResult]``
  hits ``federalregister.gov/api/v1/documents.json`` with optional date /
  agency filters. Returns rules, proposed rules, and notices since 1994.
* ``async def fetch(url, timeout=30.0) -> Source | None`` opens a document
  page and returns markdown of the body text plus a significant-rule flag.

No auth required. The API has no documented rate limit, but we throttle to
≈2 RPS to stay polite. Pagination is capped at 2,000 results — the planner
should narrow via date range or agency rather than loop pages here.

Documents are immutable post-publication, so the API JSON for a given
document is cached at ``corpus/.cache/fedregister/doc-<doc_number>.json``.
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
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.federalregister.gov/api/v1/"
_SITE_BASE = "https://www.federalregister.gov"
# No documented rate limit; ~2 RPS keeps us well within polite usage.
_RATE_LIMIT_INTERVAL = 0.5
_CACHE_DIR = Path("corpus/.cache/fedregister")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Document URLs look like /documents/YYYY/MM/DD/<doc_number>/<slug>
_DOC_URL_RE = re.compile(
    r"/documents/\d{4}/\d{2}/\d{2}/(?P<doc>[A-Za-z0-9\-]+)(?:/|$)"
)

_SEARCH_FIELDS = (
    "document_number",
    "title",
    "abstract",
    "publication_date",
    "html_url",
    "document_type",
    "agencies",
    "significant",
)

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


# ---------------------------------------------------------------------------
# Helpers
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
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except ValueError:
            continue
    head = text.split("T", 1)[0]
    try:
        return datetime.strptime(head, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _coerce_since_to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    # Accept anything _parse_iso_date can chew on; emit YYYY-MM-DD.
    parsed = _parse_iso_date(text)
    if parsed is not None:
        return parsed.date().isoformat()
    return text


def _agency_names(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("raw_name")
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
        elif isinstance(entry, str) and entry.strip():
            out.append(entry.strip())
    return out


def _build_search_result(hit: dict[str, Any]) -> SearchResult | None:
    url = hit.get("html_url") or ""
    if not url:
        return None
    title = hit.get("title") or url
    abstract = (hit.get("abstract") or "").strip()
    snippet = abstract if abstract else str(title)
    published_at = _parse_iso_date(hit.get("publication_date"))

    extras: dict[str, Any] = {
        "agencies": _agency_names(hit.get("agencies")),
        "document_type": hit.get("document_type") or "",
        "document_number": hit.get("document_number") or "",
        "significant": bool(hit.get("significant")),
    }

    return SearchResult(
        url=url,
        title=str(title),
        snippet=snippet,
        published_at=published_at,
        source_kind="fedregister",
        extras=extras,
    )


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def search(
    query: str,
    *,
    since: date | datetime | str | None = None,
    agencies: list[str] | None = None,
    max_results: int = 20,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Run a Federal Register search and return up to ``max_results`` hits.

    ``since`` accepts a ``date``, ``datetime``, or ISO ``YYYY-MM-DD`` string and
    maps to ``conditions[publication_date][gte]``. ``agencies`` is a list of
    agency slugs (e.g. ``["environmental-protection-agency"]``) emitted as
    repeated ``conditions[agencies][]`` params.

    The endpoint paginates results at 2,000 max — narrow via date range when
    needed; this connector never loops pages on the user's behalf.

    Returns ``[]`` on transport / HTTP error / non-JSON body — connector
    failures must never crash the planner.
    """
    params: list[tuple[str, Any]] = [("conditions[term]", query)]

    iso_since = _coerce_since_to_iso(since)
    if iso_since:
        params.append(("conditions[publication_date][gte]", iso_since))

    if agencies:
        for slug in agencies:
            if isinstance(slug, str) and slug.strip():
                params.append(("conditions[agencies][]", slug.strip()))

    per_page = max(1, min(max_results, 200))
    params.append(("per_page", per_page))
    for field in _SEARCH_FIELDS:
        params.append(("fields[]", field))

    await _rate_limit_gate()

    url = urljoin(_BASE_URL, "documents.json")
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("fedregister search failed for %r: %s", query, exc)
        return []

    if response.status_code != 200:
        logger.warning(
            "fedregister search returned HTTP %s for %r",
            response.status_code,
            query,
        )
        return []

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning(
            "fedregister search returned non-JSON for %r: %s", query, exc
        )
        return []

    raw_hits = payload.get("results") or []
    out: list[SearchResult] = []
    for hit in raw_hits[:max_results]:
        if not isinstance(hit, dict):
            continue
        result = _build_search_result(hit)
        if result is not None:
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def _classify_url(url: str) -> str | None:
    """Return the document number when ``url`` is a Federal Register doc page."""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    # Strict host match so look-alikes like ``federalregister.gov.attacker.example``
    # don't pass — the Source.url would otherwise leak the attacker domain
    # downstream even though the body came from the official API.
    if host != "federalregister.gov" and not host.endswith(".federalregister.gov"):
        return None
    m = _DOC_URL_RE.search(parsed.path or "")
    if not m:
        return None
    return m.group("doc")


def _cache_path(doc_number: str) -> Path:
    safe = doc_number.replace("/", "_")
    return _CACHE_DIR / f"doc-{safe}.json"


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
    url: str, timeout: float
) -> tuple[int | None, dict[str, Any] | None]:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("fedregister fetch failed for %s: %s", url, exc)
        return None, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


def _document_body(payload: dict[str, Any]) -> str:
    body_html = payload.get("body_html")
    if isinstance(body_html, str) and body_html.strip():
        extracted = _extract_html_text(body_html)
        if extracted:
            return extracted
    abstract = payload.get("abstract")
    if isinstance(abstract, str) and abstract.strip():
        return abstract.strip()
    return ""


async def fetch(url: str, timeout: float = 30.0) -> Source | None:
    """Open a Federal Register document page and return a :class:`Source`.

    The URL is classified via the path ``/documents/YYYY/MM/DD/<doc>/<slug>``;
    any URL not matching that shape (or pointing outside federalregister.gov)
    returns ``None``. The doc detail JSON is cached at
    ``corpus/.cache/fedregister/doc-<doc_number>.json`` so repeated fetches
    skip the network entirely.

    Markdown body prefers ``body_html`` (extracted via trafilatura w/
    favor_recall=True) and falls back to ``abstract``. Returns ``None`` for
    transport / HTTP / parse failures.
    """
    if not url:
        return None
    doc_number = _classify_url(url)
    if not doc_number:
        return None

    cache = _cache_path(doc_number)
    payload = _load_cache(cache)
    if payload is None:
        await _rate_limit_gate()
        api_url = urljoin(_BASE_URL, f"documents/{doc_number}.json")
        status, payload = await _http_get_json(api_url, timeout)
        if status is None or status >= 400 or not isinstance(payload, dict):
            if status is not None and status >= 400:
                logger.warning(
                    "fedregister doc HTTP %s for %s", status, api_url
                )
            return None
        _write_cache(cache, payload)

    title = payload.get("title") or url
    body = _document_body(payload)
    if not body:
        return None

    publication_date = payload.get("publication_date") or ""
    document_type = payload.get("document_type") or ""
    agency_names = _agency_names(payload.get("agencies"))
    agencies_line = ", ".join(agency_names) if agency_names else "—"
    metadata_line = (
        f"_{document_type or 'Document'} · {publication_date or '?'} · "
        f"{agencies_line}_"
    )

    cleaned_text = f"# {title}\n\n{metadata_line}\n\n{body}".strip()

    metadata: dict[str, Any] = {
        "document_number": doc_number,
        "document_type": document_type,
        "publication_date": publication_date,
        "agencies": agency_names,
        "significant": bool(payload.get("significant")),
        "html_url": payload.get("html_url") or url,
        "pdf_url": payload.get("pdf_url") or "",
        "public_inspection_pdf_url": payload.get("public_inspection_pdf_url")
        or "",
    }

    return Source(
        url=url,
        title=str(title),
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="fedregister",
        metadata=metadata,
    )


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


__all__ = ["fetch", "reset_for_tests", "search"]
