"""Smithsonian Open Access connector (issue #227, A5).

Public surface:

* ``async def search(query, *, max_results=20) -> list[SearchResult]`` hits
  ``https://api.si.edu/openaccess/api/v1.0/search`` with the shared
  ``DATA_GOV_API_KEY`` api.data.gov credential.
* ``async def fetch(url) -> Source | None`` accepts Smithsonian object pages
  and Open Access content API URLs, re-fetches the object metadata, and
  renders it as a citeable source.

Auth mirrors the FEC/Congress api.data.gov pattern: fall back to ``DEMO_KEY``
when ``DATA_GOV_API_KEY`` is unset. DEMO_KEY is only practical for smoke
checks (~40 req/hr per IP), so the connector enforces a conservative 1 RPS
process-local gate.
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

KIND: Literal["si_search"] = "si_search"

_SEARCH_URL = "https://api.si.edu/openaccess/api/v1.0/search"
_CONTENT_BASE_URL = "https://api.si.edu/openaccess/api/v1.0/content"
_SITE_BASE = "https://www.si.edu/object"
_HOSTS = frozenset({"api.si.edu", "si.edu", "www.si.edu", "3d.si.edu"})
_RATE_LIMIT_INTERVAL = 1.0
_MAX_ROWS = 100
_WS_RE = re.compile(r"\s+")
_RECORD_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_.:-]+$")

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


def _resolve_api_key() -> str:
    """Return the shared api.data.gov key, falling back to DEMO_KEY."""
    key = (config.get("DATA_GOV_API_KEY") or "").strip()
    if key:
        return key
    logger.warning(
        "DATA_GOV_API_KEY not set — si_search falling back to DEMO_KEY "
        "(~40 req/hr per IP). Sign up at https://api.data.gov/signup/ for "
        "a production api.data.gov key."
    )
    return "DEMO_KEY"


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
    await _rate_limit_gate()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("si_search: request failed for %s: %s", url, exc)
        return None

    if response.status_code == 429:
        logger.warning("si_search: Smithsonian API rate limit returned HTTP 429")
        return None
    if 400 <= response.status_code < 500:
        logger.warning(
            "si_search: Smithsonian API returned HTTP %s", response.status_code
        )
        return None
    if response.status_code >= 500:
        logger.warning(
            "si_search: Smithsonian API server error HTTP %s", response.status_code
        )
        return None

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("si_search: Smithsonian API response was not JSON: %s", exc)
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
        for key in ("content", "value", "title", "label", "name", "access"):
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


def _truncate(text: str, limit: int = 350) -> str:
    text = _clean_text(text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, (list, tuple)):
        for item in value:
            parsed = _parse_date(item)
            if parsed is not None:
                return parsed
        return None
    text = _clean_text(value)
    if not text:
        return None
    for token in re.findall(r"\b(?:1[5-9]\d{2}|20\d{2})\b", text):
        try:
            return datetime(int(token), 1, 1, tzinfo=UTC)
        except ValueError:
            continue
    head = text.split("T", 1)[0].strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(head, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _content(record: dict[str, Any]) -> dict[str, Any]:
    content = record.get("content")
    return content if isinstance(content, dict) else {}


def _descriptive(record: dict[str, Any]) -> dict[str, Any]:
    descriptive = _content(record).get("descriptiveNonRepeating")
    return descriptive if isinstance(descriptive, dict) else {}


def _structured(record: dict[str, Any]) -> dict[str, Any]:
    structured = _content(record).get("indexedStructured")
    return structured if isinstance(structured, dict) else {}


def _freetext(record: dict[str, Any]) -> dict[str, Any]:
    freetext = _content(record).get("freetext")
    return freetext if isinstance(freetext, dict) else {}


def _freetext_values(record: dict[str, Any], key: str) -> list[str]:
    values: list[str] = []
    for item in _as_list(_freetext(record).get(key)):
        text = _clean_text(item)
        if text and text not in values:
            values.append(text)
    return values


def _freetext_label(record: dict[str, Any], *labels: str) -> str:
    wanted = {label.casefold() for label in labels}
    for entries in _freetext(record).values():
        for item in _as_list(entries):
            if not isinstance(item, dict):
                continue
            label = _clean_text(item.get("label")).casefold()
            if label in wanted:
                text = _clean_text(item.get("content") or item.get("value"))
                if text:
                    return text
    return ""


def _record_id(record: dict[str, Any]) -> str:
    descriptive = _descriptive(record)
    raw_id = _first_text(
        descriptive.get("record_ID"),
        descriptive.get("record_id"),
        record.get("record_ID"),
        record.get("record_id"),
    )
    if raw_id:
        return raw_id.removeprefix("edanmdm:")

    raw = _first_text(record.get("id"), record.get("url"))
    if raw.startswith("edanmdm:"):
        return raw.split(":", 1)[1]
    return raw


def _content_id(record: dict[str, Any]) -> str:
    raw = _first_text(record.get("id"), record.get("url"), _record_id(record))
    if raw.startswith("edanmdm:"):
        return raw
    record_id = _record_id(record)
    return f"edanmdm:{record_id}" if record_id else raw


def _object_url(record_id: str) -> str:
    return f"{_SITE_BASE}/{quote(record_id, safe='._-')}"


def _unit_code(record: dict[str, Any]) -> str:
    descriptive = _descriptive(record)
    return _first_text(
        record.get("unitCode"),
        record.get("unit_code"),
        descriptive.get("unit_code"),
        descriptive.get("unitCode"),
    )


def _data_source(record: dict[str, Any]) -> str:
    return _first_text(
        _descriptive(record).get("data_source"),
        _descriptive(record).get("dataSource"),
        _freetext_label(record, "Data Source"),
    )


def _object_type(record: dict[str, Any]) -> str:
    structured = _structured(record)
    return _first_text(
        structured.get("object_type"),
        structured.get("objectType"),
        _freetext_values(record, "objectType"),
        _freetext_label(record, "Type", "Object Type"),
    )


def _media_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    media_root = _descriptive(record).get("online_media")
    if not isinstance(media_root, dict):
        return []
    return [item for item in _as_list(media_root.get("media")) if isinstance(item, dict)]


def _image_url(record: dict[str, Any]) -> str:
    for item in _media_items(record):
        for key in ("content", "thumbnail", "url", "guid"):
            value = _clean_text(item.get(key))
            if value.startswith(("http://", "https://")):
                return value
        for resource in _as_list(item.get("resources")):
            if not isinstance(resource, dict):
                continue
            value = _first_text(resource.get("url"), resource.get("content"))
            if value.startswith(("http://", "https://")):
                return value
        ids_id = _clean_text(item.get("idsId") or item.get("ids_id"))
        if ids_id:
            return f"https://ids.si.edu/ids/deliveryService?id={quote(ids_id, safe='')}"
    return ""


def _license(record: dict[str, Any]) -> str:
    content = _content(record)
    descriptive = _descriptive(record)
    media_usage = content.get("media_usage")
    metadata_usage = descriptive.get("metadata_usage")
    return _first_text(
        media_usage.get("access") if isinstance(media_usage, dict) else None,
        metadata_usage.get("access") if isinstance(metadata_usage, dict) else None,
        _freetext_label(
            record,
            "Restrictions & Rights",
            "Metadata Usage",
            "Usage Conditions",
            "Rights",
        ),
        _freetext_values(record, "usage_flag"),
    )


def _title(record: dict[str, Any]) -> str:
    descriptive = _descriptive(record)
    title = descriptive.get("title")
    return _first_text(
        record.get("title"),
        title.get("content") if isinstance(title, dict) else title,
        _freetext_label(record, "Title"),
        _record_id(record) and f"Smithsonian object {_record_id(record)}",
    )


def _summary(record: dict[str, Any]) -> str:
    return _first_text(
        _freetext_label(record, "Summary", "Description", "Brief Description"),
        _freetext_values(record, "notes"),
        _freetext_values(record, "physicalDescription"),
    )


def _metadata(record: dict[str, Any]) -> dict[str, Any]:
    media = _media_items(record)
    return {
        "smithsonian_id": _content_id(record),
        "record_id": _record_id(record),
        "unit_code": _unit_code(record),
        "object_type": _object_type(record),
        "image_url": _image_url(record),
        "license": _license(record),
        "data_source": _data_source(record),
        "online_media_count": len(media),
        "fetched_via": KIND,
    }


def _snippet(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    parts: list[str] = []
    if metadata["data_source"]:
        parts.append(metadata["data_source"])
    if metadata["unit_code"]:
        parts.append(f"Unit: {metadata['unit_code']}")
    if metadata["object_type"]:
        parts.append(f"Type: {metadata['object_type']}")
    if metadata["license"]:
        parts.append(f"License: {metadata['license']}")
    summary = _summary(record)
    if summary:
        parts.append(_truncate(summary, 220))
    return " | ".join(parts) or _title(record)


def _search_rows(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if payload is None:
        return []
    response = payload.get("response")
    if not isinstance(response, dict):
        return []
    rows = response.get("rows")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _result_from_record(record: dict[str, Any]) -> SearchResult | None:
    record_id = _record_id(record)
    title = _title(record)
    if not record_id or not title:
        return None

    metadata = _metadata(record)
    url = _object_url(record_id)
    score: float | None = None
    raw_score = record.get("score")
    if raw_score not in (None, ""):
        try:
            score = float(str(raw_score))
        except (TypeError, ValueError):
            score = None

    return SearchResult(
        url=url,
        title=title,
        snippet=_snippet(record, metadata),
        published_at=_parse_date(
            _structured(record).get("date") or _freetext_values(record, "date")
        ),
        source_kind=KIND,
        score=score,
        extras={**metadata, "raw_id": _first_text(record.get("id"))},
    )


async def search(
    query: str,
    *,
    max_results: int = 20,
    timeout: float = 20.0,
) -> list[SearchResult]:
    """Search Smithsonian Open Access objects and return public object URLs."""
    q = (query or "").strip()
    if not q or max_results <= 0:
        return []

    rows = min(max(1, int(max_results)), _MAX_ROWS)
    payload = await _request_json(
        _SEARCH_URL,
        params={"api_key": _resolve_api_key(), "q": q, "rows": rows},
        timeout=timeout,
    )

    results: list[SearchResult] = []
    seen: set[str] = set()
    for record in _search_rows(payload):
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


def _normalize_object_id(raw: str) -> str:
    text = unquote(raw).strip().strip("/")
    if not text:
        return ""
    if ":" in text:
        text = text.rsplit(":", 1)[1]
    text = text.split("?", 1)[0].split("#", 1)[0].strip()
    return text if _RECORD_ID_RE.match(text) else ""


def _extract_content_id(url: str) -> str:
    if not _is_supported_host(url):
        return ""
    parsed = urlparse(url)
    path = unquote(parsed.path or "")

    api_match = re.search(
        r"/openaccess/api/v1\.0/content/(?P<id>[^/?#]+)", path
    )
    if api_match:
        raw_id = api_match.group("id").strip()
        if raw_id.startswith("edanmdm:"):
            return raw_id
        normalized = _normalize_object_id(raw_id)
        return f"edanmdm:{normalized}" if normalized else ""

    query = parse_qs(parsed.query)
    for key in ("id", "record_ID", "record_id"):
        values = query.get(key)
        if values:
            normalized = _normalize_object_id(values[0])
            if normalized:
                return f"edanmdm:{normalized}"

    if path.startswith("/object/"):
        tail = path.rsplit("/", 1)[-1]
        normalized = _normalize_object_id(tail)
        if normalized:
            return f"edanmdm:{normalized}"

    return ""


def _record_from_content_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    response = payload.get("response")
    if isinstance(response, dict):
        rows = response.get("rows")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
        if "content" in response or "title" in response or "id" in response:
            return response
    if "content" in payload or "title" in payload or "id" in payload:
        return payload
    return None


async def _fetch_content_record(
    content_id: str,
    *,
    timeout: float,
) -> dict[str, Any] | None:
    quoted = quote(content_id, safe=":")
    payload = await _request_json(
        f"{_CONTENT_BASE_URL}/{quoted}",
        params={"api_key": _resolve_api_key()},
        timeout=timeout,
    )
    record = _record_from_content_payload(payload)
    if record is not None:
        return record

    if not content_id.startswith("edanmdm:"):
        return None
    bare = content_id.split(":", 1)[1]
    payload = await _request_json(
        f"{_CONTENT_BASE_URL}/{quote(bare, safe='')}",
        params={"api_key": _resolve_api_key()},
        timeout=timeout,
    )
    return _record_from_content_payload(payload)


def _source_markdown(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    title = _title(record)
    lines = [f"# {title}", ""]
    if metadata["record_id"]:
        lines.append(f"- Record ID: {metadata['record_id']}")
    if metadata["unit_code"]:
        lines.append(f"- Unit code: {metadata['unit_code']}")
    if metadata["data_source"]:
        lines.append(f"- Data source: {metadata['data_source']}")
    if metadata["object_type"]:
        lines.append(f"- Object type: {metadata['object_type']}")
    if metadata["license"]:
        lines.append(f"- License: {metadata['license']}")
    if metadata["image_url"]:
        lines.append(f"- Image URL: {metadata['image_url']}")

    summary = _summary(record)
    if summary:
        lines.extend(["", "## Summary", summary])

    freetext = _freetext(record)
    detail_lines: list[str] = []
    for key, entries in freetext.items():
        if key in {"notes", "objectType", "date"}:
            continue
        for item in _as_list(entries):
            label = _clean_text(item.get("label")) if isinstance(item, dict) else key
            text = _clean_text(item.get("content")) if isinstance(item, dict) else _clean_text(item)
            if not text:
                continue
            if label:
                detail_lines.append(f"- {label}: {text}")
            else:
                detail_lines.append(f"- {text}")
            if len(detail_lines) >= 20:
                break
        if len(detail_lines) >= 20:
            break
    if detail_lines:
        lines.extend(["", "## Object Details", *detail_lines])

    return "\n".join(lines).strip()


async def fetch(url: str, *, timeout: float = 20.0) -> Source | None:
    """Fetch Smithsonian object metadata from an object or content API URL."""
    content_id = _extract_content_id(url)
    if not content_id:
        return None

    record = await _fetch_content_record(content_id, timeout=timeout)
    if record is None:
        return None

    metadata = _metadata(record)
    title = _title(record)
    record_id = metadata["record_id"] or content_id.removeprefix("edanmdm:")
    return Source(
        url=_object_url(record_id),
        title=title,
        cleaned_text=_source_markdown(record, metadata),
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind=KIND,
        metadata=metadata,
    )


def reset_for_tests() -> None:
    """Clear process-local rate-limit state. Test-only."""
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
    host_patterns=("api.si.edu", "si.edu", "www.si.edu", "3d.si.edu"),
    skill_name="smithsonian",
    description=(
        "Smithsonian Open Access digitized collection objects, museum"
        " artifacts, images, 3D assets, and object metadata via api.data.gov"
    ),
    optional_payload_knobs="`max_results`",
    example_query="Apollo 11",
    module_name="smithsonian",
)


__all__ = ["KIND", "fetch", "reset_for_tests", "search"]
