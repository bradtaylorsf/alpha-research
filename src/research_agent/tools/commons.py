"""Wikimedia Commons connector (issue #233, A11).

Public surface:

* ``async def search(query, *, max_results=20) -> list[SearchResult]`` hits the
  Commons MediaWiki Action API with ``action=query&list=search`` in File
  namespace, then enriches each hit through ``prop=imageinfo`` so license and
  media URLs are present on every returned result.
* ``async def fetch(url) -> Source | None`` normalizes Commons File,
  Special:FilePath, and upload.wikimedia.org URLs to a File title and returns a
  metadata-card Source. The critical field is ``Source.metadata["license"]``.

No auth required. Wikimedia traffic goes through the shared MediaWiki helper's
project-identifying User-Agent and 1 RPS host-family limiter.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote, urlparse

from research_agent.tools import _mediawiki
from research_agent.tools._registry import (
    BaseSearchPayload as _BaseSearchPayload,
)
from research_agent.tools._registry import (
    register_kind as _register_kind,
)
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

KIND = "commons_search"

_API_URL = "https://commons.wikimedia.org/w/api.php"
_COMMONS_BASE = "https://commons.wikimedia.org"
_COMMONS_HOST = "commons.wikimedia.org"
_UPLOAD_HOST = "upload.wikimedia.org"
_FILE_PREFIX_RE = re.compile(r"^(?:File|Image):", re.IGNORECASE)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _ensure_file_title(value: str) -> str:
    title = unquote(value).strip().replace("_", " ")
    title = title.lstrip("/")
    if _FILE_PREFIX_RE.match(title):
        return f"File:{title.split(':', 1)[1].strip()}"
    return f"File:{title}"


def _file_title_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if host == _COMMONS_HOST:
        wiki_prefix = "/wiki/"
        if path.startswith(wiki_prefix):
            title = unquote(path[len(wiki_prefix) :])
            if title.startswith("Special:FilePath/"):
                return _ensure_file_title(title.split("/", 1)[1])
            if _FILE_PREFIX_RE.match(title):
                return _ensure_file_title(title)
        if path.endswith("/w/api.php"):
            return ""

    if host == _UPLOAD_HOST:
        parts = [part for part in path.split("/") if part]
        if not parts:
            return ""
        filename = parts[-2] if "thumb" in parts and len(parts) >= 2 else parts[-1]
        return _ensure_file_title(filename)

    return ""


def _file_page_url(title: str) -> str:
    return f"{_COMMONS_BASE}/wiki/{quote(title.replace(' ', '_'), safe=':/')}"


def _first_imageinfo(page: dict[str, Any]) -> dict[str, Any]:
    imageinfo = page.get("imageinfo")
    if isinstance(imageinfo, list):
        first = next((item for item in imageinfo if isinstance(item, dict)), None)
        return first or {}
    return {}


def _extmetadata(info: dict[str, Any]) -> dict[str, Any]:
    raw = info.get("extmetadata")
    return raw if isinstance(raw, dict) else {}


def _metadata_from_page(page: dict[str, Any]) -> dict[str, Any]:
    title = str(page.get("title") or "").strip()
    info = _first_imageinfo(page)
    ext = _extmetadata(info)

    license_code = _mediawiki.extmetadata_text(ext, "License")
    license_short = _mediawiki.extmetadata_text(ext, "LicenseShortName", "UsageTerms")
    usage_terms = _mediawiki.extmetadata_text(ext, "UsageTerms")
    license_value = license_code or license_short or usage_terms

    metadata: dict[str, Any] = {
        "commons_title": title,
        "page_id": page.get("pageid"),
        "mime_type": str(info.get("mime") or "").strip(),
        "media_type": str(info.get("mediatype") or "").strip(),
        "original_url": str(info.get("url") or "").strip(),
        "thumb_url": str(info.get("thumburl") or "").strip(),
        "description_url": str(info.get("descriptionurl") or _file_page_url(title)).strip(),
        "author": _mediawiki.extmetadata_text(
            ext, "Artist", "Author", "Attribution", "Credit"
        ),
        "license": license_value,
        "license_short": license_short or license_value,
        "usage_terms": usage_terms,
        "license_url": _mediawiki.extmetadata_text(ext, "LicenseUrl"),
        "description": _mediawiki.extmetadata_text(
            ext, "ImageDescription", "ObjectName", "Headline"
        ),
        "credit": _mediawiki.extmetadata_text(ext, "Credit"),
        "date": _mediawiki.extmetadata_text(ext, "DateTimeOriginal", "DateTime"),
    }
    return metadata


async def _imageinfo_for_titles(
    titles: list[str],
    *,
    timeout: float,
) -> dict[str, dict[str, Any]]:
    if not titles:
        return {}
    payload = await _mediawiki.request_json(
        _API_URL,
        params={
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "prop": "imageinfo",
            "titles": "|".join(titles[:50]),
            "iiprop": "url|mime|mediatype|extmetadata",
            "iiurlwidth": "640",
        },
        timeout=timeout,
    )
    if payload is None:
        return {}
    query_root = payload.get("query")
    pages = query_root.get("pages") if isinstance(query_root, dict) else None
    if not isinstance(pages, list):
        logger.warning("commons imageinfo response missing query.pages")
        return {}
    enriched: dict[str, dict[str, Any]] = {}
    for page in pages:
        if not isinstance(page, dict) or page.get("missing"):
            continue
        title = str(page.get("title") or "").strip()
        if not title:
            continue
        enriched[title] = page
    return enriched


def _search_result_from_hit(
    hit: dict[str, Any],
    *,
    page: dict[str, Any] | None,
) -> SearchResult | None:
    raw_title = str(hit.get("title") or "").strip()
    if not raw_title:
        return None
    title = _ensure_file_title(raw_title)
    metadata = _metadata_from_page(page or {"title": title})
    if not metadata.get("license"):
        logger.debug("commons search result missing license metadata: %s", title)
        return None
    url = metadata.get("description_url") or _file_page_url(title)
    snippet = _mediawiki.clean_text(hit.get("snippet")) or metadata.get("description") or (
        f"Wikimedia Commons media file licensed {metadata['license']}"
    )
    return SearchResult(
        url=str(url),
        title=title,
        snippet=str(snippet),
        published_at=_parse_timestamp(hit.get("timestamp")),
        source_kind=KIND,
        extras={
            "commons_title": title,
            "mime_type": metadata.get("mime_type") or "",
            "original_url": metadata.get("original_url") or "",
            "thumb_url": metadata.get("thumb_url") or "",
            "author": metadata.get("author") or "",
            "license": metadata.get("license") or "",
            "license_short": metadata.get("license_short") or "",
            "metadata": metadata,
        },
    )


async def search(
    query: str,
    *,
    max_results: int = 20,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Search Wikimedia Commons File namespace and return license-enriched media hits."""
    q = query.strip()
    if not q or max_results <= 0:
        return []

    payload = await _mediawiki.request_json(
        _API_URL,
        params={
            "action": "query",
            "list": "search",
            "srsearch": q,
            "srnamespace": "6",
            "srlimit": min(max_results, 50),
            "srprop": "snippet|timestamp|titlesnippet",
            "format": "json",
            "formatversion": "2",
        },
        timeout=timeout,
    )
    if payload is None:
        return []
    query_root = payload.get("query")
    hits = query_root.get("search") if isinstance(query_root, dict) else None
    if not isinstance(hits, list):
        logger.warning("commons search response missing query.search")
        return []

    titles: list[str] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        title = str(hit.get("title") or "").strip()
        if title:
            titles.append(_ensure_file_title(title))
    enriched = await _imageinfo_for_titles(titles, timeout=timeout)

    results: list[SearchResult] = []
    seen: set[str] = set()
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        title = _ensure_file_title(str(hit.get("title") or ""))
        if title in seen:
            continue
        result = _search_result_from_hit(hit, page=enriched.get(title))
        if result is None:
            continue
        seen.add(title)
        results.append(result)
        if len(results) >= max_results:
            break
    return results


def _source_markdown(title: str, metadata: dict[str, Any]) -> str:
    lines = [f"# {title}", "", "Wikimedia Commons media metadata."]
    description = metadata.get("description")
    if description:
        lines.extend(["", str(description)])
    lines.extend(
        [
            "",
            "## Rights and reuse",
            f"- License: {metadata.get('license') or ''}",
            f"- License short name: {metadata.get('license_short') or ''}",
            f"- License URL: {metadata.get('license_url') or ''}",
            f"- Author: {metadata.get('author') or ''}",
            "",
            "## Media",
            f"- MIME type: {metadata.get('mime_type') or ''}",
            f"- Original URL: {metadata.get('original_url') or ''}",
            f"- Thumbnail URL: {metadata.get('thumb_url') or ''}",
            f"- Commons page: {metadata.get('description_url') or ''}",
        ]
    )
    return "\n".join(lines).strip() + "\n"


async def fetch(url: str, *, timeout: float = 15.0) -> Source | None:
    """Fetch Commons file metadata and return a citation-ready Source."""
    title = _file_title_from_url(url)
    if not title:
        return None
    enriched = await _imageinfo_for_titles([title], timeout=timeout)
    page = enriched.get(title)
    if page is None:
        return None
    metadata = _metadata_from_page(page)
    if not metadata.get("license"):
        logger.debug("commons fetch missing license metadata: %s", title)
        return None
    source_url = str(metadata.get("description_url") or _file_page_url(title))
    return Source(
        url=source_url,
        title=title,
        cleaned_text=_source_markdown(title, metadata),
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind=KIND,
        metadata=metadata,
    )


def reset_for_tests() -> None:
    """Clear shared MediaWiki limiter state. Test-only."""
    _mediawiki.reset_for_tests()


class _PayloadSchema(_BaseSearchPayload):
    max_results: int | None = None
    timeout: float | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=("commons.wikimedia.org", "upload.wikimedia.org", "wikimedia.org"),
    skill_name="commons",
    description=(
        "Wikimedia Commons free media files with imageinfo license, author,"
        " MIME type, original URL, and thumbnail metadata"
    ),
    optional_payload_knobs="`max_results`",
    example_query="Algerian war photographs",
    module_name="commons",
)


__all__ = ["KIND", "fetch", "reset_for_tests", "search"]
