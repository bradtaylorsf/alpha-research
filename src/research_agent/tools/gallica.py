"""Gallica / Bibliotheque nationale de France SRU connector (issue #238).

Public surface:

* ``async def search(query, *, max_results=20) -> list[SearchResult]`` hits
  ``https://gallica.bnf.fr/services/engine/search/sru`` with SRU 1.2
  ``searchRetrieve`` parameters.
* ``async def fetch(url) -> Source | None`` resolves Gallica ARK permalinks
  back through SRU metadata and returns a compact Dublin Core record card.

Gallica is the first XML-response connector in this tree. The SRU endpoint
returns namespaced XML, not JSON; this module first parses it with stdlib
``xml.etree.ElementTree``. Live Gallica responses occasionally contain
malformed embedded metadata, so malformed payloads fall back to lxml's
recovering XML parser without changing the element-walking code. Namespace
handling deliberately combines explicit SRU/Dublin Core namespace constants
with local-name fallback so fixtures using prefixes (``srw:record``) and
default namespaces (``<record>`` under the SRU namespace) parse the same way.
Do not fork JSON-first connector patterns such as ``nonprofits.py`` for this
surface.

No auth required. Polite per-host rate of 1 RPS.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import unquote, urlparse

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

KIND: Literal["gallica_search"] = "gallica_search"

_SRU_URL = "https://gallica.bnf.fr/services/engine/search/sru"
_SITE_BASE = "https://gallica.bnf.fr"
_HOSTS = frozenset({"gallica.bnf.fr"})
_PAGE_SIZE_CAP = 50
_RATE_LIMIT_INTERVAL = 1.0

_NS = {
    "srw": "http://www.loc.gov/zing/srw/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}
_DC_FIELDS = (
    "title",
    "creator",
    "description",
    "identifier",
    "type",
    "date",
    "language",
    "source",
)
_WS_RE = re.compile(r"\s+")
_ARK_RE = re.compile(r"ark:/\d+/[A-Za-z0-9._~-]+")

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/xml, text/xml;q=0.9, */*;q=0.1",
        "User-Agent": config.get("RESEARCH_USER_AGENT")
        or "research-agent/0.1 (+local; contact unset)",
    }


async def _rate_limit_gate() -> None:
    """Block until at least one second has passed since the previous request."""
    global _last_call_monotonic
    async with _rate_lock:
        if _last_call_monotonic is not None:
            elapsed = time.monotonic() - _last_call_monotonic
            wait = _RATE_LIMIT_INTERVAL - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
        _last_call_monotonic = time.monotonic()


def _clean_text(value: str | None) -> str:
    return _WS_RE.sub(" ", value or "").strip()


def _cql_quote(value: str) -> str:
    text = _clean_text(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_cql_query(query: str) -> str:
    """Convert plain text into Gallica's simplest keyword CQL form."""
    return f"gallica all {_cql_quote(query)}"


def _build_identifier_cql(ark: str) -> str:
    return f"dc.identifier all {_cql_quote(ark)}"


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    if ":" in tag:
        return tag.rsplit(":", 1)[1]
    return tag


def _iter_local(root: ET.Element, name: str) -> list[ET.Element]:
    return [node for node in root.iter() if _local_name(node.tag) == name]


def _first_local(root: ET.Element, name: str) -> ET.Element | None:
    for node in root.iter():
        if _local_name(node.tag) == name:
            return node
    return None


def _field_values(root: ET.Element, field: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for node in root.iter():
        if _local_name(node.tag) != field:
            continue
        text = _clean_text(" ".join(node.itertext()))
        if not text or text in seen:
            continue
        seen.add(text)
        values.append(text)
    return values


def _join(values: list[str]) -> str:
    return "; ".join(values)


def _extract_ark(values: list[str]) -> str:
    for value in values:
        match = _ARK_RE.search(unquote(value))
        if match is not None:
            return match.group(0)
    return ""


def _canonical_url(ark: str, identifiers: list[str]) -> str:
    if ark:
        return f"{_SITE_BASE}/{ark}"
    for value in identifiers:
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname in _HOSTS:
            return f"https://{parsed.hostname}{parsed.path}"
    return ""


def _parse_date(value: str) -> datetime | None:
    text = _clean_text(value)
    if not text:
        return None
    head = text.split("/", 1)[0].strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(head, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    match = re.search(r"\b(\d{4})\b", text)
    if match is None:
        return None
    try:
        return datetime(int(match.group(1)), 1, 1, tzinfo=UTC)
    except ValueError:
        return None


def _record_data(record: ET.Element) -> ET.Element:
    record_data = _first_local(record, "recordData")
    return record_data if record_data is not None else record


def _record_fields(record: ET.Element) -> dict[str, list[str]]:
    data = _record_data(record)
    return {field: _field_values(data, field) for field in _DC_FIELDS}


def _metadata_from_fields(fields: dict[str, list[str]]) -> dict[str, Any]:
    ark = _extract_ark(fields["identifier"])
    return {
        "ark": ark,
        "dc:type": _join(fields["type"]),
        "dc:date": _join(fields["date"]),
        "dc:language": _join(fields["language"]),
        "dc:source": _join(fields["source"]),
        "dc:identifier": fields["identifier"],
        "dc:creator": fields["creator"],
    }


def _snippet_from_fields(fields: dict[str, list[str]]) -> str:
    parts: list[str] = []
    if fields["creator"]:
        parts.append("Creator: " + _join(fields["creator"][:3]))
    if fields["date"]:
        parts.append("Date: " + _join(fields["date"][:2]))
    if fields["type"]:
        parts.append("Type: " + _join(fields["type"][:2]))
    if fields["language"]:
        parts.append("Language: " + _join(fields["language"][:2]))
    if fields["source"]:
        parts.append("Source: " + _join(fields["source"][:2]))
    if fields["description"]:
        parts.append(_join(fields["description"][:2]))
    return " | ".join(parts)[:600]


def _search_result_from_record(record: ET.Element) -> SearchResult | None:
    fields = _record_fields(record)
    metadata = _metadata_from_fields(fields)
    title = fields["title"][0] if fields["title"] else ""
    ark = str(metadata.get("ark") or "")
    url = _canonical_url(ark, fields["identifier"])
    if not title or not url:
        return None
    return SearchResult(
        url=url,
        title=title,
        snippet=_snippet_from_fields(fields) or title,
        published_at=_parse_date(metadata["dc:date"]),
        source_kind=KIND,
        extras=metadata,
    )


def _source_markdown(title: str, metadata: dict[str, Any], fields: dict[str, list[str]]) -> str:
    lines = [f"# {title}", ""]
    lines.append(f"- ARK: {metadata.get('ark') or 'none listed'}")
    for label, key in (
        ("Type", "dc:type"),
        ("Date", "dc:date"),
        ("Language", "dc:language"),
        ("Source", "dc:source"),
    ):
        value = str(metadata.get(key) or "").strip()
        if value:
            lines.append(f"- {label}: {value}")

    if fields["creator"]:
        lines.extend(["", "## Creators"])
        lines.extend(f"- {creator}" for creator in fields["creator"])
    if fields["description"]:
        lines.extend(["", "## Description"])
        lines.extend(fields["description"])
    if fields["identifier"]:
        lines.extend(["", "## Identifiers"])
        lines.extend(f"- {identifier}" for identifier in fields["identifier"])
    return "\n".join(lines).strip()


def _source_from_record(record: ET.Element) -> Source | None:
    fields = _record_fields(record)
    metadata = _metadata_from_fields(fields)
    title = fields["title"][0] if fields["title"] else ""
    ark = str(metadata.get("ark") or "")
    url = _canonical_url(ark, fields["identifier"])
    if not title or not url:
        return None
    return Source(
        url=url,
        title=title,
        cleaned_text=_source_markdown(title, metadata, fields),
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind=KIND,
        metadata=metadata,
    )


def _records(root: ET.Element) -> list[ET.Element]:
    records = root.findall(".//srw:record", _NS)
    if records:
        return records
    return _iter_local(root, "record")


async def _request_xml(
    *,
    params: dict[str, Any],
    timeout: float,
) -> ET.Element | None:
    await _rate_limit_gate()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(_SRU_URL, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("gallica request failed for params=%s: %s", params, exc)
        return None

    if response.status_code != 200:
        logger.warning(
            "gallica request returned HTTP %s for params=%s",
            response.status_code,
            params,
        )
        return None

    try:
        return ET.fromstring(response.text)
    except ET.ParseError as exc:
        logger.warning(
            "gallica request returned malformed XML; retrying with recovery: %s",
            exc,
        )
        return _recover_malformed_xml(response.text)


def _recover_malformed_xml(xml_text: str) -> ET.Element | None:
    """Parse live Gallica SRU responses that contain malformed metadata tags."""
    try:
        from lxml import etree
    except ImportError:
        logger.warning("gallica malformed XML recovery unavailable: lxml missing")
        return None

    parser = etree.XMLParser(
        recover=True,
        resolve_entities=False,
        no_network=True,
    )
    try:
        root = etree.fromstring(xml_text.encode("utf-8"), parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        logger.warning("gallica malformed XML recovery failed: %s", exc)
        return None
    if root is None:
        return None
    if parser.error_log:
        logger.info(
            "gallica malformed XML recovered with %s parser error(s)",
            len(parser.error_log),
        )
    return root


async def search(
    query: str,
    *,
    max_results: int = 20,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Search Gallica SRU metadata with a plain-text keyword query."""
    q = query.strip()
    if not q or max_results <= 0:
        return []

    root = await _request_xml(
        params={
            "operation": "searchRetrieve",
            "version": "1.2",
            "query": build_cql_query(q),
            "maximumRecords": min(max_results, _PAGE_SIZE_CAP),
            "suggest": 0,
        },
        timeout=timeout,
    )
    if root is None:
        return []

    results: list[SearchResult] = []
    seen: set[str] = set()
    for record in _records(root):
        result = _search_result_from_record(record)
        if result is None or result.url in seen:
            continue
        seen.add(result.url)
        results.append(result)
        if len(results) >= max_results:
            break
    return results


def _ark_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if (parsed.hostname or "").casefold() not in _HOSTS:
        return ""
    match = _ARK_RE.search(unquote(parsed.path))
    return match.group(0) if match is not None else ""


async def fetch(url: str, *, timeout: float = 15.0) -> Source | None:
    """Fetch Gallica Dublin Core metadata for a Gallica ARK permalink."""
    ark = _ark_from_url(url)
    if not ark:
        return None

    root = await _request_xml(
        params={
            "operation": "searchRetrieve",
            "version": "1.2",
            "query": _build_identifier_cql(ark),
            "maximumRecords": 1,
            "suggest": 0,
        },
        timeout=timeout,
    )
    if root is None:
        return None
    for record in _records(root):
        source = _source_from_record(record)
        if source is not None:
            return source
    return None


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
    host_patterns=("gallica.bnf.fr",),
    skill_name="gallica",
    description=(
        "Gallica/BnF SRU XML search for French national-library newspapers,"
        " books, manuscripts, maps, and other digitized primary sources"
    ),
    optional_payload_knobs="`max_results` (SRU maximumRecords capped at 50)",
    example_query="guerre d'Algerie",
    module_name="gallica",
)


__all__ = [
    "KIND",
    "build_cql_query",
    "fetch",
    "reset_for_tests",
    "search",
]
