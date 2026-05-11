"""Wikidata Query Service connector (issue #232, A10).

Public surface:

* ``async def search(query, *, max_results=20) -> list[SearchResult]`` runs a
  raw SPARQL query against ``https://query.wikidata.org/sparql`` and returns
  entity-shaped rows. v1 deliberately accepts raw SPARQL only; natural-language
  to SPARQL translation is a follow-on.
* ``async def fetch(url) -> Source | None`` resolves ``wikidata.org/wiki/Q...``
  URLs through ``Special:EntityData`` and returns labels, descriptions, claims,
  and sitelinks in ``Source.metadata``.

No auth required. WDQS rate limiting is query-CPU-time based rather than RPS:
one client (IP + User-Agent) gets 60 seconds of processing time per 60 seconds.
The connector tracks local wall-clock query duration as a conservative proxy
and backs off once the rolling minute reaches 50 seconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_agent import config
from research_agent.tools._registry import (
    BaseSearchPayload as _BaseSearchPayload,
)
from research_agent.tools._registry import (
    register_kind as _register_kind,
)
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

KIND = "wikidata_search"

_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
_ENTITY_DATA_BASE = "https://www.wikidata.org/wiki/Special:EntityData"
_PROJECT_URL = "https://github.com/bradtaylorsf/alpha-research"
_HOSTS = {"wikidata.org", "www.wikidata.org"}

_RATE_WINDOW_SECONDS = 60.0
_CPU_BUDGET_SECONDS = 60.0
_BACKOFF_THRESHOLD_SECONDS = _CPU_BUDGET_SECONDS - 10.0
_DEFAULT_RETRY_AFTER_SECONDS = 60.0

_QID_RE = re.compile(r"\bQ[1-9]\d*\b")
_EMAIL_RE = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+")

_rate_lock = asyncio.Lock()
_query_durations: deque[tuple[float, float]] = deque()


class _SparqlBindingValue(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    type: str | None = None
    value: Any
    datatype: str | None = None
    xml_lang: str | None = Field(default=None, alias="xml:lang")


class _SparqlResults(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bindings: list[dict[str, _SparqlBindingValue]]


class _SparqlResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    head: dict[str, Any]
    results: _SparqlResults


def _contact_from_user_agent() -> str:
    raw = config.get("RESEARCH_USER_AGENT") or ""
    match = _EMAIL_RE.search(raw)
    return match.group(0) if match else "unset"


def _user_agent() -> str:
    return (
        "research-agent/0.1 "
        f"(+{_PROJECT_URL}; contact: {_contact_from_user_agent()})"
    )


def _sparql_headers() -> dict[str, str]:
    return {
        "Accept": "application/sparql-results+json",
        "User-Agent": _user_agent(),
    }


def _json_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": _user_agent(),
    }


def _prune_durations(now: float) -> None:
    while _query_durations and now - _query_durations[0][0] >= _RATE_WINDOW_SECONDS:
        _query_durations.popleft()


async def _rate_limit_gate() -> None:
    """Wait until the rolling WDQS CPU budget is comfortably below 60s/min."""
    async with _rate_lock:
        while True:
            now = time.monotonic()
            _prune_durations(now)
            used = sum(duration for _, duration in _query_durations)
            if used < _BACKOFF_THRESHOLD_SECONDS:
                return
            if not _query_durations:
                return
            wait = max(_query_durations[0][0] + _RATE_WINDOW_SECONDS - now, 0.1)
            logger.info(
                "wikidata CPU-budget backoff: %.2fs used in rolling %.0fs; sleeping %.2fs",
                used,
                _RATE_WINDOW_SECONDS,
                wait,
            )
            await asyncio.sleep(wait)


async def _record_query_duration(duration: float) -> None:
    async with _rate_lock:
        now = time.monotonic()
        _prune_durations(now)
        _query_durations.append((now, max(duration, 0.0)))


def _parse_retry_after(value: str | None) -> float:
    if not value:
        return _DEFAULT_RETRY_AFTER_SECONDS
    text = value.strip()
    try:
        return max(float(text), 0.0)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_AFTER_SECONDS
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max((parsed - datetime.now(UTC)).total_seconds(), 0.0)


async def _request_sparql(
    query: str,
    *,
    timeout: float,
) -> dict[str, Any] | None:
    for attempt in range(2):
        await _rate_limit_gate()
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=timeout,
                headers=_sparql_headers(),
            ) as client:
                response = await client.post(_SPARQL_ENDPOINT, data={"query": query})
        except (httpx.HTTPError, OSError) as exc:
            await _record_query_duration(time.monotonic() - started)
            logger.warning("wikidata SPARQL request failed: %s", exc)
            return None

        await _record_query_duration(time.monotonic() - started)

        if response.status_code == 429:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            if attempt == 0:
                logger.warning(
                    "wikidata SPARQL returned 429; honoring Retry-After %.2fs",
                    retry_after,
                )
                await asyncio.sleep(retry_after)
                continue
            logger.warning("wikidata SPARQL returned repeated HTTP 429")
            return None

        if response.status_code != 200:
            logger.warning("wikidata SPARQL returned HTTP %s", response.status_code)
            return None

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("wikidata SPARQL returned non-JSON response: %s", exc)
            return None
        if not isinstance(payload, dict):
            logger.warning("wikidata SPARQL JSON root was %s", type(payload).__name__)
            return None
        return payload
    return None


def _entity_id_from_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    match = _QID_RE.search(value)
    return match.group(0) if match else ""


def _display_value(value: _SparqlBindingValue) -> str:
    raw = value.value
    qid = _entity_id_from_value(raw)
    if qid and isinstance(raw, str) and "wikidata.org/entity/" in raw:
        return qid
    if isinstance(raw, str):
        return raw.strip()
    return str(raw).strip()


def _normalize_binding_value(value: _SparqlBindingValue) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        "type": value.type or "",
        "value": value.value,
    }
    if value.datatype:
        normalized["datatype"] = value.datatype
    if value.xml_lang:
        normalized["lang"] = value.xml_lang
    qid = _entity_id_from_value(value.value)
    if qid:
        normalized["entity_id"] = qid
    return normalized


def _find_entity_binding(bindings: dict[str, _SparqlBindingValue]) -> tuple[str, str]:
    for name in ("item", "entity", "person", "subject", "human"):
        value = bindings.get(name)
        if value is not None:
            qid = _entity_id_from_value(value.value)
            if qid:
                return name, qid
    for name, value in bindings.items():
        qid = _entity_id_from_value(value.value)
        if qid:
            return name, qid
    return "", ""


def _first_literal(
    bindings: dict[str, _SparqlBindingValue],
    candidates: list[str],
) -> tuple[str, str]:
    seen = set(candidates)
    for name in list(bindings):
        if name.endswith("Label") and name not in seen:
            candidates.append(name)
            seen.add(name)
    for name in candidates:
        value = bindings.get(name)
        if value is None:
            continue
        text = _display_value(value)
        if text:
            return name, text
    return "", ""


def _snippet_for_row(
    bindings: dict[str, _SparqlBindingValue],
    *,
    entity_var: str,
    qid: str,
    label_var: str,
) -> str:
    _, description = _first_literal(
        bindings,
        [
            f"{entity_var}Description",
            "itemDescription",
            "entityDescription",
            "description",
        ],
    )
    parts: list[str] = []
    if description:
        parts.append(description)

    skip = {entity_var, label_var}
    for name, value in bindings.items():
        if name in skip or name.endswith("Label") or name.endswith("Description"):
            continue
        text = _display_value(value)
        if text:
            parts.append(f"{name}: {text}")
        if len(parts) >= 5:
            break
    return "; ".join(parts) or f"Wikidata entity {qid}"


def _build_search_result(bindings: dict[str, _SparqlBindingValue]) -> SearchResult | None:
    entity_var, qid = _find_entity_binding(bindings)
    if not qid:
        return None

    label_var, label = _first_literal(
        bindings,
        [
            f"{entity_var}Label",
            "itemLabel",
            "entityLabel",
            "personLabel",
            "label",
            "name",
        ],
    )
    title = label or qid
    url = f"https://www.wikidata.org/wiki/{qid}"
    normalized_bindings = {
        name: _normalize_binding_value(value) for name, value in bindings.items()
    }
    return SearchResult(
        url=url,
        title=title,
        snippet=_snippet_for_row(
            bindings,
            entity_var=entity_var,
            qid=qid,
            label_var=label_var,
        ),
        source_kind=KIND,
        extras={
            "entity_id": qid,
            "entity_var": entity_var,
            "bindings": normalized_bindings,
        },
    )


async def search(
    query: str,
    *,
    max_results: int = 20,
    timeout: float = 30.0,
) -> list[SearchResult]:
    """Run a raw SPARQL query against WDQS and return entity-shaped rows."""
    if max_results <= 0:
        return []
    sparql = query.strip()
    if not sparql:
        return []

    payload = await _request_sparql(sparql, timeout=timeout)
    if payload is None:
        return []
    try:
        parsed = _SparqlResponse.model_validate(payload)
    except ValidationError as exc:
        logger.warning("wikidata SPARQL response failed schema validation: %s", exc)
        return []

    results: list[SearchResult] = []
    for row in parsed.results.bindings:
        result = _build_search_result(row)
        if result is None:
            continue
        results.append(result)
        if len(results) >= max_results:
            break
    return results


def _extract_qid_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    host = (parsed.hostname or "").lower()
    if host not in _HOSTS:
        return ""
    path = unquote(parsed.path or "")
    match = _QID_RE.search(path)
    return match.group(0) if match else ""


def _localized_value(bucket: Any) -> str:
    if not isinstance(bucket, dict) or not bucket:
        return ""
    preferred = bucket.get("en")
    if isinstance(preferred, dict) and isinstance(preferred.get("value"), str):
        return preferred["value"].strip()
    for entry in bucket.values():
        if isinstance(entry, dict) and isinstance(entry.get("value"), str):
            text = entry["value"].strip()
            if text:
                return text
    return ""


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _claim_value(claim: Any) -> Any:
    if not isinstance(claim, dict):
        return None
    mainsnak = claim.get("mainsnak")
    if not isinstance(mainsnak, dict):
        return None
    datavalue = mainsnak.get("datavalue")
    if not isinstance(datavalue, dict):
        return None
    value = datavalue.get("value")
    if isinstance(value, dict):
        if isinstance(value.get("id"), str):
            return value["id"]
        if isinstance(value.get("time"), str):
            return value["time"]
        if isinstance(value.get("text"), str):
            return value["text"]
        if "latitude" in value and "longitude" in value:
            return {
                "latitude": value.get("latitude"),
                "longitude": value.get("longitude"),
                "precision": value.get("precision"),
                "globe": value.get("globe"),
            }
        if isinstance(value.get("amount"), str):
            return value["amount"]
        return _jsonable(value)
    if isinstance(value, (str, int, float, bool)):
        return value
    return None


def _normalize_claims(raw_claims: Any) -> dict[str, list[Any]]:
    if not isinstance(raw_claims, dict):
        return {}
    claims: dict[str, list[Any]] = {}
    for pid, entries in raw_claims.items():
        if not isinstance(pid, str) or not pid.startswith("P"):
            continue
        values: list[Any] = []
        if isinstance(entries, list):
            for claim in entries:
                value = _claim_value(claim)
                if value is not None:
                    values.append(value)
        if values:
            claims[pid] = values
    return claims


def _normalize_sitelinks(raw_sitelinks: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_sitelinks, dict):
        return {}
    sitelinks: dict[str, dict[str, Any]] = {}
    for site, entry in raw_sitelinks.items():
        if not isinstance(site, str) or not isinstance(entry, dict):
            continue
        title = entry.get("title")
        if not isinstance(title, str) or not title:
            continue
        url = entry.get("url")
        badges = entry.get("badges")
        sitelinks[site] = {
            "title": title,
            "url": url if isinstance(url, str) else "",
            "badges": badges if isinstance(badges, list) else [],
        }
    return sitelinks


def _property_sort_key(pid: str) -> tuple[int, str]:
    try:
        return (int(pid[1:]), pid)
    except ValueError:
        return (10**9, pid)


def _format_md_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    return str(value)


def _metadata_markdown(
    *,
    qid: str,
    label: str,
    description: str,
    claims: dict[str, list[Any]],
    sitelinks: dict[str, dict[str, Any]],
) -> str:
    lines = [f"# {label or qid}", "", f"Entity: {qid}"]
    if description:
        lines.extend(["", description])
    lines.extend(["", "## Claims"])
    if claims:
        for pid in sorted(claims, key=_property_sort_key):
            values = claims[pid]
            preview = ", ".join(_format_md_value(v) for v in values[:5])
            if len(values) > 5:
                preview += f", ... (+{len(values) - 5} more)"
            lines.append(f"- {pid}: {preview}")
    else:
        lines.append("- (none)")
    lines.extend(["", "## Sitelinks"])
    if sitelinks:
        for site in sorted(sitelinks)[:20]:
            entry = sitelinks[site]
            lines.append(f"- {site}: {entry['title']}")
    else:
        lines.append("- (none)")
    return "\n".join(lines).strip()


async def _fetch_entity_json(qid: str, *, timeout: float) -> dict[str, Any] | None:
    url = f"{_ENTITY_DATA_BASE}/{qid}.json"
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=timeout,
                headers=_json_headers(),
            ) as client:
                response = await client.get(url)
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("wikidata entity fetch failed for %s: %s", qid, exc)
            return None
        if response.status_code == 429 and attempt == 0:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            logger.warning(
                "wikidata entity fetch returned 429; honoring Retry-After %.2fs",
                retry_after,
            )
            await asyncio.sleep(retry_after)
            continue
        if response.status_code != 200:
            logger.warning(
                "wikidata entity fetch returned HTTP %s for %s",
                response.status_code,
                qid,
            )
            return None
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("wikidata entity fetch returned non-JSON body: %s", exc)
            return None
        return payload if isinstance(payload, dict) else None
    return None


async def fetch(url: str, *, timeout: float = 20.0) -> Source | None:
    """Fetch a Wikidata entity URL and return structured metadata."""
    qid = _extract_qid_from_url(url)
    if not qid:
        return None

    payload = await _fetch_entity_json(qid, timeout=timeout)
    if payload is None:
        return None
    entities = payload.get("entities")
    if not isinstance(entities, dict):
        return None
    entity = entities.get(qid)
    if not isinstance(entity, dict):
        return None

    label = _localized_value(entity.get("labels")) or qid
    description = _localized_value(entity.get("descriptions"))
    claims = _normalize_claims(entity.get("claims"))
    sitelinks = _normalize_sitelinks(entity.get("sitelinks"))
    canonical = f"https://www.wikidata.org/wiki/{qid}"
    metadata = {
        "entity_id": qid,
        "label": label,
        "description": description,
        "claims": claims,
        "sitelinks": sitelinks,
    }
    return Source(
        url=canonical,
        title=label,
        cleaned_text=_metadata_markdown(
            qid=qid,
            label=label,
            description=description,
            claims=claims,
            sitelinks=sitelinks,
        ),
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind=KIND,
        metadata=metadata,
    )


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _rate_lock
    _query_durations.clear()
    _rate_lock = asyncio.Lock()


class _PayloadSchema(_BaseSearchPayload):
    max_results: int | None = None
    timeout: float | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=("wikidata.org", "www.wikidata.org", "query.wikidata.org"),
    skill_name="wikidata",
    description=(
        "Wikidata Query Service raw SPARQL for biographical, relational,"
        " occupational, place, and entity-ID data"
    ),
    optional_payload_knobs="`max_results` (client-side truncation; SPARQL should include `LIMIT`)",
    example_query=(
        'SELECT ?item ?itemLabel WHERE { ?item wdt:P31 wd:Q5; wdt:P19 wd:Q90 . '
        'SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } } LIMIT 3'
    ),
    module_name="wikidata",
)


__all__ = ["KIND", "fetch", "reset_for_tests", "search"]
