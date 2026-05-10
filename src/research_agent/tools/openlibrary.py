"""Open Library connector (issue #236, A14).

Public surface:

* ``async def search(query, *, max_results=20) -> list[SearchResult]`` hits
  ``openlibrary.org/search.json`` with a focused ``fields=`` list so book
  metadata searches do not request the endpoint's expensive full payload.
* ``async def fetch(url) -> Source | None`` resolves Open Library work/book
  permalinks back through the JSON search API and returns bibliographic
  metadata plus Internet Archive scan identifiers.

No auth required. Polite per-host rate of 3 RPS for identified User-Agents.
The User-Agent header uses ``RESEARCH_USER_AGENT`` when configured.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, unquote, urlparse

import httpx

from research_agent import config
from research_agent.tools._registry import (
    BaseSearchPayload as _BaseSearchPayload,
)
from research_agent.tools._registry import (
    register_kind as _register_kind,
)
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

KIND: Literal["openlibrary_search"] = "openlibrary_search"

_SEARCH_URL = "https://openlibrary.org/search.json"
_SITE_BASE = "https://openlibrary.org"
_HOSTS = frozenset({"openlibrary.org", "www.openlibrary.org"})
_RATE_LIMIT_INTERVAL = 1.0 / 3.0

_SEARCH_FIELDS: tuple[str, ...] = (
    "key",
    "title",
    "subtitle",
    "author_name",
    "author_key",
    "first_publish_year",
    "edition_count",
    "isbn",
    "oclc",
    "lccn",
    "ia",
    "has_fulltext",
    "public_scan_b",
    "publisher",
    "publish_year",
    "language",
    "edition_key",
    "cover_edition_key",
)
SEARCH_FIELDS = ",".join(_SEARCH_FIELDS)

_WORK_OR_BOOK_RE = re.compile(
    r"^/(?P<collection>works|books)/(?P<olid>OL\d+[WM])(?:/.*)?$",
    re.IGNORECASE,
)
_IDENTIFIER_RE = re.compile(
    r"^/(?P<kind>isbn|oclc|lccn)/(?P<identifier>[^/]+)/?$",
    re.IGNORECASE,
)

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": config.get("RESEARCH_USER_AGENT")
        or "research-agent/0.1 (+local; contact unset)",
    }


async def _rate_limit_gate() -> None:
    """Block until the next 3 RPS request slot is available."""
    global _last_call_monotonic
    async with _rate_lock:
        if _last_call_monotonic is not None:
            elapsed = time.monotonic() - _last_call_monotonic
            wait = _RATE_LIMIT_INTERVAL - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
        _last_call_monotonic = time.monotonic()


async def _request_search(
    params: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any] | None:
    await _rate_limit_gate()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(_SEARCH_URL, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("openlibrary request failed for params=%s: %s", params, exc)
        return None

    if response.status_code != 200:
        logger.warning(
            "openlibrary request returned HTTP %s for params=%s",
            response.status_code,
            params,
        )
        return None

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("openlibrary request returned non-JSON: %s", exc)
        return None
    if not isinstance(payload, dict):
        logger.warning("openlibrary request returned %s JSON", type(payload).__name__)
        return None
    return payload


def _as_str_list(value: Any) -> list[str]:
    raw_values = value if isinstance(value, list) else [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        if item is None:
            continue
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _year_datetime(value: Any) -> datetime | None:
    year = _as_int(value)
    if year is None or year < 1:
        return None
    try:
        return datetime(year, 1, 1, tzinfo=UTC)
    except ValueError:
        return None


def _openlibrary_path(doc: dict[str, Any]) -> str | None:
    raw_key = str(doc.get("key") or "").strip()
    if not raw_key:
        return None
    if raw_key.startswith("/works/") or raw_key.startswith("/books/"):
        return raw_key
    if re.fullmatch(r"OL\d+W", raw_key, flags=re.IGNORECASE):
        return f"/works/{raw_key.upper()}"
    if re.fullmatch(r"OL\d+M", raw_key, flags=re.IGNORECASE):
        return f"/books/{raw_key.upper()}"
    return None


def _openlibrary_url(doc: dict[str, Any]) -> str | None:
    path = _openlibrary_path(doc)
    if path is None:
        return None
    return f"{_SITE_BASE}{path}"


def _metadata_from_doc(doc: dict[str, Any]) -> dict[str, Any]:
    edition_count = _as_int(doc.get("edition_count"))
    return {
        "openlibrary_key": _openlibrary_path(doc) or "",
        "isbn": _as_str_list(doc.get("isbn")),
        "oclc": _as_str_list(doc.get("oclc")),
        "lccn": _as_str_list(doc.get("lccn")),
        "ia_scan_id": _as_str_list(doc.get("ia")),
        "edition_count": edition_count if edition_count is not None else 0,
        "author_keys": _as_str_list(doc.get("author_key")),
        "authors": _as_str_list(doc.get("author_name")),
        "first_publish_year": _as_int(doc.get("first_publish_year")),
        "has_fulltext": bool(doc.get("has_fulltext")),
        "public_scan": bool(doc.get("public_scan_b")),
        "edition_keys": _as_str_list(doc.get("edition_key")),
        "cover_edition_key": str(doc.get("cover_edition_key") or "").strip(),
        "publisher": _as_str_list(doc.get("publisher")),
        "publish_year": [
            year
            for year in (_as_int(v) for v in _as_str_list(doc.get("publish_year")))
            if year is not None
        ],
        "language": _as_str_list(doc.get("language")),
        "fetched_via": KIND,
    }


def _snippet_from_doc(doc: dict[str, Any]) -> str:
    metadata = _metadata_from_doc(doc)
    parts: list[str] = []
    authors = metadata["authors"]
    if authors:
        parts.append("Authors: " + ", ".join(authors[:3]))
    first_year = metadata["first_publish_year"]
    if first_year:
        parts.append(f"First published: {first_year}")
    edition_count = metadata["edition_count"]
    if edition_count:
        parts.append(f"Editions: {edition_count}")
    if metadata["ia_scan_id"]:
        parts.append("IA scans: " + ", ".join(metadata["ia_scan_id"][:3]))
    identifiers: list[str] = []
    if metadata["oclc"]:
        identifiers.append("OCLC " + ", ".join(metadata["oclc"][:2]))
    if metadata["lccn"]:
        identifiers.append("LCCN " + ", ".join(metadata["lccn"][:2]))
    if metadata["isbn"]:
        identifiers.append("ISBN " + ", ".join(metadata["isbn"][:2]))
    if identifiers:
        parts.append("; ".join(identifiers))
    if metadata["has_fulltext"] or metadata["public_scan"]:
        parts.append("full text available")

    title = str(doc.get("title") or "").strip()
    subtitle = str(doc.get("subtitle") or "").strip()
    heading = f"{title}: {subtitle}" if title and subtitle else title
    return " | ".join(parts) or heading


def _search_result_from_doc(doc: dict[str, Any]) -> SearchResult | None:
    title = str(doc.get("title") or "").strip()
    if not title:
        return None
    url = _openlibrary_url(doc)
    if url is None:
        return None
    metadata = _metadata_from_doc(doc)
    return SearchResult(
        url=url,
        title=title,
        snippet=_snippet_from_doc(doc),
        published_at=_year_datetime(doc.get("first_publish_year")),
        source_kind=KIND,
        extras=metadata,
    )


def _source_markdown(title: str, metadata: dict[str, Any]) -> str:
    lines = [f"# {title}", ""]
    key = metadata.get("openlibrary_key") or ""
    if key:
        lines.append(f"- Open Library key: {key}")
    authors = metadata.get("authors") or []
    if authors:
        lines.append(f"- Authors: {', '.join(authors)}")
    if metadata.get("author_keys"):
        lines.append(f"- Author keys: {', '.join(metadata['author_keys'])}")
    if metadata.get("first_publish_year"):
        lines.append(f"- First publish year: {metadata['first_publish_year']}")
    lines.append(f"- Edition count: {metadata.get('edition_count', 0)}")
    lines.append(
        f"- Full text flag: {'yes' if metadata.get('has_fulltext') else 'no'}"
    )
    lines.append(
        f"- Public scan flag: {'yes' if metadata.get('public_scan') else 'no'}"
    )

    lines.extend(["", "## Identifiers"])
    for label, key_name in (
        ("ISBN", "isbn"),
        ("OCLC", "oclc"),
        ("LCCN", "lccn"),
    ):
        values = metadata.get(key_name) or []
        lines.append(f"- {label}: {', '.join(values) if values else 'none'}")

    lines.extend(["", "## Internet Archive scans"])
    scan_ids = metadata.get("ia_scan_id") or []
    if scan_ids:
        for scan_id in scan_ids[:20]:
            quoted = quote(scan_id, safe="")
            lines.append(f"- {scan_id}: https://archive.org/details/{quoted}")
    else:
        lines.append("- none listed")

    return "\n".join(lines).strip()


def _source_from_doc(doc: dict[str, Any]) -> Source | None:
    title = str(doc.get("title") or "").strip()
    url = _openlibrary_url(doc)
    if not title or url is None:
        return None
    metadata = _metadata_from_doc(doc)
    return Source(
        url=url,
        title=title,
        cleaned_text=_source_markdown(title, metadata),
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind=KIND,
        metadata=metadata,
    )


def _docs_from_payload(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if payload is None:
        return []
    docs = payload.get("docs")
    if not isinstance(docs, list):
        return []
    return [doc for doc in docs if isinstance(doc, dict)]


async def search(
    query: str,
    *,
    max_results: int = 20,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Search Open Library book metadata and return citation seed records."""
    q = query.strip()
    if not q or max_results <= 0:
        return []

    limit = min(max_results, 100)
    payload = await _request_search(
        {"q": q, "limit": limit, "fields": SEARCH_FIELDS},
        timeout=timeout,
    )

    results: list[SearchResult] = []
    seen: set[str] = set()
    for doc in _docs_from_payload(payload):
        result = _search_result_from_doc(doc)
        if result is None or result.url in seen:
            continue
        seen.add(result.url)
        results.append(result)
        if len(results) >= max_results:
            break
    return results


def _query_for_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").casefold()
    if host not in _HOSTS:
        return None
    path = unquote(parsed.path or "")

    key_match = _WORK_OR_BOOK_RE.match(path)
    if key_match is not None:
        collection = key_match.group("collection").lower()
        olid = key_match.group("olid").upper()
        key = f"/{collection}/{olid}"
        return f"key:{key}", f"{_SITE_BASE}{key}"

    identifier_match = _IDENTIFIER_RE.match(path)
    if identifier_match is not None:
        kind = identifier_match.group("kind").lower()
        identifier = identifier_match.group("identifier").strip()
        if identifier:
            return f"{kind}:{identifier}", f"{_SITE_BASE}/{kind}/{identifier}"

    return None


async def _enrich_with_hathitrust(source: Source) -> Source:
    try:
        from research_agent.tools import hathitrust

        return await hathitrust.enrich_source_from_identifiers(source)
    except Exception as exc:  # noqa: BLE001 — enrichment must never break fetch.
        logger.warning("openlibrary HathiTrust enrichment failed for %s: %s", source.url, exc)
        return source


async def fetch(url: str, *, timeout: float = 15.0) -> Source | None:
    """Fetch Open Library metadata for a work/book/identifier permalink."""
    classified = _query_for_url(url)
    if classified is None:
        return None
    query, _canonical_url = classified

    payload = await _request_search(
        {"q": query, "limit": 1, "fields": SEARCH_FIELDS},
        timeout=timeout,
    )
    docs = _docs_from_payload(payload)
    if not docs:
        return None
    source = _source_from_doc(docs[0])
    if source is None:
        return None
    return await _enrich_with_hathitrust(source)


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


class _PayloadSchema(_BaseSearchPayload):
    max_results: int | None = None
    timeout: float | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=("openlibrary.org", "www.openlibrary.org"),
    skill_name="openlibrary",
    description=(
        "Open Library book metadata, ISBN/OCLC/LCCN identifiers, and Internet"
        " Archive scan IDs through search.json"
    ),
    optional_payload_knobs="`max_results`",
    example_query="Pullman Strike 1894",
    module_name="openlibrary",
)


__all__ = [
    "KIND",
    "SEARCH_FIELDS",
    "fetch",
    "reset_for_tests",
    "search",
]
