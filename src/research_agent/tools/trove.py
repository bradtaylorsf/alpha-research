"""Trove API v3 connector (National Library of Australia, issue #230).

Public surface:

* ``async def search(query, *, max_results=20, **knobs) -> list[SearchResult]``
  hits ``https://api.trove.nla.gov.au/v3/result`` with ``X-API-KEY`` auth.
  The default category set is metadata-bearing only: books, newspapers,
  images/pictures, and magazines.
* ``async def fetch(url) -> Source | None`` resolves Trove work / newspaper /
  gazette / magazine URLs to the matching v3 metadata endpoint and renders a
  short metadata card.

Trove API keys expire after 12 months and NLA has revoked keys for workflows
that download full text by default. This connector is intentionally
metadata-only: it never sends ``include=articletext`` and never inlines article
text into ``cleaned_text``. Potential full-text / online-copy URLs are exposed
as metadata for a deliberate, operator-controlled follow-up.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from research_agent import config
from research_agent.tools._errors import MissingCredentialError
from research_agent.tools._registry import (
    BaseSearchPayload as _BaseSearchPayload,
    register_kind as _register_kind,
)
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

KIND = "trove_search"

_API_BASE = "https://api.trove.nla.gov.au/v3"
_RESULT_URL = f"{_API_BASE}/result"
_SITE_BASE = "https://trove.nla.gov.au"
_DEFAULT_CATEGORIES: tuple[str, ...] = ("book", "newspaper", "image", "magazine")
_CATEGORY_ALIASES = {
    "picture": "image",
    "pictures": "image",
    "photo": "image",
    "photos": "image",
    "sound": "music",
    "audio": "music",
}
_CATEGORY_TO_ZONE = {
    "image": "picture",
    "music": "sound",
    "book": "book",
    "newspaper": "newspaper",
    "gazette": "newspaper",
    "magazine": "magazine",
}
_RATE_LIMIT_INTERVAL = 1.0
_PAGE_SIZE_CAP = 100

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_API_RECORD_RE = re.compile(r"^/v3/(?P<kind>work|newspaper|gazette|magazine)/(?P<id>[^/?#]+)")
_WORK_URL_RE = re.compile(r"^/work/(?P<id>[^/?#]+)")
_NEWSPAPER_URL_RE = re.compile(r"^/newspaper/(?:r/)?article/(?P<id>\d+)")
_GAZETTE_URL_RE = re.compile(r"^/gazette/(?:r/)?article/(?P<id>\d+)")
_MAGAZINE_URL_RE = re.compile(r"^/magazine/(?:article/)?(?P<id>[^/?#]+)")
_NLA_NEWS_RE = re.compile(r"nla\.news-article(?P<id>\d+)")
_NLA_OBJ_RE = re.compile(r"nla\.obj-\d+")

_BLOCKED_EXTRA_PARAMS = frozenset(
    {"q", "key", "category", "zone", "n", "encoding", "include", "reclevel"}
)
_SAFE_EXTRA_PARAMS = frozenset({"facet", "s"})
_RECORD_KEYS = (
    "work",
    "works",
    "article",
    "articles",
    "record",
    "records",
    "item",
    "items",
)

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


def _resolve_api_key() -> str:
    raw = config.get("TROVE_API_KEY") or ""
    key = raw.strip()
    if not key:
        raise MissingCredentialError(
            "Trove requires TROVE_API_KEY. Request a free key through the "
            "Trove account API form; keys expire after 12 months. This "
            "connector is metadata-only and does not auto-fetch full text."
        )
    return key


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-API-KEY": _resolve_api_key(),
        "User-Agent": config.get("RESEARCH_USER_AGENT")
        or "research-agent/0.1 (+local; contact unset)",
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


def _strip_markup(value: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", value)).strip()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _first_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _strip_markup(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [_first_text(item) for item in value]
        return next((part for part in parts if part), "")
    if isinstance(value, dict):
        for key in (
            "title",
            "heading",
            "value",
            "name",
            "display",
            "displayName",
            "text",
            "date",
            "issued",
            "id",
            "@id",
        ):
            text = _first_text(value.get(key))
            if text:
                return text
    return ""


def _join_text(value: Any, *, limit: int = 3) -> str:
    parts: list[str] = []
    for item in _as_list(value):
        text = _first_text(item)
        if text and text not in parts:
            parts.append(text)
        if len(parts) >= limit:
            break
    return "; ".join(parts)


def _parse_date(value: Any) -> datetime | None:
    text = _first_text(value)
    if not text:
        return None
    head = text.split("T", 1)[0].strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(head, fmt)
            return parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _category_for(value: str | None) -> str:
    raw = (value or "").strip().lower()
    return _CATEGORY_ALIASES.get(raw, raw)


def _zone_for(category: str | None) -> str:
    normalized = _category_for(category)
    return _CATEGORY_TO_ZONE.get(normalized, normalized or "trove")


def _normalize_categories(
    *,
    category: str | list[str] | tuple[str, ...] | None,
    zone: str | list[str] | tuple[str, ...] | None,
) -> list[str]:
    raw: Any = category if category is not None else zone
    if raw is None:
        raw = _DEFAULT_CATEGORIES
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    else:
        items = [str(part).strip() for part in raw]

    normalized: list[str] = []
    for item in items:
        category_name = _category_for(item)
        if category_name and category_name not in normalized:
            normalized.append(category_name)
    return normalized or list(_DEFAULT_CATEGORIES)


def _safe_extra_params(knobs: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, value in knobs.items():
        if value is None:
            continue
        if key in _BLOCKED_EXTRA_PARAMS:
            continue
        if key.startswith("l-") or key in _SAFE_EXTRA_PARAMS:
            params[key] = ",".join(str(v) for v in value) if isinstance(value, list) else value
    return params


async def _request_json(
    url: str,
    *,
    params: dict[str, Any],
    timeout: float,
) -> dict[str, Any] | list[Any] | None:
    await _rate_limit_gate()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("trove request failed for %s: %s", url, exc)
        return None

    if response.status_code != 200:
        logger.warning("trove request returned HTTP %s for %s", response.status_code, url)
        return None
    try:
        payload = response.json()
    except ValueError:
        logger.warning("trove request returned non-JSON response for %s", url)
        return None
    if isinstance(payload, (dict, list)):
        return payload
    return None


def _category_code(category: dict[str, Any]) -> str:
    for key in ("code", "category", "name"):
        text = _first_text(category.get(key))
        if text:
            return _category_for(text)
    return ""


def _records_container(category: dict[str, Any]) -> dict[str, Any] | list[Any]:
    records = category.get("records")
    if isinstance(records, (dict, list)):
        return records
    return category


def _iter_records_from_container(
    container: dict[str, Any] | list[Any],
    *,
    category: str,
) -> list[tuple[str, str, dict[str, Any]]]:
    found: list[tuple[str, str, dict[str, Any]]] = []
    if isinstance(container, list):
        for item in container:
            if isinstance(item, dict):
                found.append((category, "record", item))
        return found

    for key in _RECORD_KEYS:
        value = container.get(key)
        for item in _as_list(value):
            if isinstance(item, dict):
                singular = key[:-1] if key.endswith("s") else key
                found.append((category, singular, item))
    return found


def _iter_search_records(
    payload: dict[str, Any] | list[Any],
) -> list[tuple[str, str, dict[str, Any]]]:
    if isinstance(payload, list):
        return _iter_records_from_container(payload, category="")

    root: Any = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    categories = _as_list(root.get("category") or root.get("categories"))
    if categories:
        found: list[tuple[str, str, dict[str, Any]]] = []
        for cat in categories:
            if not isinstance(cat, dict):
                continue
            category = _category_code(cat)
            found.extend(
                _iter_records_from_container(_records_container(cat), category=category)
            )
        return found

    return _iter_records_from_container(root, category="")


def _extract_id_from_url(value: str) -> str:
    for pattern in (
        r"/work/([^/?#]+)",
        r"/v3/(?:work|newspaper|gazette|magazine)/([^/?#]+)",
        r"/(?:newspaper|gazette)/(?:r/)?article/(\d+)",
        r"nla\.news-article(\d+)",
        r"(nla\.obj-\d+)",
    ):
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    return ""


def _record_id(record: dict[str, Any]) -> str:
    for key in ("id", "@id", "trove_id", "troveId", "work_id", "articleId"):
        text = _first_text(record.get(key))
        if text:
            return text
    for key in ("url", "troveUrl", "trove_url", "recordUrl"):
        text = _first_text(record.get(key))
        if text:
            found = _extract_id_from_url(text)
            if found:
                return found
    return ""


def _record_title(record: dict[str, Any], *, category: str, record_key: str) -> str:
    if category in {"newspaper", "gazette", "magazine"} or record_key == "article":
        heading = _first_text(record.get("heading"))
        if heading:
            return heading
    for key in ("title", "name", "displayTitle", "heading"):
        text = _first_text(record.get(key))
        if text:
            return text
    return "Trove record"


def _record_pub_date(record: dict[str, Any]) -> str:
    for key in ("date", "issued", "publicationDate", "firstdate", "lastdate", "year"):
        text = _first_text(record.get(key))
        if text:
            return text
    return ""


def _record_snippet(record: dict[str, Any]) -> str:
    for key in ("snippet", "abstract", "description", "summary", "note"):
        text = _join_text(record.get(key))
        if text:
            return text
    parts = []
    for key in ("title", "heading", "creator", "contributor", "type", "format"):
        text = _join_text(record.get(key), limit=2)
        if text:
            parts.append(text)
    return "; ".join(parts[:3])


def _public_url_from_api(url: str, *, category: str, trove_id: str, record_key: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc != "api.trove.nla.gov.au":
        return url
    return _fallback_public_url(category=category, trove_id=trove_id, record_key=record_key)


def _fallback_public_url(*, category: str, trove_id: str, record_key: str) -> str:
    if not trove_id:
        return _SITE_BASE
    if category in {"newspaper", "gazette"} or record_key == "article":
        return f"{_SITE_BASE}/newspaper/article/{trove_id}"
    return f"{_SITE_BASE}/work/{trove_id}"


def _record_url(record: dict[str, Any], *, category: str, record_key: str, trove_id: str) -> str:
    for key in ("troveUrl", "trove_url", "url", "recordUrl", "landingPage"):
        text = _first_text(record.get(key))
        if text:
            return _public_url_from_api(
                text, category=category, trove_id=trove_id, record_key=record_key
            )
    return _fallback_public_url(category=category, trove_id=trove_id, record_key=record_key)


def _holding_label(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    has_holding_shape = any(
        key in value
        for key in ("nuc", "nucSymbol", "shortName", "library", "holdingLibrary")
    ) or ("name" in value and "title" not in value)
    if not has_holding_shape:
        return ""
    nuc = _first_text(value.get("nuc") or value.get("id") or value.get("nucSymbol"))
    name = _first_text(value.get("name") or value.get("shortName") or value.get("library"))
    if nuc and name:
        return f"{name} ({nuc})"
    return name or nuc


def _extract_holdings(value: Any, *, _depth: int = 0) -> list[str]:
    if _depth > 5:
        return []
    holdings: list[str] = []
    if isinstance(value, list):
        for item in value:
            for label in _extract_holdings(item, _depth=_depth + 1):
                if label not in holdings:
                    holdings.append(label)
        return holdings
    if not isinstance(value, dict):
        return []

    label = _holding_label(value)
    if label:
        holdings.append(label)
    for key in (
        "holding",
        "holdings",
        "holdingLibraries",
        "holding_libraries",
        "libraries",
        "library",
        "version",
        "versions",
    ):
        for label in _extract_holdings(value.get(key), _depth=_depth + 1):
            if label not in holdings:
                holdings.append(label)
    return holdings


def _url_from_link(value: Any) -> str:
    if isinstance(value, str):
        return value.strip() if value.startswith(("http://", "https://")) else ""
    if not isinstance(value, dict):
        return ""
    link_type = _first_text(value.get("linktype") or value.get("linkType")).lower()
    candidate = _first_text(
        value.get("url")
        or value.get("href")
        or value.get("value")
        or value.get("link")
        or value.get("identifier")
    )
    if not candidate.startswith(("http://", "https://")):
        return ""
    if link_type and link_type not in {"fulltext", "restricted", "viewcopy"}:
        return ""
    return candidate


def _extract_fulltext_url(record: dict[str, Any], *, public_url: str, category: str) -> str | None:
    for key in ("fulltext_url", "fullTextUrl", "fulltextUrl", "trovePageUrl", "pdf"):
        text = _first_text(record.get(key))
        if text.startswith(("http://", "https://")):
            return text
    for key in ("identifier", "identifiers", "link", "links"):
        for item in _as_list(record.get(key)):
            url = _url_from_link(item)
            if url:
                return url
    if category in {"newspaper", "gazette", "magazine"} and public_url:
        return public_url
    return None


def _search_result_from_record(
    category: str,
    record_key: str,
    record: dict[str, Any],
) -> SearchResult | None:
    category = _category_for(category) or _category_for(_first_text(record.get("category")))
    trove_id = _record_id(record)
    title = _record_title(record, category=category, record_key=record_key)
    url = _record_url(record, category=category, record_key=record_key, trove_id=trove_id)
    if not url:
        return None
    pub_date = _record_pub_date(record)
    holdings = _extract_holdings(record)
    fulltext_url = _extract_fulltext_url(record, public_url=url, category=category)
    extras = {
        "trove_id": trove_id or None,
        "zone": _zone_for(category),
        "category": category or None,
        "pub_date": pub_date or None,
        "holding_libraries": holdings,
        "fulltext_url": fulltext_url,
        "metadata_only": True,
    }
    return SearchResult(
        url=url,
        title=title,
        snippet=_record_snippet(record) or "Trove metadata result",
        published_at=_parse_date(pub_date),
        source_kind=KIND,  # type: ignore[arg-type]
        extras=extras,
    )


async def search(
    query: str,
    *,
    max_results: int = 20,
    category: str | list[str] | tuple[str, ...] | None = None,
    zone: str | list[str] | tuple[str, ...] | None = None,
    sortby: str | None = None,
    timeout: float = 30.0,
    **knobs: Any,
) -> list[SearchResult]:
    """Search Trove v3 metadata. Full-text bodies are never requested."""
    if max_results <= 0:
        return []
    categories = _normalize_categories(category=category, zone=zone)
    params: dict[str, Any] = {
        "q": query,
        "category": ",".join(categories),
        "encoding": "json",
        "n": min(max_results, _PAGE_SIZE_CAP),
    }
    if sortby:
        params["sortby"] = sortby
    params.update(_safe_extra_params(knobs))

    payload = await _request_json(_RESULT_URL, params=params, timeout=timeout)
    if payload is None:
        return []

    results: list[SearchResult] = []
    seen: set[str] = set()
    for category_code, record_key, record in _iter_search_records(payload):
        result = _search_result_from_record(category_code, record_key, record)
        if result is None:
            continue
        dedupe_key = result.url or str(result.extras.get("trove_id") or "")
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        results.append(result)
        if len(results) >= max_results:
            break
    return results


def _classify_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path

    if host == "api.trove.nla.gov.au":
        match = _API_RECORD_RE.match(path)
        if match:
            return match.group("kind"), match.group("id")

    if host in {"trove.nla.gov.au", "www.trove.nla.gov.au"}:
        for kind, pattern in (
            ("work", _WORK_URL_RE),
            ("newspaper", _NEWSPAPER_URL_RE),
            ("gazette", _GAZETTE_URL_RE),
            ("magazine", _MAGAZINE_URL_RE),
        ):
            match = pattern.match(path)
            if match:
                return kind, match.group("id")

    if host in {"nla.gov.au", "www.nla.gov.au"}:
        match = _NLA_NEWS_RE.search(path)
        if match:
            return "newspaper", match.group("id")
        obj_match = _NLA_OBJ_RE.search(path)
        if obj_match:
            return "work", obj_match.group(0)

    return None


def _unwrap_record(payload: dict[str, Any] | list[Any], record_type: str) -> dict[str, Any]:
    if isinstance(payload, list):
        return next((item for item in payload if isinstance(item, dict)), {})
    for key in (record_type, "work", "article", "record"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def _infer_category(record: dict[str, Any], record_type: str) -> str:
    explicit = _category_for(_first_text(record.get("category")))
    if explicit in {"book", "newspaper", "gazette", "magazine", "image", "music"}:
        return explicit
    if record_type in {"newspaper", "gazette", "magazine"}:
        return record_type
    formats = " ".join(
        text.lower()
        for text in (
            _join_text(record.get("type"), limit=5),
            _join_text(record.get("format"), limit=5),
        )
        if text
    )
    if any(word in formats for word in ("photograph", "image", "picture", "map")):
        return "image"
    if any(word in formats for word in ("sound", "audio", "music", "interview")):
        return "music"
    if any(word in formats for word in ("periodical", "magazine", "newspaper")):
        return "magazine"
    return "book" if record_type == "work" else record_type


def _metadata_markdown(
    *,
    title: str,
    trove_id: str,
    zone: str,
    pub_date: str,
    url: str,
    fulltext_url: str | None,
    holdings: list[str],
) -> str:
    lines = [f"# {title}", "", "Trove metadata-only record.", ""]
    lines.append(f"- Trove ID: {trove_id or 'unknown'}")
    lines.append(f"- Zone: {zone or 'unknown'}")
    if pub_date:
        lines.append(f"- Published: {pub_date}")
    lines.append(f"- URL: {url}")
    if fulltext_url:
        lines.append(f"- Full-text URL (operator-controlled): {fulltext_url}")
    if holdings:
        lines.append(f"- Holding libraries: {', '.join(holdings)}")
    lines.append("")
    lines.append(
        "Full-text bodies are intentionally not fetched or inlined by this "
        "connector."
    )
    return "\n".join(lines)


async def fetch(url: str, timeout: float = 30.0) -> Source | None:
    """Fetch Trove metadata for a public or API URL without article text."""
    classified = _classify_url(url)
    if classified is None:
        return None
    record_type, trove_id = classified
    api_url = f"{_API_BASE}/{record_type}/{trove_id}"
    payload = await _request_json(api_url, params={"encoding": "json"}, timeout=timeout)
    if payload is None:
        return None

    record = _unwrap_record(payload, record_type)
    if not record:
        return None
    category = _infer_category(record, record_type)
    title = _record_title(record, category=category, record_key=record_type)
    pub_date = _record_pub_date(record)
    holdings = _extract_holdings(record)
    public_url = _record_url(
        record,
        category=category,
        record_key=record_type,
        trove_id=trove_id,
    )
    if public_url == _SITE_BASE:
        public_url = url
    fulltext_url = _extract_fulltext_url(record, public_url=public_url, category=category)
    zone = _zone_for(category)
    metadata = {
        "trove_id": trove_id,
        "zone": zone,
        "category": category,
        "pub_date": pub_date or None,
        "holding_libraries": holdings,
        "fulltext_url": fulltext_url,
        "metadata_only": True,
    }
    return Source(
        url=url,
        title=title,
        cleaned_text=_metadata_markdown(
            title=title,
            trove_id=trove_id,
            zone=zone,
            pub_date=pub_date,
            url=url,
            fulltext_url=fulltext_url,
            holdings=holdings,
        ),
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind=KIND,  # type: ignore[arg-type]
        metadata=metadata,
    )


def reset_for_tests() -> None:
    global _last_call_monotonic
    _last_call_monotonic = None


class _PayloadSchema(_BaseSearchPayload):
    max_results: int | None = None
    category: str | list[str] | None = None
    zone: str | list[str] | None = None
    sortby: str | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=("trove.nla.gov.au", "api.trove.nla.gov.au"),
    description=(
        "Trove / National Library of Australia metadata for newspapers, books,"
        " photos, magazines, oral histories; metadata-only default"
    ),
    optional_payload_knobs="`category`, `zone`, `sortby`",
    example_query="White Australia Policy 1901",
    module_name="trove",
)


__all__ = ["KIND", "fetch", "reset_for_tests", "search"]
