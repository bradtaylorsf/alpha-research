"""National Archives Catalog OPA v2 connector (issue #226).

Public surface:

* ``async def search(query, *, max_results=20, **knobs) -> list[SearchResult]``
  hits ``https://catalog.archives.gov/api/v2/records/search`` with the
  required ``x-api-key`` header from ``NARA_API_KEY``.
* ``async def fetch(url) -> Source | None`` accepts Catalog detail URLs such
  as ``https://catalog.archives.gov/id/<NAID>`` or API search URLs with a
  ``naIds=`` query parameter, re-queries OPA v2, and renders the archival
  metadata as a citeable source.

NARA's v2 API is free but key-gated in practice: request a key by emailing
Catalog_API@nara.gov, then keep usage under the default 10,000 queries/month
cap. This connector enforces a conservative 0.5 RPS process-local gate and
returns ``[]`` / ``None`` with a clear warning when ``NARA_API_KEY`` is unset.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from research_agent import config
from research_agent.tools import archive
from research_agent.tools._registry import (
    BaseSearchPayload as _BaseSearchPayload,
)
from research_agent.tools._registry import (
    register_kind as _register_kind,
)
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

KIND = "nara_search"

_SEARCH_URL = "https://catalog.archives.gov/api/v2/records/search"
_SITE_BASE = "https://catalog.archives.gov"
_HOST = "catalog.archives.gov"
# AC: polite per-host rate at 0.5 RPS.
_RATE_LIMIT_INTERVAL = 2.0
_MAX_ROWS = 100

_DETAIL_URL_RE = re.compile(r"^/id/(?P<naid>\d+)(?:/)?$")
_WS_RE = re.compile(r"\s+")

_SAFE_EXTRA_PARAMS = frozenset(
    {
        "availableOnline",
        "cursorMark",
        "exists",
        "naIds",
        "not_exist",
        "objectIds",
        "offset",
        "recordGroupNumber",
        "resultFields",
        "resultTypes",
        "sort",
        "typeOfMaterials",
    }
)
_SNAKE_TO_API_PARAM = {
    "available_online": "availableOnline",
    "cursor_mark": "cursorMark",
    "na_ids": "naIds",
    "object_ids": "objectIds",
    "record_group": "recordGroupNumber",
    "record_group_number": "recordGroupNumber",
    "result_fields": "resultFields",
    "result_types": "resultTypes",
    "type_of_materials": "typeOfMaterials",
}

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None
_wayback_attempted_urls: set[str] = set()


def _api_key() -> str:
    return (config.get("NARA_API_KEY") or "").strip()


def _missing_key_message() -> str:
    return "nara_search: would need NARA_API_KEY; skipping"


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": config.get("RESEARCH_USER_AGENT")
        or "research-agent/0.1 (+local; contact unset)",
        "x-api-key": api_key,
    }


async def _rate_limit_gate() -> None:
    global _last_call_monotonic
    async with _rate_lock:
        if _last_call_monotonic is not None:
            elapsed = time.monotonic() - _last_call_monotonic
            wait = _RATE_LIMIT_INTERVAL - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
        _last_call_monotonic = time.monotonic()


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _WS_RE.sub(" ", value).strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in (
            "logicalDate",
            "status",
            "termName",
            "title",
            "heading",
            "name",
            "value",
            "description",
        ):
            text = _clean_text(value.get(key))
            if text:
                return text
    if isinstance(value, list):
        return "; ".join(
            part for item in value if (part := _clean_text(item))
        )
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


def _truncate(text: str, limit: int = 300) -> str:
    text = _clean_text(text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _parse_nara_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, list):
        for item in value:
            parsed = _parse_nara_date(item)
            if parsed is not None:
                return parsed
        return None
    if isinstance(value, dict):
        logical = _clean_text(value.get("logicalDate"))
        if logical:
            parsed = _parse_nara_date(logical)
            if parsed is not None:
                return parsed
        year = value.get("year")
        if isinstance(year, int):
            month = value.get("month") if isinstance(value.get("month"), int) else 1
            day = value.get("day") if isinstance(value.get("day"), int) else 1
            try:
                return datetime(year, month, day, tzinfo=UTC)
            except ValueError:
                return None
        return None
    text = str(value).strip()
    if not text:
        return None
    head = text.split("T", 1)[0].strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(head, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _record_naid(record: dict[str, Any], *, fallback: Any = None) -> str:
    return _first_text(record.get("naId"), record.get("naid"), fallback)


def _detail_url(naid: str) -> str:
    return f"{_SITE_BASE}/id/{naid}"


def _record_group(record: dict[str, Any]) -> str:
    direct = _first_text(
        record.get("recordGroupNumber"),
        record.get("recordGroupNo"),
        record.get("recordGroup"),
    )
    title = ""
    if isinstance(record.get("recordGroup"), dict):
        rg = record["recordGroup"]
        direct = _first_text(direct, rg.get("recordGroupNumber"), rg.get("naId"))
        title = _first_text(rg.get("title"), rg.get("name"))

    for ancestor in _as_list(record.get("ancestors")):
        if not isinstance(ancestor, dict):
            continue
        level = _clean_text(ancestor.get("levelOfDescription")).lower()
        has_rg_number = bool(
            _first_text(
                ancestor.get("recordGroupNumber"),
                ancestor.get("recordGroupNo"),
            )
        )
        if "recordgroup" not in level and "record group" not in level and not has_rg_number:
            continue
        direct = _first_text(
            direct,
            ancestor.get("recordGroupNumber"),
            ancestor.get("recordGroupNo"),
        )
        title = _first_text(title, ancestor.get("title"))
        break

    if direct and title and title not in direct:
        return f"RG {direct}: {title}"
    if direct:
        return direct if direct.upper().startswith("RG ") else f"RG {direct}"
    return title


def _series_title(record: dict[str, Any]) -> str:
    if _clean_text(record.get("levelOfDescription")).lower() == "series":
        return _clean_text(record.get("title"))
    series: list[tuple[int, str]] = []
    for ancestor in _as_list(record.get("ancestors")):
        if not isinstance(ancestor, dict):
            continue
        level = _clean_text(ancestor.get("levelOfDescription")).lower()
        if "series" not in level:
            continue
        title = _clean_text(ancestor.get("title"))
        if title:
            distance = ancestor.get("distance")
            series.append((distance if isinstance(distance, int) else 999, title))
    if not series:
        return ""
    series.sort(key=lambda item: item[0])
    return series[0][1]


def _scope_and_content(record: dict[str, Any]) -> str:
    return _first_text(
        record.get("scopeAndContentNote"),
        record.get("scopeAndContent"),
        record.get("scope_and_content"),
        record.get("summary"),
        record.get("description"),
    )


def _restriction_status(record: dict[str, Any], *keys: str) -> str:
    for key in keys:
        raw = record.get(key)
        if isinstance(raw, dict):
            status = _first_text(raw.get("status"), raw.get("termName"), raw.get("name"))
            if status:
                return status
        text = _clean_text(raw)
        if text:
            return text
    general = record.get("general_records_information")
    if isinstance(general, dict):
        for key in keys:
            snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
            text = _clean_text(general.get(snake))
            if text:
                return text
    return ""


def _digital_objects(record: dict[str, Any]) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for obj in _as_list(record.get("digitalObjects")):
        if not isinstance(obj, dict):
            continue
        url = _first_text(obj.get("objectUrl"), obj.get("url"))
        if not url:
            continue
        entry = {
            "url": url,
            "type": _first_text(obj.get("objectType"), obj.get("type")),
            "filename": _first_text(obj.get("objectFilename"), obj.get("filename")),
            "object_id": _first_text(obj.get("objectId"), obj.get("id")),
        }
        objects.append({k: v for k, v in entry.items() if v})
    return objects


def _hierarchy(record: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for ancestor in _as_list(record.get("ancestors")):
        if not isinstance(ancestor, dict):
            continue
        row = {
            "level": _clean_text(ancestor.get("levelOfDescription")),
            "title": _clean_text(ancestor.get("title")),
            "naId": _first_text(ancestor.get("naId"), ancestor.get("naid")),
        }
        rows.append({k: v for k, v in row.items() if v})
    return rows


def _search_hits(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    body = payload.get("body")
    if isinstance(body, dict):
        hits = body.get("hits")
        if isinstance(hits, dict):
            return [hit for hit in _as_list(hits.get("hits")) if isinstance(hit, dict)]

    hits = payload.get("hits")
    if isinstance(hits, dict):
        raw_hits = hits.get("hits")
    else:
        raw_hits = hits
    return [hit for hit in _as_list(raw_hits) if isinstance(hit, dict)]


def _record_from_hit(hit: dict[str, Any]) -> dict[str, Any]:
    source = hit.get("_source")
    if isinstance(source, dict) and isinstance(source.get("record"), dict):
        return source["record"]
    if isinstance(hit.get("record"), dict):
        return hit["record"]
    return {}


def _build_result(hit: dict[str, Any]) -> SearchResult | None:
    record = _record_from_hit(hit)
    naid = _record_naid(record, fallback=hit.get("_id"))
    if not naid:
        return None

    title = _first_text(record.get("title"), f"NARA record {naid}")
    scope = _scope_and_content(record)
    record_group = _record_group(record)
    series_title = _series_title(record)
    access_restriction = _restriction_status(record, "accessRestriction")
    use_restriction = _restriction_status(record, "useRestriction")
    object_rows = _digital_objects(record)

    snippet_parts = [
        part
        for part in (
            record_group,
            series_title,
            access_restriction and f"Access: {access_restriction}",
            scope,
        )
        if part
    ]
    snippet = _truncate(" | ".join(snippet_parts) or title)

    score = hit.get("_score")
    numeric_score = float(score) if isinstance(score, (int, float)) else None
    extras: dict[str, Any] = {
        "nara_record_id": naid,
        "record_group": record_group,
        "series_title": series_title,
        "scope_and_content": scope,
        "access_restriction": access_restriction,
        "use_restriction": use_restriction,
        "level_of_description": _clean_text(record.get("levelOfDescription")),
        "digital_objects": object_rows,
        "local_identifier": _clean_text(record.get("localIdentifier")),
        "general_records_types": _as_list(record.get("generalRecordsTypes")),
    }

    return SearchResult(
        url=_detail_url(naid),
        title=title,
        snippet=snippet,
        published_at=_parse_nara_date(
            record.get("productionDates")
            or record.get("inclusiveStartDate")
            or record.get("date")
        ),
        source_kind=KIND,  # type: ignore[arg-type]
        score=numeric_score,
        extras={k: v for k, v in extras.items() if v not in ("", [], None)},
    )


def _normalize_bool(value: bool | str | int) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return "true" if value else "false"
    text = str(value).strip().lower()
    return "true" if text in {"1", "true", "yes", "y"} else "false"


def _params_from_knobs(
    knobs: dict[str, Any],
    *,
    rows: int,
    page: int | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"rows": rows}
    if page is not None:
        params["offset"] = max(0, int(page) - 1) * rows

    for raw_key, value in knobs.items():
        if value is None:
            continue
        api_key = _SNAKE_TO_API_PARAM.get(raw_key, raw_key)
        if api_key not in _SAFE_EXTRA_PARAMS:
            logger.warning("nara_search: ignoring unsupported knob %s", raw_key)
            continue
        if api_key == "availableOnline":
            params[api_key] = _normalize_bool(value)
        elif isinstance(value, (list, tuple, set)):
            params[api_key] = ",".join(str(v) for v in value if v is not None)
        else:
            params[api_key] = value
    return params


async def _request_json(
    *,
    params: dict[str, Any],
    timeout: float,
) -> dict[str, Any] | None:
    api_key = _api_key()
    if not api_key:
        logger.warning(_missing_key_message())
        return None

    await _rate_limit_gate()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(api_key),
        ) as client:
            response = await client.get(_SEARCH_URL, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("nara_search: request failed: %s", exc)
        return None

    if response.status_code == 429:
        logger.warning("nara_search: NARA API rate limit returned HTTP 429")
        return None
    if 400 <= response.status_code < 500:
        logger.warning("nara_search: NARA API returned HTTP %s", response.status_code)
        return None
    if response.status_code >= 500:
        logger.warning("nara_search: NARA API server error HTTP %s", response.status_code)
        return None

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("nara_search: response was not JSON: %s", exc)
        return None
    return payload if isinstance(payload, dict) else None


async def search(
    query: str,
    *,
    max_results: int = 20,
    page: int | None = None,
    timeout: float = 20.0,
    **knobs: Any,
) -> list[SearchResult]:
    """Search NARA OPA v2 records and return Catalog detail-page hits.

    Supported knobs are translated to OPA v2 query parameters:
    ``available_online``/``availableOnline``, ``type_of_materials``/
    ``typeOfMaterials``, ``result_types``/``resultTypes``,
    ``record_group``/``recordGroupNumber``, ``sort``, ``offset``,
    ``na_ids``/``naIds``, ``object_ids``/``objectIds``, ``exists``,
    ``not_exist``, ``result_fields``/``resultFields``, and ``cursor_mark``.
    Unsupported knobs are logged and ignored.
    """
    q = (query or "").strip()
    if not q:
        return []

    if not _api_key():
        logger.warning(_missing_key_message())
        return []

    rows = min(max(1, int(max_results)), _MAX_ROWS)
    params = _params_from_knobs(knobs, rows=rows, page=page)
    params["q"] = q

    payload = await _request_json(params=params, timeout=timeout)
    if payload is None:
        return []

    results: list[SearchResult] = []
    for hit in _search_hits(payload):
        result = _build_result(hit)
        if result is not None:
            results.append(result)
        if len(results) >= rows:
            break
    return results


def _extract_naid_from_url(url: str) -> tuple[str, bool]:
    parsed = urlparse(url)
    host = parsed.netloc.split("@")[-1].split(":")[0].lower()
    if host != _HOST:
        return "", False

    match = _DETAIL_URL_RE.match(parsed.path)
    if match:
        return match.group("naid"), True

    if parsed.path.rstrip("/") == "/api/v2/records/search":
        query = parse_qs(parsed.query)
        for key in ("naIds", "naids", "na_ids"):
            values = query.get(key)
            if values:
                first = values[0].split(",", 1)[0].strip()
                if first.isdigit():
                    return first, False
    return "", False


async def _fetch_record_by_naid(naid: str, *, timeout: float) -> dict[str, Any] | None:
    payload = await _request_json(
        params={"naIds": naid, "rows": 1},
        timeout=timeout,
    )
    if payload is None:
        return None
    hits = _search_hits(payload)
    if not hits:
        return None
    return _record_from_hit(hits[0])


def _metadata_for_record(record: dict[str, Any], naid: str) -> dict[str, Any]:
    record_group = _record_group(record)
    series_title = _series_title(record)
    scope = _scope_and_content(record)
    metadata: dict[str, Any] = {
        "nara_record_id": naid,
        "record_group": record_group,
        "series_title": series_title,
        "scope_and_content": scope,
        "access_restriction": _restriction_status(record, "accessRestriction"),
        "use_restriction": _restriction_status(record, "useRestriction"),
        "level_of_description": _clean_text(record.get("levelOfDescription")),
        "local_identifier": _clean_text(record.get("localIdentifier")),
        "general_records_types": _as_list(record.get("generalRecordsTypes")),
        "production_dates": _as_list(record.get("productionDates")),
        "digital_objects": _digital_objects(record),
        "hierarchy": _hierarchy(record),
    }
    return {k: v for k, v in metadata.items() if v not in ("", [], None)}


def _render_source_text(record: dict[str, Any], metadata: dict[str, Any]) -> str:
    title = _first_text(record.get("title"), f"NARA record {metadata['nara_record_id']}")
    lines = [
        f"# {title}",
        "",
        f"National Archives Identifier: {metadata['nara_record_id']}",
    ]
    if level := metadata.get("level_of_description"):
        lines.append(f"Level of description: {level}")
    if local_id := metadata.get("local_identifier"):
        lines.append(f"Local identifier: {local_id}")
    if record_group := metadata.get("record_group"):
        lines.append(f"Record group: {record_group}")
    if series_title := metadata.get("series_title"):
        lines.append(f"Series: {series_title}")
    if access := metadata.get("access_restriction"):
        lines.append(f"Access restriction: {access}")
    if use := metadata.get("use_restriction"):
        lines.append(f"Use restriction: {use}")

    scope = metadata.get("scope_and_content")
    if isinstance(scope, str) and scope:
        lines.extend(["", "## Scope and Content", scope])

    hierarchy = metadata.get("hierarchy")
    if isinstance(hierarchy, list) and hierarchy:
        lines.extend(["", "## Hierarchy"])
        for row in hierarchy:
            if not isinstance(row, dict):
                continue
            label = " - ".join(
                part
                for part in (
                    _clean_text(row.get("level")),
                    _clean_text(row.get("title")),
                    _clean_text(row.get("naId")) and f"NAID {row['naId']}",
                )
                if part
            )
            if label:
                lines.append(f"- {label}")

    objects = metadata.get("digital_objects")
    if isinstance(objects, list) and objects:
        lines.extend(["", "## Digital Objects"])
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            label = _first_text(obj.get("filename"), obj.get("type"), obj.get("url"))
            obj_url = _clean_text(obj.get("url"))
            if obj_url:
                lines.append(f"- {label}: {obj_url}")

    return "\n".join(lines).strip()


def _spawn_wayback_save(url: str) -> None:
    if url in _wayback_attempted_urls:
        return
    _wayback_attempted_urls.add(url)
    try:
        asyncio.get_running_loop().create_task(archive.save(url))
    except RuntimeError:
        pass


async def fetch(url: str, *, timeout: float = 20.0) -> Source | None:
    """Fetch a Catalog detail record by NAID and render archival metadata."""
    naid, should_archive = _extract_naid_from_url(url)
    if not naid:
        return None
    if not _api_key():
        logger.warning("nara_fetch: would need NARA_API_KEY; skipping")
        return None

    record = await _fetch_record_by_naid(naid, timeout=timeout)
    if not record:
        return None

    canonical = _detail_url(naid)
    metadata = _metadata_for_record(record, naid)
    title = _first_text(record.get("title"), f"NARA record {naid}")

    if should_archive:
        _spawn_wayback_save(canonical)

    return Source(
        url=canonical,
        title=title,
        cleaned_text=_render_source_text(record, metadata),
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind=KIND,  # type: ignore[arg-type]
        metadata=metadata,
    )


def reset_for_tests() -> None:
    """Clear process-local rate and Wayback state. Test-only."""
    global _last_call_monotonic, _rate_lock, _wayback_attempted_urls
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()
    _wayback_attempted_urls = set()


class _PayloadSchema(_BaseSearchPayload):
    max_results: int | None = None
    page: int | None = None
    available_online: bool | str | None = None
    type_of_materials: str | list[str] | None = None
    result_types: str | list[str] | None = None
    record_group: str | int | None = None
    sort: str | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=("catalog.archives.gov",),
    skill_name="nara",
    description=(
        "US National Archives Catalog OPA v2 records, declassified federal"
        " records, military records, photos; requires NARA_API_KEY"
    ),
    optional_payload_knobs=(
        "`available_online`, `type_of_materials`, `result_types`,"
        " `record_group`, `page`"
    ),
    example_query="Vietnam War declassified",
    module_name="nara",
)


__all__ = ["KIND", "fetch", "reset_for_tests", "search"]
