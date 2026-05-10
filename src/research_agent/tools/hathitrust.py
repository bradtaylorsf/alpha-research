"""HathiTrust Digital Library fetch-only enrichment connector (issue #235).

The HathiTrust Bibliographic API is an identifier lookup surface, not a
keyword search API. This module therefore intentionally has no planner kind;
planner-callable search belongs to catalog connectors that can surface
ISBN/OCLC/LCCN/HTID values first.

Public surface:

* ``async def fetch_by_identifier(*, isbn=None, oclc=None, lccn=None, htid=None)``
  calls ``https://catalog.hathitrust.org/api/volumes/brief/<id_type>/<id>.json``
  and returns rights / full-text availability metadata as a :class:`Source`.
* ``async def fetch(url)`` classifies HathiTrust catalog and handle URLs, then
  performs the same lookup.
* ``async def enrich_source_from_identifiers(source)`` merges HathiTrust
  metadata into a source that already carries catalog identifiers. This is the
  hook OpenLibrary/LoC-style fetch paths can call after they land.

No auth required. Polite per-host rate of 1 RPS.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from research_agent import config
from research_agent.tools.models import Source

logger = logging.getLogger(__name__)

_BASE_URL = "https://catalog.hathitrust.org/api/volumes/brief"
_DEFAULT_TIMEOUT = 15.0
_RATE_LIMIT_INTERVAL = 1.0

_IdType = Literal["isbn", "oclc", "lccn", "htid", "recordnumber"]
_SUPPORTED_ID_TYPES: frozenset[str] = frozenset(
    {"isbn", "oclc", "lccn", "htid", "recordnumber"}
)

_RECORD_PATH_RE = re.compile(r"^/Record/(?P<recordnumber>\d{1,12})/?$")
_API_PATH_RE = re.compile(
    r"^/api/volumes/(?:brief|full)/"
    r"(?P<id_type>isbn|oclc|lccn|htid|recordnumber)/"
    r"(?P<identifier>[^/]+)\.json$",
    re.IGNORECASE,
)
_HANDLE_PATH_RE = re.compile(r"^/2027/(?P<htid>[^/?#]+)")

_FULL_TEXT_RIGHTS = frozenset(
    {
        "pd",
        "pdus",
        "world",
        "und-world",
        "cc0",
        "cc-by",
        "cc-by-nd",
        "cc-by-sa",
        "cc-by-nc",
        "cc-by-nc-nd",
        "cc-by-nc-sa",
    }
)

_HATHI_ENRICHMENT_KEYS = (
    "hathi_record_id",
    "rights",
    "full_text_available",
    "volumes",
    "hathi_permalink",
    "identifiers",
    "fetched_via",
)

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


def reset_for_tests() -> None:
    """Reset module-level rate limiter state for deterministic unit tests."""
    global _last_call_monotonic
    _last_call_monotonic = None


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": config.get("RESEARCH_USER_AGENT")
        or "research-agent/0.1 (+local; contact unset)",
    }


async def _rate_limit_gate() -> None:
    """Block until at least ``_RATE_LIMIT_INTERVAL`` has passed since last call."""
    global _last_call_monotonic
    async with _rate_lock:
        if _last_call_monotonic is not None:
            elapsed = time.monotonic() - _last_call_monotonic
            wait = _RATE_LIMIT_INTERVAL - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
        _last_call_monotonic = time.monotonic()


def _clean_identifier(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _select_identifier(
    *,
    isbn: Any = None,
    oclc: Any = None,
    lccn: Any = None,
    htid: Any = None,
) -> tuple[_IdType, str] | None:
    # Prefer direct HathiTrust volume IDs when present; among bibliographic
    # identifiers OCLC/LCCN tend to be more precise than ISBN for multi-edition
    # works.
    for id_type, value in (
        ("htid", htid),
        ("oclc", oclc),
        ("lccn", lccn),
        ("isbn", isbn),
    ):
        identifier = _clean_identifier(value)
        if identifier:
            return id_type, identifier  # type: ignore[return-value]
    return None


def _api_url(id_type: str, identifier: str) -> str:
    return f"{_BASE_URL}/{id_type}/{quote(identifier, safe='')}.json"


async def _lookup_payload(
    id_type: _IdType,
    identifier: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any] | None:
    await _rate_limit_gate()
    url = _api_url(id_type, identifier)
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("hathitrust lookup failed for %s:%s: %s", id_type, identifier, exc)
        return None

    if response.status_code != 200:
        logger.warning(
            "hathitrust lookup returned HTTP %s for %s:%s",
            response.status_code,
            id_type,
            identifier,
        )
        return None

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning(
            "hathitrust lookup returned non-JSON for %s:%s: %s",
            id_type,
            identifier,
            exc,
        )
        return None

    return payload if isinstance(payload, dict) else None


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(entry).strip() for entry in value if str(entry).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _aggregate_identifiers(records: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {
        "isbns": [],
        "issns": [],
        "oclcs": [],
        "lccns": [],
    }
    seen: dict[str, set[str]] = {key: set() for key in out}
    for record in records.values():
        if not isinstance(record, dict):
            continue
        for key in out:
            for value in _as_str_list(record.get(key)):
                if value not in seen[key]:
                    seen[key].add(value)
                    out[key].append(value)
    return out


def _first_title(record: dict[str, Any] | None) -> str:
    if not isinstance(record, dict):
        return ""
    titles = _as_str_list(record.get("titles"))
    return titles[0] if titles else ""


def _record_url(record: dict[str, Any] | None, record_id: str) -> str:
    if isinstance(record, dict):
        raw = record.get("recordURL")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return f"https://catalog.hathitrust.org/Record/{record_id}"


def _is_full_text_available(rights_code: str, rights_string: str) -> bool:
    label = rights_string.casefold()
    if "full view" in label:
        return True
    if "limited" in label or "search-only" in label or "search only" in label:
        return False

    code = rights_code.strip().casefold()
    return code in _FULL_TEXT_RIGHTS or code.startswith("cc-")


def _normalize_volume(item: dict[str, Any]) -> dict[str, Any] | None:
    htid = _clean_identifier(item.get("htid"))
    if not htid:
        return None
    rights = str(item.get("rightsCode") or "").strip()
    rights_string = str(item.get("usRightsString") or "").strip()
    item_url = str(item.get("itemURL") or "").strip()
    if not item_url:
        item_url = f"https://hdl.handle.net/2027/{quote(htid, safe='')}"

    return {
        "htid": htid,
        "item_url": item_url,
        "rights": rights,
        "rights_string": rights_string,
        "full_text_available": _is_full_text_available(rights, rights_string),
        "from_record": str(item.get("fromRecord") or "").strip(),
        "origin": str(item.get("orig") or "").strip(),
        "last_update": str(item.get("lastUpdate") or "").strip(),
        "enumcron": item.get("enumcron"),
    }


def _pick_display_volume(volumes: list[dict[str, Any]]) -> dict[str, Any]:
    for volume in volumes:
        if volume.get("full_text_available"):
            return volume
    return volumes[0]


def _render_cleaned_text(
    *,
    title: str,
    record_id: str,
    record_url: str,
    rights: str,
    full_text_available: bool,
    identifiers: dict[str, list[str]],
    volumes: list[dict[str, Any]],
) -> str:
    lines = [
        f"# {title}",
        "",
        f"HathiTrust record: {record_id}",
        f"Catalog URL: {record_url}",
        f"Rights: {rights or 'unknown'}",
        f"Full text available: {'yes' if full_text_available else 'no'}",
    ]

    id_parts: list[str] = []
    for label, key in (
        ("OCLC", "oclcs"),
        ("LCCN", "lccns"),
        ("ISBN", "isbns"),
        ("ISSN", "issns"),
    ):
        values = identifiers.get(key) or []
        if values:
            id_parts.append(f"{label} {', '.join(values[:5])}")
    if id_parts:
        lines.extend(["", "Identifiers: " + "; ".join(id_parts)])

    lines.extend(["", "Volumes:"])
    for volume in volumes:
        bits = [
            str(volume.get("htid") or ""),
            str(volume.get("rights") or "unknown"),
            "Full View" if volume.get("full_text_available") else "Limited",
        ]
        origin = str(volume.get("origin") or "").strip()
        if origin:
            bits.append(origin)
        item_url = str(volume.get("item_url") or "").strip()
        if item_url:
            bits.append(item_url)
        lines.append("- " + " - ".join(bit for bit in bits if bit))

    return "\n".join(lines).strip()


def _build_source(
    payload: dict[str, Any],
    *,
    id_type: _IdType,
    identifier: str,
) -> Source | None:
    records_raw = payload.get("records")
    items_raw = payload.get("items")
    if not isinstance(records_raw, dict) or not records_raw:
        return None
    if not isinstance(items_raw, list) or not items_raw:
        return None

    records: dict[str, dict[str, Any]] = {
        str(record_id): record
        for record_id, record in records_raw.items()
        if isinstance(record, dict)
    }
    if not records:
        return None

    volumes = [
        volume
        for item in items_raw
        if isinstance(item, dict)
        for volume in [_normalize_volume(item)]
        if volume is not None
    ]
    if not volumes:
        return None

    display_volume = _pick_display_volume(volumes)
    record_id = str(display_volume.get("from_record") or "").strip()
    if not record_id or record_id not in records:
        record_id = next(iter(records))
    record = records.get(record_id)
    title = _first_title(record) or f"HathiTrust record {record_id}"
    record_url = _record_url(record, record_id)
    rights = str(display_volume.get("rights") or "").strip()
    if not rights:
        rights = next((str(v.get("rights") or "").strip() for v in volumes if v.get("rights")), "")
    full_text_available = any(bool(volume.get("full_text_available")) for volume in volumes)
    identifiers = _aggregate_identifiers(records)
    hathi_permalink = str(display_volume.get("item_url") or record_url)

    metadata: dict[str, Any] = {
        "hathi_record_id": record_id,
        "rights": rights,
        "full_text_available": full_text_available,
        "volumes": volumes,
        "hathi_permalink": hathi_permalink,
        "identifiers": identifiers,
        "lookup": {"id_type": id_type, "identifier": identifier},
        "record_count": len(records),
        "fetched_via": "hathitrust",
    }

    return Source(
        url=record_url,
        title=title,
        cleaned_text=_render_cleaned_text(
            title=title,
            record_id=record_id,
            record_url=record_url,
            rights=rights,
            full_text_available=full_text_available,
            identifiers=identifiers,
            volumes=volumes,
        ),
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="hathitrust",
        metadata=metadata,
    )


async def _fetch_lookup(
    id_type: _IdType,
    identifier: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Source | None:
    if id_type not in _SUPPORTED_ID_TYPES:
        return None
    cleaned = _clean_identifier(identifier)
    if not cleaned:
        return None
    payload = await _lookup_payload(id_type, cleaned, timeout=timeout)
    if payload is None:
        return None
    return _build_source(payload, id_type=id_type, identifier=cleaned)


async def fetch_by_identifier(
    *,
    isbn: str | None = None,
    oclc: str | None = None,
    lccn: str | None = None,
    htid: str | None = None,
) -> Source | None:
    """Fetch HathiTrust bibliographic metadata by a known identifier.

    Returns ``None`` when no identifier is supplied, the API misses, the API
    returns an HTTP/non-JSON error, or the matched record has no item rows.
    """
    selected = _select_identifier(isbn=isbn, oclc=oclc, lccn=lccn, htid=htid)
    if selected is None:
        return None
    id_type, identifier = selected
    return await _fetch_lookup(id_type, identifier)


def _classify_url(url: str) -> tuple[_IdType, str] | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path or ""

    if host == "catalog.hathitrust.org":
        record_match = _RECORD_PATH_RE.match(path)
        if record_match:
            return "recordnumber", unquote(record_match.group("recordnumber"))

        api_match = _API_PATH_RE.match(path)
        if api_match:
            id_type = api_match.group("id_type").casefold()
            identifier = unquote(api_match.group("identifier"))
            if id_type in _SUPPORTED_ID_TYPES:
                return id_type, identifier  # type: ignore[return-value]

        if path == "/cgi/pt":
            htid_values = parse_qs(parsed.query).get("id") or []
            htid = htid_values[0] if htid_values else None
            cleaned = _clean_identifier(htid)
            if cleaned:
                return "htid", cleaned

    if host == "hdl.handle.net":
        handle_match = _HANDLE_PATH_RE.match(path)
        if handle_match:
            return "htid", unquote(handle_match.group("htid"))

    return None


async def fetch(url: str, timeout: float = _DEFAULT_TIMEOUT) -> Source | None:
    """Fetch a HathiTrust URL by extracting its catalog/volume identifier."""
    classified = _classify_url(url)
    if classified is None:
        return None
    id_type, identifier = classified
    return await _fetch_lookup(id_type, identifier, timeout=timeout)


def _first_metadata_value(metadata: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    def _first(value: Any) -> str | None:
        if isinstance(value, list):
            for entry in value:
                cleaned = _clean_identifier(entry)
                if cleaned:
                    return cleaned
            return None
        return _clean_identifier(value)

    for key in keys:
        value = _first(metadata.get(key))
        if value:
            return value

    identifiers = metadata.get("identifiers")
    if isinstance(identifiers, dict):
        for key in keys:
            value = _first(identifiers.get(key))
            if value:
                return value
    return None


def _identifier_kwargs_from_metadata(metadata: dict[str, Any]) -> dict[str, str]:
    oclc = _first_metadata_value(
        metadata,
        ("oclc", "oclc_number", "oclc_numbers", "oclcs"),
    )
    if oclc:
        return {"oclc": oclc}

    lccn = _first_metadata_value(
        metadata,
        ("lccn", "lccn_number", "lccn_numbers", "lccns"),
    )
    if lccn:
        return {"lccn": lccn}

    isbn = _first_metadata_value(
        metadata,
        ("isbn", "isbn_10", "isbn_13", "isbn_numbers", "isbns"),
    )
    if isbn:
        return {"isbn": isbn}

    htid = _first_metadata_value(metadata, ("htid", "hathi_id", "hathitrust_id"))
    if htid:
        return {"htid": htid}

    return {}


async def enrich_source_from_identifiers(source: Source) -> Source:
    """Merge HathiTrust enrichment into ``source`` when it has lookup IDs.

    The source keeps its original URL/source_kind; only metadata is augmented.
    Misses are non-fatal and return the source unchanged.
    """
    kwargs = _identifier_kwargs_from_metadata(source.metadata)
    if not kwargs:
        return source

    hathi_source = await fetch_by_identifier(**kwargs)
    if hathi_source is None:
        return source

    for key in _HATHI_ENRICHMENT_KEYS:
        if key in hathi_source.metadata:
            source.metadata[key] = hathi_source.metadata[key]
    source.metadata["hathi_source_url"] = hathi_source.url
    source.metadata["hathi_title"] = hathi_source.title
    return source


__all__ = [
    "enrich_source_from_identifiers",
    "fetch",
    "fetch_by_identifier",
    "reset_for_tests",
]
