"""UK National Archives Discovery API connector (issue #231, A9).

Public surface:

* ``async def search(query, *, max_results=20) -> list[SearchResult]`` hits
  ``https://discovery.nationalarchives.gov.uk/API/search/v1/records`` with
  ``sps.searchQuery=<query>``.
* ``async def fetch(url) -> Source | None`` accepts Discovery detail URLs and
  API search URLs, re-queries the Discovery API, and renders a metadata card.

Discovery is a beta JSON API and exposes catalogue descriptions, not full
record text. This connector treats schema drift as a warning and keeps going
with whatever useful fields remain. No auth is required.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

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

KIND: Literal["ukna_search"] = "ukna_search"

_SEARCH_URL = "https://discovery.nationalarchives.gov.uk/API/search/v1/records"
_SITE_BASE = "https://discovery.nationalarchives.gov.uk"
_HOSTS = frozenset({"discovery.nationalarchives.gov.uk"})
_RATE_LIMIT_INTERVAL = 1.0
_MAX_RESULTS = 1000

_DETAIL_PATH_RE = re.compile(r"^/details/(?P<section>[rc])/(?P<token>[^/?#]+)")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_RECORDED_TOP_KEYS = frozenset(
    {
        "Records",
        "TaxonomySubjects",
        "TimePeriods",
        "Departments",
        "CatalogueLevels",
        "ClosureStatuses",
        "Sources",
        "Repositories",
        "HeldByReps",
        "ReferenceFirstLetters",
        "TitleFirstLetters",
        "Count",
        "NextBatchMark",
    }
)
_REQUIRED_TOP_KEYS = frozenset({"Records", "Count"})
_TOP_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    key: (key, key[:1].lower() + key[1:]) for key in _RECORDED_TOP_KEYS
}
_RECORDED_RECORD_KEYS = frozenset(
    {
        "AltName",
        "Places",
        "CorpBodies",
        "Taxonomies",
        "FormerReferenceDep",
        "FormerReferencePro",
        "HeldBy",
        "Context",
        "Content",
        "URLParameters",
        "Department",
        "Note",
        "AdminHistory",
        "Arrangement",
        "MapDesignation",
        "MapScale",
        "PhysicalCondition",
        "CatalogueLevel",
        "OpeningDate",
        "ClosureStatus",
        "ClosureType",
        "ClosureCode",
        "DocumentType",
        "CoveringDates",
        "Description",
        "EndDate",
        "NumEndDate",
        "NumStartDate",
        "StartDate",
        "Id",
        "Reference",
        "Score",
        "Source",
        "Title",
    }
)
_REQUIRED_RECORD_KEYS = frozenset(
    {"Title", "Reference", "CoveringDates", "HeldBy", "Content", "URLParameters"}
)
_RECORD_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    key: (key, key[:1].lower() + key[1:]) for key in _RECORDED_RECORD_KEYS
}
_RECORD_KEY_ALIASES["URLParameters"] = ("URLParameters", "urlParameters")

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None
_schema_drift_warnings: set[str] = set()


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": config.get("RESEARCH_USER_AGENT")
        or "research-agent/0.1 (+local; contact unset)",
    }


async def _rate_limit_gate() -> None:
    """Block until the next 1 RPS Discovery API request slot is available."""
    global _last_call_monotonic
    async with _rate_lock:
        if _last_call_monotonic is not None:
            elapsed = time.monotonic() - _last_call_monotonic
            wait = _RATE_LIMIT_INTERVAL - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
        _last_call_monotonic = time.monotonic()


def _warn_schema_drift(code: str, detail: str) -> None:
    if code in _schema_drift_warnings:
        return
    _schema_drift_warnings.add(code)
    logger.warning("ukna_search: beta API schema drift: %s", detail)


async def _request_json(
    *,
    params: dict[str, Any],
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
        logger.warning("ukna_search: request failed: %s", exc)
        return None

    if response.status_code == 429:
        logger.warning("ukna_search: UKNA Discovery API rate limit returned HTTP 429")
        return None
    if 400 <= response.status_code < 500:
        logger.warning(
            "ukna_search: UKNA Discovery API returned HTTP %s",
            response.status_code,
        )
        return None
    if response.status_code >= 500:
        logger.warning(
            "ukna_search: UKNA Discovery API server error HTTP %s",
            response.status_code,
        )
        return None

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("ukna_search: UKNA Discovery API response was not JSON: %s", exc)
        return None
    if not isinstance(payload, dict):
        _warn_schema_drift("payload_type", f"expected object, got {type(payload).__name__}")
        return None
    return payload


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = html.unescape(_TAG_RE.sub(" ", value))
        return _WS_RE.sub(" ", text).strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in (
            "value",
            "name",
            "title",
            "label",
            "description",
            "text",
            "Code",
            "Id",
            "code",
            "id",
        ):
            text = _clean_text(value.get(key))
            if text:
                return text
        for item in value.values():
            text = _clean_text(item)
            if text:
                return text
        return ""
    if isinstance(value, (list, tuple, set)):
        parts: list[str] = []
        for item in value:
            text = _clean_text(item)
            if text and text not in parts:
                parts.append(text)
        return "; ".join(parts)
    return str(value).strip()


def _canonical_keyset(
    keys: set[str],
    aliases: dict[str, tuple[str, ...]],
) -> set[str]:
    reverse = {
        alias: canonical
        for canonical, key_aliases in aliases.items()
        for alias in key_aliases
    }
    return {reverse.get(key, key) for key in keys}


def _field(mapping: dict[str, Any], key: str, *fallbacks: str) -> Any:
    for candidate in (key, *fallbacks):
        aliases = _RECORD_KEY_ALIASES.get(candidate, ())
        for alias in (candidate, *aliases, candidate[:1].lower() + candidate[1:]):
            if alias in mapping:
                return mapping[alias]
    return None


def _first_text(*values: Any) -> str:
    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return ""


def _truncate(text: str, limit: int = 300) -> str:
    text = _clean_text(text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _score(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _records_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    top_keys = _canonical_keyset(set(payload), _TOP_KEY_ALIASES)
    missing_top = _REQUIRED_TOP_KEYS - top_keys
    if missing_top:
        _warn_schema_drift(
            "top_missing",
            "missing expected top-level keys "
            f"{sorted(missing_top)}; recorded keys include {sorted(_RECORDED_TOP_KEYS)}",
        )
    recorded_top_missing = _RECORDED_TOP_KEYS - top_keys
    recorded_top_extra = top_keys - _RECORDED_TOP_KEYS
    if recorded_top_missing or recorded_top_extra:
        _warn_schema_drift(
            "top_keyset",
            "top-level keys differ from recorded fixture; "
            f"missing={sorted(recorded_top_missing)} extra={sorted(recorded_top_extra)}",
        )

    records = payload.get("Records", payload.get("records"))
    if records is None:
        return []
    if not isinstance(records, list):
        _warn_schema_drift(
            "records_type",
            f"expected Records list, got {type(records).__name__}",
        )
        return []

    out: list[dict[str, Any]] = []
    for item in records:
        if not isinstance(item, dict):
            _warn_schema_drift(
                "record_item_type",
                f"expected each Records item to be object, got {type(item).__name__}",
            )
            continue
        record_keys = _canonical_keyset(set(item), _RECORD_KEY_ALIASES)
        missing_record = _REQUIRED_RECORD_KEYS - record_keys
        if missing_record:
            _warn_schema_drift(
                "record_missing",
                "record missing expected catalogue keys "
                f"{sorted(missing_record)}; recorded keys include "
                f"{sorted(_RECORDED_RECORD_KEYS)}",
            )
        recorded_record_missing = _RECORDED_RECORD_KEYS - record_keys
        recorded_record_extra = record_keys - _RECORDED_RECORD_KEYS
        if recorded_record_missing or recorded_record_extra:
            _warn_schema_drift(
                "record_keyset",
                "record keys differ from recorded fixture; "
                f"missing={sorted(recorded_record_missing)} "
                f"extra={sorted(recorded_record_extra)}",
            )
        out.append(item)
    return out


def _record_reference(record: dict[str, Any]) -> str:
    return _first_text(_field(record, "Reference"))


def _record_title(record: dict[str, Any]) -> str:
    return _first_text(
        _field(record, "Title"),
        _field(record, "Description"),
        _record_reference(record),
        _field(record, "Id"),
        "UK National Archives record",
    )


def _scope_content(record: dict[str, Any]) -> str:
    return _first_text(
        _field(record, "Content"),
        _field(record, "ScopeContent"),
        _field(record, "Description"),
        _field(record, "Context"),
        _field(record, "Note"),
    )


def _detail_token(record: dict[str, Any]) -> str:
    return _first_text(
        _field(record, "URLParameters"),
        _field(record, "Id"),
    )


def _record_url(record: dict[str, Any]) -> str:
    for key in ("Url", "URL", "url", "Link"):
        text = _clean_text(record.get(key))
        if text.startswith(("http://", "https://")):
            return text
    token = _detail_token(record)
    if token:
        if token.startswith(("http://", "https://")):
            return token
        return f"{_SITE_BASE}/details/r/{quote(token, safe='')}"
    reference = _record_reference(record)
    if reference:
        return f"{_SEARCH_URL}?{urlencode({'sps.searchQuery': reference})}"
    return _SITE_BASE


def _metadata_for_record(record: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "catalogue_reference": _record_reference(record),
        "covering_dates": _first_text(_field(record, "CoveringDates")),
        "held_by": _first_text(_field(record, "HeldBy")),
        "scope_content": _scope_content(record),
        "department": _first_text(_field(record, "Department")),
        "catalogue_level": _field(record, "CatalogueLevel"),
        "record_id": _first_text(_field(record, "Id")),
        "url_parameters": _detail_token(record),
        "source": _first_text(_field(record, "Source")),
        "closure_status": _first_text(_field(record, "ClosureStatus")),
        "opening_date": _first_text(_field(record, "OpeningDate")),
    }
    return {
        key: value
        for key, value in metadata.items()
        if value not in ("", None, [], {})
    }


def _snippet(record: dict[str, Any]) -> str:
    parts: list[str] = []
    if ref := _record_reference(record):
        parts.append(f"Ref: {ref}")
    if dates := _first_text(_field(record, "CoveringDates")):
        parts.append(f"Dates: {dates}")
    if held_by := _first_text(_field(record, "HeldBy")):
        parts.append(f"Held by: {held_by}")
    if scope := _scope_content(record):
        parts.append(_truncate(scope, 220))
    return " | ".join(parts)


def _build_result(record: dict[str, Any]) -> SearchResult | None:
    title = _record_title(record)
    url = _record_url(record)
    if not title or not url:
        return None

    metadata = _metadata_for_record(record)
    score = _score(_field(record, "Score"))
    extras = dict(metadata)
    if score is not None:
        extras["score"] = score
    return SearchResult(
        url=url,
        title=title,
        snippet=_snippet(record),
        source_kind=KIND,
        score=score,
        extras=extras,
    )


async def search(
    query: str,
    *,
    max_results: int = 20,
    page: int | None = None,
    timeout: float = 20.0,
) -> list[SearchResult]:
    """Search UKNA Discovery catalogue descriptions.

    The API indexes catalogue metadata and descriptions, not the text inside
    physical records or scanned images.
    """
    q = (query or "").strip()
    if not q:
        return []

    rows = min(max(1, int(max_results)), _MAX_RESULTS)
    params: dict[str, Any] = {
        "sps.searchQuery": q,
        "sps.resultsPageSize": rows,
    }
    if page is not None:
        params["sps.page"] = page

    payload = await _request_json(params=params, timeout=timeout)
    if payload is None:
        return []

    results: list[SearchResult] = []
    for record in _records_from_payload(payload):
        result = _build_result(record)
        if result is not None:
            results.append(result)
        if len(results) >= rows:
            break
    return results


def _extract_query_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if host not in _HOSTS:
        return ""

    match = _DETAIL_PATH_RE.match(parsed.path)
    if match:
        return unquote(match.group("token")).strip()

    if parsed.path.rstrip("/").casefold() == "/api/search/v1/records":
        query = parse_qs(parsed.query)
        for key in ("sps.searchQuery", "sps.searchquery", "SearchQuery", "searchQuery"):
            values = query.get(key)
            if values:
                return values[0].strip()
    return ""


def _normalized_lookup(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", _clean_text(value).casefold())


def _record_matches_query(record: dict[str, Any], query: str) -> bool:
    wanted = _normalized_lookup(query)
    if not wanted:
        return False
    candidates = (
        _field(record, "URLParameters"),
        _field(record, "Id"),
        _field(record, "Reference"),
    )
    return any(_normalized_lookup(candidate) == wanted for candidate in candidates)


async def _fetch_record(query: str, *, timeout: float) -> dict[str, Any] | None:
    payload = await _request_json(
        params={"sps.searchQuery": query, "sps.resultsPageSize": 10},
        timeout=timeout,
    )
    if payload is None:
        return None
    records = _records_from_payload(payload)
    if not records:
        return None
    for record in records:
        if _record_matches_query(record, query):
            return record
    return records[0]


def _render_source_text(
    *,
    title: str,
    url: str,
    metadata: dict[str, Any],
) -> str:
    lines = [f"# {title}", "", f"URL: {url}"]
    if ref := metadata.get("catalogue_reference"):
        lines.append(f"Catalogue reference: {ref}")
    if dates := metadata.get("covering_dates"):
        lines.append(f"Covering dates: {dates}")
    if held_by := metadata.get("held_by"):
        lines.append(f"Held by: {held_by}")
    if department := metadata.get("department"):
        lines.append(f"Department: {department}")
    if level := metadata.get("catalogue_level"):
        lines.append(f"Catalogue level: {level}")
    if closure := metadata.get("closure_status"):
        lines.append(f"Closure status: {closure}")
    if opening := metadata.get("opening_date"):
        lines.append(f"Opening date: {opening}")

    scope = metadata.get("scope_content")
    if isinstance(scope, str) and scope:
        lines.extend(["", "## Scope and Content", scope])
    return "\n".join(lines).strip()


async def fetch(url: str, *, timeout: float = 20.0) -> Source | None:
    """Fetch one Discovery catalogue record by detail/API URL."""
    query = _extract_query_from_url(url)
    if not query:
        return None

    record = await _fetch_record(query, timeout=timeout)
    if record is None:
        return None

    canonical_url = _record_url(record)
    title = _record_title(record)
    metadata = _metadata_for_record(record)
    return Source(
        url=canonical_url,
        title=title,
        cleaned_text=_render_source_text(
            title=title,
            url=canonical_url,
            metadata=metadata,
        ),
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind=KIND,
        metadata=metadata,
    )


def reset_for_tests() -> None:
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()
    _schema_drift_warnings.clear()


class _PayloadSchema(_BaseSearchPayload):
    max_results: int | None = None
    page: int | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=("discovery.nationalarchives.gov.uk",),
    skill_name="ukna",
    description=(
        "UK National Archives Discovery catalogue metadata for Foreign Office,"
        " War Office, Colonial Office, and other UK archival records (no auth)"
    ),
    optional_payload_knobs="`max_results`, `page`",
    example_query="Mau Mau Kenya",
    module_name="ukna",
)


__all__ = ["KIND", "fetch", "reset_for_tests", "search"]
