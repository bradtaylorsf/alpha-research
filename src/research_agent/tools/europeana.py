"""Europeana Search API connector (issue #229, A7).

Public surface:

* ``async def search(query, *, lang=None, max_results=20) -> list[SearchResult]``
  hits ``https://api.europeana.eu/api/v2/search.json`` with ``query=<query>``
  and the required ``wskey=<EUROPEANA_API_KEY>`` parameter.
* ``async def fetch(url) -> Source | None`` accepts Europeana item URLs and
  Record API URLs, rehydrates one metadata record by Europeana ID, and renders
  archival metadata as a citeable source.

Europeana keys are free but required. Since 2025-05-28, key registration lives
inside a Europeana account under Manage API keys. This connector intentionally
uses the issue-specified ``wskey`` query parameter and enforces a conservative
1 RPS process-local gate. When ``EUROPEANA_API_KEY`` is unset, it returns
``[]`` / ``None`` and logs a clear skip message.
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

KIND: Literal["europeana_search"] = "europeana_search"

_SEARCH_URL = "https://api.europeana.eu/api/v2/search.json"
_RECORD_URL_BASE = "https://api.europeana.eu/record/v2"
_SITE_BASE = "https://www.europeana.eu/en/item"
_HOSTS = frozenset({"api.europeana.eu", "europeana.eu", "www.europeana.eu"})
_RATE_LIMIT_INTERVAL = 1.0
_MAX_ROWS = 100
_WS_RE = re.compile(r"\s+")

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None
_missing_key_warned = False


def _api_key() -> str:
    return (config.get("EUROPEANA_API_KEY") or "").strip()


def _missing_key_message() -> str:
    return (
        "europeana_search: would need EUROPEANA_API_KEY; create a free key in "
        "your Europeana account under Manage API keys (migrated May 2025); "
        "skipping"
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

    request_params = {**params, "wskey": key}
    await _rate_limit_gate()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url, params=request_params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("europeana_search: request failed for %s: %s", url, exc)
        return None

    if response.status_code == 429:
        logger.warning("europeana_search: Europeana API rate limit returned HTTP 429")
        return None
    if 400 <= response.status_code < 500:
        logger.warning(
            "europeana_search: Europeana API returned HTTP %s",
            response.status_code,
        )
        return None
    if response.status_code >= 500:
        logger.warning(
            "europeana_search: Europeana API server error HTTP %s",
            response.status_code,
        )
        return None

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("europeana_search: Europeana API response was not JSON: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("success") is False:
        logger.warning(
            "europeana_search: Europeana API returned success=false: %s",
            _clean_text(payload.get("error")),
        )
        return None
    return payload


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
            "prefLabel",
            "label",
            "title",
            "value",
            "def",
            "en",
            "fr",
            "de",
            "es",
            "it",
            "nl",
            "description",
            "@id",
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


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _iter_dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in _as_list(value) if isinstance(item, dict)]


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
        for key in (
            "@id",
            "id",
            "url",
            "resource",
            "edmIsShownAt",
            "edmLandingPage",
            "thumbnail",
        ):
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


def _clean_europeana_id(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    text = unquote(text).split("?", 1)[0].split("#", 1)[0].strip()
    for prefix in ("record/v2/", "api/v2/record/", "item/"):
        if text.startswith(prefix):
            text = text.removeprefix(prefix)
            break
    for suffix in (".json", ".json-ld", ".rdf", ".html"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    text = text.strip("/")
    if not text or "/" not in text:
        return ""
    return f"/{text}"


def _is_supported_host(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    return (parsed.hostname or "").casefold() in _HOSTS


def _extract_europeana_id(url: str) -> str:
    if not _is_supported_host(url):
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = unquote(parsed.path or "").strip("/")

    if host == "api.europeana.eu":
        for prefix in ("record/v2/", "api/v2/record/"):
            if path.startswith(prefix):
                return _clean_europeana_id(path.removeprefix(prefix))
        query = parse_qs(parsed.query)
        for key in ("id", "europeana_id"):
            values = query.get(key)
            if values:
                return _clean_europeana_id(values[0])
        return ""

    parts = [part for part in path.split("/") if part]
    if "item" in parts:
        index = parts.index("item")
        return _clean_europeana_id("/".join(parts[index + 1 :]))
    if path.startswith("portal/record/"):
        return _clean_europeana_id(path.removeprefix("portal/record/"))
    return ""


def _europeana_id(record: dict[str, Any]) -> str:
    return _clean_europeana_id(
        _first_text(record.get("id"), record.get("about"), record.get("@id"))
    )


def _site_url(europeana_id: str) -> str:
    return f"{_SITE_BASE}/{quote(europeana_id.strip('/'), safe='/')}"


def _record_api_url(europeana_id: str) -> str:
    return f"{_RECORD_URL_BASE}/{quote(europeana_id.strip('/'), safe='/')}.json"


def _proxy_text(record: dict[str, Any], *keys: str) -> str:
    for proxy in _iter_dicts(record.get("proxies")):
        for key in keys:
            text = _clean_text(proxy.get(key))
            if text:
                return text
    return ""


def _proxy_join(record: dict[str, Any], *keys: str, limit: int = 5) -> str:
    parts: list[str] = []
    for proxy in _iter_dicts(record.get("proxies")):
        for key in keys:
            for item in _as_list(proxy.get(key)):
                text = _clean_text(item)
                if text and text not in parts:
                    parts.append(text)
                if len(parts) >= limit:
                    return "; ".join(parts)
    return "; ".join(parts)


def _aggregation_text(record: dict[str, Any], *keys: str) -> str:
    for collection_key in ("aggregations", "europeanaAggregation"):
        for aggregation in _iter_dicts(record.get(collection_key)):
            for key in keys:
                text = _clean_text(aggregation.get(key))
                if text:
                    return text
    return ""


def _aggregation_url(record: dict[str, Any], *keys: str) -> str:
    for collection_key in ("aggregations", "europeanaAggregation"):
        for aggregation in _iter_dicts(record.get(collection_key)):
            for key in keys:
                url = _first_url(aggregation.get(key))
                if url:
                    return url
    return ""


def _web_resource_text(record: dict[str, Any], *keys: str) -> str:
    for resource in _iter_dicts(record.get("webResources")):
        for key in keys:
            text = _clean_text(resource.get(key))
            if text:
                return text
    return ""


def _title(record: dict[str, Any]) -> str:
    europeana_id = _europeana_id(record)
    return _first_text(
        record.get("title"),
        record.get("dcTitle"),
        _proxy_text(record, "dcTitle", "title"),
        europeana_id and f"Europeana item {europeana_id.strip('/')}",
    )


def _description(record: dict[str, Any]) -> str:
    return _first_text(
        record.get("dcDescription"),
        record.get("description"),
        _proxy_text(record, "dcDescription", "description"),
    )


def _creator(record: dict[str, Any]) -> str:
    return _first_text(
        record.get("dcCreator"),
        record.get("creator"),
        _proxy_text(record, "dcCreator", "creator"),
    )


def _metadata(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "europeana_id": _europeana_id(record),
        "dataProvider": _first_text(
            record.get("dataProvider"),
            _aggregation_text(record, "edmDataProvider", "dataProvider"),
        ),
        "country": _join_text(record.get("country"), limit=4),
        "language": _join_text(record.get("language"), limit=4),
        "rights": _first_text(
            record.get("rights"),
            _aggregation_text(record, "edmRights", "rights"),
            _web_resource_text(record, "webResourceEdmRights", "rights"),
        ),
        "edmIsShownAt": _first_url(record.get("edmIsShownAt"))
        or _aggregation_url(record, "edmIsShownAt"),
        "provider": _first_text(
            record.get("provider"),
            _aggregation_text(record, "edmProvider", "provider"),
        ),
        "type": _first_text(record.get("type")),
        "year": _join_text(record.get("year"), limit=3),
        "edmPreview": _first_url(record.get("edmPreview")),
        "fetched_via": KIND,
    }


def _snippet(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    parts: list[str] = []
    if metadata["dataProvider"]:
        parts.append(f"Data provider: {metadata['dataProvider']}")
    if metadata["country"]:
        parts.append(f"Country: {metadata['country']}")
    if metadata["language"]:
        parts.append(f"Language: {metadata['language']}")
    if metadata["year"]:
        parts.append(f"Year: {metadata['year']}")
    if metadata["type"]:
        parts.append(f"Type: {metadata['type']}")
    if metadata["rights"]:
        parts.append(f"Rights: {_truncate(metadata['rights'], 120)}")
    description = _description(record)
    if description:
        parts.append(_truncate(description, 220))
    return " | ".join(parts) or _title(record)


def _items(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if payload is None:
        return []
    items = payload.get("items")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return []


def _record_from_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    obj = payload.get("object")
    if isinstance(obj, dict):
        return obj
    items = _items(payload)
    if items:
        return items[0]
    if "id" in payload or "about" in payload or "dataProvider" in payload:
        return payload
    return None


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, dict):
        for item in value.values():
            parsed = _parse_date(item)
            if parsed is not None:
                return parsed
        return None
    if isinstance(value, (list, tuple, set)):
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
    match = re.search(r"\b(1[0-9]\d{2}|20\d{2})\b", text)
    if match:
        try:
            return datetime(int(match.group(1)), 1, 1, tzinfo=UTC)
        except ValueError:
            return None
    return None


def _result_from_record(record: dict[str, Any]) -> SearchResult | None:
    europeana_id = _europeana_id(record)
    title = _title(record)
    if not europeana_id or not title:
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
        url=_site_url(europeana_id),
        title=title,
        snippet=_snippet(record, metadata),
        published_at=_parse_date(record.get("year") or _proxy_text(record, "dcDate")),
        source_kind=KIND,
        score=score,
        extras=metadata,
    )


async def search(
    query: str,
    *,
    lang: str | None = None,
    max_results: int = 20,
    timeout: float = 20.0,
) -> list[SearchResult]:
    """Search Europeana item metadata and return Europeana item-page URLs."""
    q = (query or "").strip()
    if not q or max_results <= 0:
        return []

    rows = min(max(1, int(max_results)), _MAX_ROWS)
    params: dict[str, Any] = {"query": q, "rows": rows}
    if lang:
        params["qf"] = f"LANGUAGE:{lang.strip()}"

    payload = await _request_json(_SEARCH_URL, params=params, timeout=timeout)

    results: list[SearchResult] = []
    seen: set[str] = set()
    for record in _items(payload):
        result = _result_from_record(record)
        if result is None or result.url in seen:
            continue
        seen.add(result.url)
        results.append(result)
        if len(results) >= max_results:
            break
    return results


async def _fetch_record(
    europeana_id: str,
    *,
    timeout: float,
) -> dict[str, Any] | None:
    payload = await _request_json(
        _record_api_url(europeana_id),
        params={},
        timeout=timeout,
    )
    return _record_from_payload(payload)


def _source_markdown(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    lines = [f"# {_title(record)}", ""]
    if metadata["europeana_id"]:
        lines.append(f"- Europeana ID: {metadata['europeana_id']}")
    if metadata["dataProvider"]:
        lines.append(f"- Data provider: {metadata['dataProvider']}")
    if metadata["provider"]:
        lines.append(f"- Provider: {metadata['provider']}")
    if metadata["country"]:
        lines.append(f"- Country: {metadata['country']}")
    if metadata["language"]:
        lines.append(f"- Language: {metadata['language']}")
    if metadata["year"]:
        lines.append(f"- Year: {metadata['year']}")
    if metadata["type"]:
        lines.append(f"- Type: {metadata['type']}")
    if metadata["rights"]:
        lines.append(f"- Rights: {metadata['rights']}")
    if metadata["edmIsShownAt"]:
        lines.append(f"- Provider item URL: {metadata['edmIsShownAt']}")
    if metadata["edmPreview"]:
        lines.append(f"- Preview: {metadata['edmPreview']}")

    description = _description(record)
    if description:
        lines.extend(["", "## Description", description])

    creator = _creator(record)
    if creator:
        lines.extend(["", "## Creator", creator])

    detail_lines: list[str] = []
    for label, keys in (
        ("Subject", ("dcSubject", "dctermsSubject")),
        ("Date", ("dcDate", "dctermsCreated", "dctermsIssued")),
        ("Format", ("dcFormat",)),
        ("Source", ("dcSource",)),
        ("Coverage", ("dcCoverage",)),
    ):
        text = _proxy_join(record, *keys, limit=6)
        if text:
            detail_lines.append(f"- {label}: {text}")
    if detail_lines:
        lines.extend(["", "## Metadata", *detail_lines])

    return "\n".join(lines).strip()


async def fetch(url: str, *, timeout: float = 20.0) -> Source | None:
    """Fetch one Europeana item metadata record from an item/API URL."""
    europeana_id = _extract_europeana_id(url)
    if not europeana_id:
        return None

    record = await _fetch_record(europeana_id, timeout=timeout)
    if record is None:
        return None

    metadata = _metadata(record)
    resolved_id = metadata["europeana_id"] or europeana_id
    return Source(
        url=_site_url(resolved_id),
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
    lang: str | None = None
    timeout: float | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=("api.europeana.eu", "europeana.eu", "www.europeana.eu"),
    skill_name="europeana",
    description=(
        "Europeana multilingual European cultural-heritage item metadata across"
        " museums, libraries, and archives; requires EUROPEANA_API_KEY"
    ),
    optional_payload_knobs="`max_results`, `lang`",
    example_query="Algerian war 1954",
    module_name="europeana",
)


__all__ = ["KIND", "fetch", "reset_for_tests", "search"]
