"""Digital Public Library of America API connector (issue #228, A6).

Public surface:

* ``async def search(query, *, max_results=20) -> list[SearchResult]`` hits
  ``https://api.dp.la/v2/items`` with ``q=<query>`` and the required
  ``DPLA_API_KEY`` URL parameter.
* ``async def fetch(url) -> Source | None`` accepts DPLA item pages and item
  API URLs, rehydrates one API record by DPLA id, and renders archival
  metadata as a citeable source.

DPLA keys are free but required. Request one with
``curl -X POST https://api.dp.la/v2/api_key/<your-email>``; the 32-character
key arrives by email and is sent as ``?api_key=<key>`` on API requests. This
connector enforces a conservative 1 RPS process-local gate and returns
``[]`` / ``None`` when ``DPLA_API_KEY`` is unset.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, date, datetime
from typing import Any, Literal
from urllib.parse import parse_qs, quote, unquote, urlparse

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

KIND: Literal["dpla_search"] = "dpla_search"

_ITEMS_URL = "https://api.dp.la/v2/items"
_SITE_BASE = "https://dp.la/item"
_HOSTS = frozenset({"api.dp.la", "dp.la", "www.dp.la"})
_RATE_LIMIT_INTERVAL = 1.0
_MAX_PAGE_SIZE = 100
_WS_RE = re.compile(r"\s+")

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None
_missing_key_warned = False


def _api_key() -> str:
    return (config.get("DPLA_API_KEY") or "").strip()


def _missing_key_message() -> str:
    return (
        "dpla_search: would need DPLA_API_KEY; request one with "
        "curl -X POST https://api.dp.la/v2/api_key/<your-email>; skipping"
    )


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": config.get("RESEARCH_USER_AGENT")
        or "research-agent/0.1 (+local; contact unset)",
    }


async def _rate_limit_gate() -> None:
    """Block until the next 1 RPS request slot is available."""
    global _last_call_monotonic
    async with _rate_lock:
        if _last_call_monotonic is not None:
            elapsed = time.monotonic() - _last_call_monotonic
            wait = _RATE_LIMIT_INTERVAL - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
        _last_call_monotonic = time.monotonic()


async def _request_json(
    url: str,
    *,
    params: dict[str, Any],
    timeout: float,
) -> dict[str, Any] | None:
    global _missing_key_warned
    key = _api_key()
    if not key:
        if not _missing_key_warned:
            logger.warning(_missing_key_message())
            _missing_key_warned = True
        return None

    request_params = {**params, "api_key": key}
    await _rate_limit_gate()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url, params=request_params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("dpla_search: request failed for %s: %s", url, exc)
        return None

    if response.status_code == 429:
        logger.warning("dpla_search: DPLA API rate limit returned HTTP 429")
        return None
    if 400 <= response.status_code < 500:
        logger.warning("dpla_search: DPLA API returned HTTP %s", response.status_code)
        return None
    if response.status_code >= 500:
        logger.warning(
            "dpla_search: DPLA API server error HTTP %s", response.status_code
        )
        return None

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("dpla_search: DPLA API response was not JSON: %s", exc)
        return None
    return payload if isinstance(payload, dict) else None


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _WS_RE.sub(" ", value).strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in (
            "name",
            "title",
            "value",
            "displayDate",
            "begin",
            "end",
            "description",
            "@id",
            "id",
        ):
            text = _clean_text(value.get(key))
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


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _first_text(*values: Any) -> str:
    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return ""


def _join_text(value: Any, *, limit: int = 4) -> str:
    parts: list[str] = []
    for item in _as_list(value):
        text = _clean_text(item)
        if text and text not in parts:
            parts.append(text)
        if len(parts) >= limit:
            break
    return "; ".join(parts)


def _first_url(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        return text if text.startswith(("http://", "https://")) else ""
    if isinstance(value, dict):
        for key in ("@id", "id", "url", "content", "thumbnail"):
            url = _first_url(value.get(key))
            if url:
                return url
    if isinstance(value, (list, tuple)):
        for item in value:
            url = _first_url(item)
            if url:
                return url
    return ""


def _truncate(text: str, limit: int = 350) -> str:
    text = _clean_text(text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _source_resource(record: dict[str, Any]) -> dict[str, Any]:
    source = record.get("sourceResource")
    return source if isinstance(source, dict) else {}


def _dpla_id(record: dict[str, Any]) -> str:
    raw = _first_text(record.get("id"), record.get("@id"))
    if not raw:
        return ""
    raw = unquote(raw).strip()
    if "/" in raw:
        raw = raw.rstrip("/").rsplit("/", 1)[-1]
    return raw


def _dpla_url(dpla_id: str) -> str:
    return f"{_SITE_BASE}/{quote(dpla_id, safe='')}"


def _title(record: dict[str, Any]) -> str:
    source = _source_resource(record)
    return _first_text(
        source.get("title"),
        record.get("title"),
        _dpla_id(record) and f"DPLA item {_dpla_id(record)}",
    )


def _provider(record: dict[str, Any]) -> str:
    return _first_text(record.get("provider"))


def _data_provider(record: dict[str, Any]) -> str:
    return _first_text(
        record.get("dataProvider"),
        _source_resource(record).get("dataProvider"),
    )


def _license(record: dict[str, Any]) -> str:
    source = _source_resource(record)
    return _first_text(
        source.get("rights"),
        record.get("rights"),
        record.get("object", {}).get("rights")
        if isinstance(record.get("object"), dict)
        else None,
        record.get("hasView", {}).get("rights")
        if isinstance(record.get("hasView"), dict)
        else None,
    )


def _object_url(record: dict[str, Any]) -> str:
    return _first_url(record.get("object")) or _first_url(record.get("isShownAt"))


def _is_shown_at(record: dict[str, Any]) -> str:
    return _first_url(record.get("isShownAt"))


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, dict):
        for key in ("displayDate", "begin", "end"):
            parsed = _parse_date(value.get(key))
            if parsed is not None:
                return parsed
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            parsed = _parse_date(item)
            if parsed is not None:
                return parsed
        return None

    text = _clean_text(value)
    if not text:
        return None
    head = text.split("T", 1)[0].strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(head, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    match = re.search(r"\b(1[5-9]\d{2}|20\d{2})\b", text)
    if match:
        try:
            return datetime(int(match.group(1)), 1, 1, tzinfo=UTC)
        except ValueError:
            return None
    return None


def _metadata(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "dpla_id": _dpla_id(record),
        "provider": _provider(record),
        "data_provider": _data_provider(record),
        "license": _license(record),
        "object_url": _object_url(record),
        "is_shown_at": _is_shown_at(record),
        "fetched_via": KIND,
    }


def _snippet(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    source = _source_resource(record)
    parts: list[str] = []
    if metadata["data_provider"]:
        parts.append(f"Data provider: {metadata['data_provider']}")
    if metadata["provider"]:
        parts.append(f"DPLA provider: {metadata['provider']}")
    date_text = _join_text(source.get("date"), limit=2)
    if date_text:
        parts.append(f"Date: {date_text}")
    type_text = _join_text(source.get("type"), limit=2)
    if type_text:
        parts.append(f"Type: {type_text}")
    subjects = _join_text(source.get("subject"), limit=4)
    if subjects:
        parts.append(f"Subjects: {subjects}")
    if metadata["license"]:
        parts.append(f"Rights: {_truncate(metadata['license'], 120)}")
    description = _join_text(source.get("description"), limit=2)
    if description:
        parts.append(_truncate(description, 220))
    return " | ".join(parts) or _title(record)


def _docs(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if payload is None:
        return []
    docs = payload.get("docs")
    if isinstance(docs, list):
        return [doc for doc in docs if isinstance(doc, dict)]
    response = payload.get("response")
    if isinstance(response, dict) and isinstance(response.get("docs"), list):
        return [doc for doc in response["docs"] if isinstance(doc, dict)]
    return []


def _record_from_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    docs = _docs(payload)
    if docs:
        return docs[0]
    if payload is None:
        return None
    if "sourceResource" in payload or "dataProvider" in payload or "id" in payload:
        return payload
    doc = payload.get("doc")
    return doc if isinstance(doc, dict) else None


def _result_from_record(record: dict[str, Any]) -> SearchResult | None:
    dpla_id = _dpla_id(record)
    title = _title(record)
    if not dpla_id or not title:
        return None
    metadata = _metadata(record)

    score: float | None = None
    raw_score = record.get("score")
    if raw_score not in (None, ""):
        try:
            score = float(str(raw_score))
        except (TypeError, ValueError):
            score = None

    return SearchResult(
        url=_dpla_url(dpla_id),
        title=title,
        snippet=_snippet(record, metadata),
        published_at=_parse_date(_source_resource(record).get("date")),
        source_kind=KIND,
        score=score,
        extras=metadata,
    )


async def search(
    query: str,
    *,
    max_results: int = 20,
    provider: str | None = None,
    timeout: float = 20.0,
) -> list[SearchResult]:
    """Search DPLA item metadata and return DPLA item-page URLs."""
    q = (query or "").strip()
    if not q or max_results <= 0:
        return []

    page_size = min(max(1, int(max_results)), _MAX_PAGE_SIZE)
    params: dict[str, Any] = {"q": q, "page_size": page_size}
    if provider:
        params["provider"] = provider

    payload = await _request_json(_ITEMS_URL, params=params, timeout=timeout)

    results: list[SearchResult] = []
    seen: set[str] = set()
    for record in _docs(payload):
        result = _result_from_record(record)
        if result is None or result.url in seen:
            continue
        seen.add(result.url)
        results.append(result)
        if len(results) >= max_results:
            break
    return results


def _is_supported_host(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    return (parsed.hostname or "").casefold() in _HOSTS


def _extract_dpla_id(url: str) -> str:
    if not _is_supported_host(url):
        return ""
    parsed = urlparse(url)
    path = unquote(parsed.path or "").strip("/")

    if (parsed.hostname or "").casefold() == "api.dp.la":
        match = re.match(r"^v2/items/(?P<id>[^/?#]+)$", path)
        if match:
            return match.group("id").strip()
        query = parse_qs(parsed.query)
        for key in ("id", "dpla_id"):
            values = query.get(key)
            if values:
                return values[0].strip()

    if path.startswith("item/"):
        return path.split("/", 1)[1].strip()
    if path.startswith("api/items/"):
        return path.rsplit("/", 1)[-1].strip()
    return ""


async def _fetch_record(dpla_id: str, *, timeout: float) -> dict[str, Any] | None:
    payload = await _request_json(
        f"{_ITEMS_URL}/{quote(dpla_id, safe='')}",
        params={},
        timeout=timeout,
    )
    return _record_from_payload(payload)


def _source_markdown(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    source = _source_resource(record)
    lines = [f"# {_title(record)}", ""]
    if metadata["dpla_id"]:
        lines.append(f"- DPLA ID: {metadata['dpla_id']}")
    if metadata["provider"]:
        lines.append(f"- DPLA provider: {metadata['provider']}")
    if metadata["data_provider"]:
        lines.append(f"- Data provider: {metadata['data_provider']}")
    date_text = _join_text(source.get("date"), limit=3)
    if date_text:
        lines.append(f"- Date: {date_text}")
    type_text = _join_text(source.get("type"), limit=3)
    if type_text:
        lines.append(f"- Type: {type_text}")
    if metadata["license"]:
        lines.append(f"- Rights/license: {metadata['license']}")
    if metadata["object_url"]:
        lines.append(f"- Object URL: {metadata['object_url']}")
    if metadata["is_shown_at"]:
        lines.append(f"- Provider item URL: {metadata['is_shown_at']}")

    description = _join_text(source.get("description"), limit=4)
    if description:
        lines.extend(["", "## Description", description])

    subjects = _join_text(source.get("subject"), limit=12)
    if subjects:
        lines.extend(["", "## Subjects", subjects])

    detail_lines: list[str] = []
    for label, key in (
        ("Creator", "creator"),
        ("Contributor", "contributor"),
        ("Publisher", "publisher"),
        ("Format", "format"),
        ("Language", "language"),
        ("Spatial", "spatial"),
        ("Temporal", "temporal"),
        ("Collection", "collection"),
    ):
        text = _join_text(source.get(key), limit=5)
        if text:
            detail_lines.append(f"- {label}: {text}")
    if detail_lines:
        lines.extend(["", "## Metadata", *detail_lines])

    return "\n".join(lines).strip()


async def fetch(url: str, *, timeout: float = 20.0) -> Source | None:
    """Fetch one DPLA item metadata record from a DPLA item/API URL."""
    dpla_id = _extract_dpla_id(url)
    if not dpla_id:
        return None

    record = await _fetch_record(dpla_id, timeout=timeout)
    if record is None:
        return None

    metadata = _metadata(record)
    resolved_id = metadata["dpla_id"] or dpla_id
    return Source(
        url=_dpla_url(resolved_id),
        title=_title(record),
        cleaned_text=_source_markdown(record, metadata),
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind=KIND,
        metadata=metadata,
    )


def reset_for_tests() -> None:
    """Clear process-local rate-limit and warning state. Test-only."""
    global _last_call_monotonic, _rate_lock, _missing_key_warned
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()
    _missing_key_warned = False


class _PayloadSchema(_BaseSearchPayload):
    max_results: int | None = None
    provider: str | None = None
    timeout: float | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=("api.dp.la", "dp.la", "www.dp.la"),
    skill_name="dpla",
    description=(
        "Digital Public Library of America item metadata across US cultural"
        " institutions; requires DPLA_API_KEY"
    ),
    optional_payload_knobs="`max_results`, `provider`",
    example_query="Maya land claims",
    module_name="dpla",
)


__all__ = ["KIND", "fetch", "reset_for_tests", "search"]
