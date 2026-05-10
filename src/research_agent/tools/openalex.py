"""OpenAlex Works connector (issue #241, A20).

Public surface:

* ``async def search(query, *, max_results=20, **knobs) -> list[SearchResult]``
  hits ``https://api.openalex.org/works`` with ``search=<query>`` and
  ``per_page=<max_results capped at 200>``.
* ``async def fetch(url) -> Source | None`` resolves OpenAlex work URLs, API
  URLs, and DOI resolver URLs to a single Work record.

OpenAlex is the academic-articles replacement for the retired JSTOR
Constellate slot. Low-volume requests can still run without auth, but the
current OpenAlex API policy expects a free ``OPENALEX_API_KEY`` for regular
use. The key is optional here and sent as ``api_key`` when configured.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, date, datetime
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

KIND: Literal["openalex_search"] = "openalex_search"

_WORKS_URL = "https://api.openalex.org/works"
_SITE_BASE = "https://openalex.org"
_HOSTS = frozenset({"openalex.org", "www.openalex.org", "api.openalex.org"})
_DOI_HOSTS = frozenset({"doi.org", "dx.doi.org"})
_MAX_PER_PAGE = 200
_POLITE_INTERVAL = 1.0 / 5.0
_COMMON_INTERVAL = 1.0
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.+-])",
    re.IGNORECASE,
)
_WORK_ID_RE = re.compile(r"^W\d+$", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


def _clean_text(value: Any) -> str:
    return _WS_RE.sub(" ", str(value or "")).strip()


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": config.get("RESEARCH_USER_AGENT")
        or "research-agent/0.1 (+local; contact unset)",
    }


def _contact_email() -> str:
    user_agent = config.get("RESEARCH_USER_AGENT") or ""
    match = _EMAIL_RE.search(user_agent)
    return match.group(1) if match else ""


def _api_key() -> str:
    return (config.get("OPENALEX_API_KEY") or "").strip()


def _request_params(base: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
    params = dict(base or {})
    email = _contact_email()
    key = _api_key()
    if email:
        params["mailto"] = email
    if key:
        params["api_key"] = key
    return params, bool(email or key)


async def _rate_limit_gate(*, identified: bool) -> None:
    """Block until the next OpenAlex request slot is available."""
    global _last_call_monotonic
    interval = _POLITE_INTERVAL if identified else _COMMON_INTERVAL
    async with _rate_lock:
        if _last_call_monotonic is not None:
            elapsed = time.monotonic() - _last_call_monotonic
            wait = interval - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
        _last_call_monotonic = time.monotonic()


async def _request_json(
    url: str,
    params: dict[str, Any],
    *,
    identified: bool,
    timeout: float,
) -> dict[str, Any] | None:
    await _rate_limit_gate(identified=identified)
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("openalex request failed for %s params=%s: %s", url, params, exc)
        return None

    if response.status_code != 200:
        logger.warning(
            "openalex request returned HTTP %s for %s params=%s",
            response.status_code,
            url,
            params,
        )
        return None

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("openalex request returned non-JSON for %s: %s", url, exc)
        return None
    if not isinstance(payload, dict):
        logger.warning("openalex request returned %s JSON", type(payload).__name__)
        return None
    return payload


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    text = _clean_text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _normalize_doi(value: Any) -> str:
    doi = _clean_text(value)
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:", "", doi, flags=re.IGNORECASE)
    return doi.rstrip(").]")


def _work_id_from_value(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    text = text.rstrip("/")
    if text.startswith("https://openalex.org/"):
        text = text.rsplit("/", 1)[-1]
    return text.upper() if _WORK_ID_RE.match(text) else text


def _openalex_url(work: dict[str, Any]) -> str:
    work_id = _work_id_from_value(work.get("id") or "")
    if work_id:
        return f"{_SITE_BASE}/{work_id}"
    raw_id = _clean_text(work.get("id"))
    return raw_id if raw_id.startswith("http") else ""


def _authors(work: dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    authorships = work.get("authorships")
    if not isinstance(authorships, list):
        return out
    for authorship in authorships:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author")
        if not isinstance(author, dict):
            continue
        name = _clean_text(author.get("display_name"))
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _host_venue(work: dict[str, Any]) -> str:
    for location_key in ("primary_location", "best_oa_location"):
        location = work.get(location_key)
        if not isinstance(location, dict):
            continue
        source = location.get("source")
        if isinstance(source, dict):
            name = _clean_text(source.get("display_name"))
            if name:
                return name
    host_venue = work.get("host_venue")
    if isinstance(host_venue, dict):
        return _clean_text(host_venue.get("display_name"))
    return ""


def _abstract_from_inverted_index(value: Any) -> str:
    """Reconstruct OpenAlex's ``abstract_inverted_index`` into readable text."""
    if not isinstance(value, dict):
        return ""

    positions: dict[int, str] = {}
    for word, raw_offsets in value.items():
        token = _clean_text(word)
        if not token or not isinstance(raw_offsets, list):
            continue
        for raw_offset in raw_offsets:
            try:
                offset = int(raw_offset)
            except (TypeError, ValueError):
                continue
            if offset < 0 or offset in positions:
                continue
            positions[offset] = token

    return " ".join(positions[offset] for offset in sorted(positions))


def _open_access_url(work: dict[str, Any]) -> str:
    for location_key in ("best_oa_location", "primary_location"):
        location = work.get(location_key)
        if not isinstance(location, dict):
            continue
        if location_key == "primary_location" and not location.get("is_oa"):
            continue
        for key in ("pdf_url", "landing_page_url"):
            url = _clean_text(location.get(key))
            if url:
                return url
    open_access = work.get("open_access")
    if isinstance(open_access, dict):
        return _clean_text(open_access.get("oa_url"))
    return ""


def _metadata_from_work(work: dict[str, Any]) -> dict[str, Any]:
    abstract = _abstract_from_inverted_index(work.get("abstract_inverted_index"))
    return {
        "doi": _normalize_doi(work.get("doi")),
        "openalex_id": _work_id_from_value(work.get("id")),
        "pub_year": _as_int(work.get("publication_year")),
        "publication_date": _clean_text(work.get("publication_date")),
        "authors": _authors(work),
        "host_venue": _host_venue(work),
        "abstract": abstract,
        "citation_count": _as_int(work.get("cited_by_count")) or 0,
        "open_access_url": _open_access_url(work),
        "work_type": _clean_text(work.get("type")),
        "language": _clean_text(work.get("language")),
        "fetched_via": KIND,
    }


def _snippet_from_work(work: dict[str, Any]) -> str:
    metadata = _metadata_from_work(work)
    parts: list[str] = []
    if metadata["authors"]:
        parts.append("Authors: " + ", ".join(metadata["authors"][:3]))
    if metadata["pub_year"]:
        parts.append(f"Year: {metadata['pub_year']}")
    if metadata["host_venue"]:
        parts.append(f"Venue: {metadata['host_venue']}")
    if metadata["doi"]:
        parts.append(f"DOI: {metadata['doi']}")
    if metadata["citation_count"]:
        parts.append(f"Citations: {metadata['citation_count']}")
    abstract = metadata["abstract"]
    if abstract:
        parts.append(abstract[:240])
    return " | ".join(parts)


def _search_result_from_work(work: dict[str, Any]) -> SearchResult | None:
    title = _clean_text(work.get("display_name") or work.get("title"))
    url = _openalex_url(work)
    if not title or not url:
        return None
    metadata = _metadata_from_work(work)
    return SearchResult(
        url=url,
        title=title,
        snippet=_snippet_from_work(work),
        published_at=_parse_date(work.get("publication_date")),
        source_kind=KIND,
        extras=metadata,
    )


def _source_markdown(title: str, metadata: dict[str, Any]) -> str:
    lines = [f"# {title}", ""]
    for label, key in (
        ("OpenAlex ID", "openalex_id"),
        ("DOI", "doi"),
        ("Publication year", "pub_year"),
        ("Host venue", "host_venue"),
        ("Citation count", "citation_count"),
        ("Open access URL", "open_access_url"),
        ("Work type", "work_type"),
        ("Language", "language"),
    ):
        value = metadata.get(key)
        if value not in (None, "", []):
            lines.append(f"- {label}: {value}")
    authors = metadata.get("authors") or []
    if authors:
        lines.append(f"- Authors: {', '.join(authors)}")

    abstract = _clean_text(metadata.get("abstract"))
    if abstract:
        lines.extend(["", "## Abstract", abstract])
    return "\n".join(lines).strip()


def _source_from_work(work: dict[str, Any]) -> Source | None:
    title = _clean_text(work.get("display_name") or work.get("title"))
    url = _openalex_url(work)
    if not title or not url:
        return None
    metadata = _metadata_from_work(work)
    return Source(
        url=url,
        title=title,
        cleaned_text=_source_markdown(title, metadata),
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind=KIND,
        metadata=metadata,
    )


async def search(
    query: str,
    *,
    max_results: int = 20,
    filter: str | None = None,
    sort: str | None = None,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Search OpenAlex Works and return scholarly article metadata."""
    q = query.strip()
    if not q or max_results <= 0:
        return []

    base_params: dict[str, Any] = {
        "search": q,
        "per_page": min(max_results, _MAX_PER_PAGE),
    }
    if filter:
        base_params["filter"] = filter
    if sort:
        base_params["sort"] = sort
    params, identified = _request_params(base_params)
    payload = await _request_json(
        _WORKS_URL,
        params,
        identified=identified,
        timeout=timeout,
    )
    if payload is None:
        return []

    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []

    results: list[SearchResult] = []
    seen: set[str] = set()
    for work in raw_results:
        if not isinstance(work, dict):
            continue
        result = _search_result_from_work(work)
        if result is None or result.url in seen:
            continue
        seen.add(result.url)
        results.append(result)
        if len(results) >= max_results:
            break
    return results


def _identifier_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").casefold()

    if host in _DOI_HOSTS:
        doi = _normalize_doi(unquote(parsed.path.lstrip("/")))
        return f"https://doi.org/{doi}" if doi else None

    if host not in _HOSTS:
        return None

    path = unquote(parsed.path or "")
    if host == "api.openalex.org":
        if path.startswith("/works/"):
            identifier = path.removeprefix("/works/").strip("/")
            return identifier or None
        return None

    path = path.strip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) == 1 and _WORK_ID_RE.match(parts[0]):
        return parts[0].upper()
    if len(parts) >= 2 and parts[0] == "works" and _WORK_ID_RE.match(parts[1]):
        return parts[1].upper()
    return None


async def fetch(url: str, *, timeout: float = 15.0) -> Source | None:
    """Fetch a single OpenAlex Work by OpenAlex URL, API URL, or DOI URL."""
    identifier = _identifier_from_url(url)
    if identifier is None:
        return None

    request_url = f"{_WORKS_URL}/{quote(identifier, safe=':/')}"
    params, identified = _request_params()
    payload = await _request_json(
        request_url,
        params,
        identified=identified,
        timeout=timeout,
    )
    if payload is None:
        return None
    return _source_from_work(payload)


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


class _PayloadSchema(_BaseSearchPayload):
    max_results: int | None = None
    filter: str | None = None
    sort: str | None = None
    timeout: float | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=("openalex.org", "api.openalex.org", "doi.org"),
    skill_name="openalex",
    description=(
        "OpenAlex Works scholarly articles, abstracts, DOIs, citations, authors,"
        " venues, and open-access URLs"
    ),
    optional_payload_knobs="`max_results`, `filter`, `sort`",
    example_query="Project 2025 unitary executive theory",
    module_name="openalex",
)


__all__ = [
    "KIND",
    "fetch",
    "reset_for_tests",
    "search",
]
