"""Library of Congress (loc.gov) connector (issue #224, A1+A2).

Public surface:

* ``async def search(query, *, collection=None, max_results=20, page=1) -> list[SearchResult]``
  hits ``https://www.loc.gov/{collection_path or "search"}/?fo=json`` and
  parses the unified ``results[]`` array.
* ``async def fetch(url) -> Source | None`` opens an LoC item or resource
  page, returning a :class:`Source`. For Chronicling America newspaper
  pages (``/resource/<lccn>/<date>/...``) the per-page OCR text is
  fetched from the ``fulltext_service`` URL and placed in
  :attr:`Source.cleaned_text` so downstream FTS5 / embeddings can
  retrieve it. Image-bearing surfaces (prints, maps) carry the IIIF
  image URL and a synthesized IIIF manifest URL in
  :attr:`Source.metadata`.

The Library of Congress retired the standalone Chronicling America API
on 2025-08-04; the collection now lives under the unified loc.gov JSON
API at ``www.loc.gov``. This connector deliberately points at the new
host — never the deprecated ``chroniclingamerica.loc.gov``.

No auth required (public API). Polite per-host rate of 1 RPS.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from research_agent import config
from research_agent.tools import archive
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

KIND = "loc_search"

_BASE_URL = "https://www.loc.gov"
_HOST = "www.loc.gov"
# AC: polite per-host rate of 1 RPS.
_RATE_LIMIT_INTERVAL = 1.0
_CACHE_DIR = Path("corpus/.cache/loc")

# Maps the operator-facing ``collection`` knob to the loc.gov endpoint that
# returns the standard ``results`` shape. ``/pictures/`` returns a
# landing-page payload (no ``results`` array) when queried directly, so
# ``prints`` routes to ``/photos/`` which exposes the same searchable
# format-portal at the unified shape. ``recordings`` routes to ``/audio/``
# for the same reason. Unrecognised values fall through as raw collection
# slugs under ``/collections/<slug>/``.
_COLLECTION_PATHS: dict[str, str] = {
    "chronicling-america": "collections/chronicling-america",
    "prints": "photos",
    "manuscripts": "manuscripts",
    "recordings": "audio",
    "maps": "maps",
}

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


# ---------------------------------------------------------------------------
# Headers / rate limit
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": config.get("RESEARCH_USER_AGENT")
        or "research-agent/0.1 (+local; contact unset)",
    }


async def _rate_limit_gate() -> None:
    """Block until at least ``_RATE_LIMIT_INTERVAL`` has passed since the last call."""
    global _last_call_monotonic
    async with _rate_lock:
        if _last_call_monotonic is not None:
            elapsed = time.monotonic() - _last_call_monotonic
            wait = _RATE_LIMIT_INTERVAL - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
        _last_call_monotonic = time.monotonic()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_collection_path(collection: str | None) -> str:
    """Return the URL path segment for a ``collection`` knob value."""
    if not collection:
        return "search"
    mapped = _COLLECTION_PATHS.get(collection)
    if mapped is not None:
        return mapped
    # Treat unknown values as a raw collection slug — supports the long tail
    # of LoC collection names without hard-coding every one.
    return f"collections/{collection}"


def _parse_loc_date(value: Any) -> datetime | None:
    """Parse the LoC ``date`` / ``dates[0]`` field into a tz-aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, list):
        for entry in value:
            parsed = _parse_loc_date(entry)
            if parsed is not None:
                return parsed
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    head = text[:4]
    if len(head) == 4 and head.isdigit():
        try:
            return datetime(int(head), 1, 1, tzinfo=UTC)
        except ValueError:
            return None
    return None


def _normalize_url(raw: Any) -> str:
    """Normalize protocol-relative / http URLs from loc.gov to https."""
    if not isinstance(raw, str) or not raw:
        return ""
    if raw.startswith("//"):
        return f"https:{raw}"
    if raw.startswith("http://www.loc.gov") or raw.startswith("http://loc.gov"):
        return "https://" + raw[len("http://") :]
    return raw


def _first_str(value: Any) -> str:
    """Return the first non-empty string from ``value`` (which may be a list)."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, str) and entry.strip():
                return entry.strip()
    return ""


def _strip_image_url(url: str) -> str:
    """Drop the ``#h=...&w=...`` fragment LoC appends to image URLs."""
    if not isinstance(url, str):
        return ""
    return url.split("#", 1)[0]


def _build_search_result(
    hit: dict[str, Any], *, collection: str | None
) -> SearchResult | None:
    raw_url = _normalize_url(hit.get("url") or hit.get("id"))
    if not raw_url:
        return None
    title = _first_str(hit.get("title"))
    if not title:
        return None
    snippet = _first_str(hit.get("description"))
    if len(snippet) > 400:
        snippet = snippet[:400].rstrip() + "…"
    if not snippet:
        # Some LoC results omit description but carry ``subject`` /
        # ``original_format``; build a graceful fallback so the planner
        # still sees a non-empty snippet.
        subj = hit.get("subject") or hit.get("subjects") or []
        if isinstance(subj, list):
            snippet = ", ".join(s for s in subj if isinstance(s, str))[:400]

    published = _parse_loc_date(hit.get("date") or hit.get("dates"))

    image_urls = hit.get("image_url") or []
    image_url = ""
    if isinstance(image_urls, list) and image_urls:
        image_url = _strip_image_url(image_urls[0])

    extras: dict[str, Any] = {
        "collection": collection or "",
        "item_id": hit.get("id") or "",
        "image_url": image_url,
        "mime_type": hit.get("mime_type") or [],
        "original_format": hit.get("original_format") or [],
        "online_format": hit.get("online_format") or [],
        "partof": hit.get("partof") or [],
        "site": hit.get("site") or [],
    }

    return SearchResult(
        url=raw_url,
        title=title,
        snippet=snippet,
        published_at=published,
        source_kind="loc",
        extras=extras,
    )


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def search(
    query: str,
    *,
    collection: str | None = None,
    max_results: int = 20,
    page: int = 1,
    timeout: float = 15.0,
    **knobs: Any,
) -> list[SearchResult]:
    """Run a loc.gov search and return up to ``max_results`` hits.

    ``collection`` selects the sub-surface: ``chronicling-america`` for
    historical US newspapers (1690–1963, OCR text in ``description``),
    ``prints`` / ``manuscripts`` / ``recordings`` / ``maps`` for the
    media-format portals, or ``None`` for an all-collections search.

    Pagination uses ``sp=<page>`` (1-indexed, per the loc.gov request
    spec). Returns ``[]`` on transport / HTTP error / non-JSON body —
    connector failures must never crash the planner.
    """
    if knobs:
        # Accept and ignore extra planner-threaded kwargs so the
        # orchestrator's payload passthrough doesn't TypeError on
        # connector-agnostic fields like ``sub_question``.
        logger.debug("loc.search: ignoring extra knobs %r", sorted(knobs))

    await _rate_limit_gate()

    path = _resolve_collection_path(collection)
    url = f"{_BASE_URL}/{path}/"
    params: dict[str, str | int] = {
        "fo": "json",
        "q": query,
        "sp": int(page),
        "c": int(max_results),
    }
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("loc search failed for %r: %s", query, exc)
        return []

    if response.status_code != 200:
        logger.warning(
            "loc search returned HTTP %s for %r (collection=%r)",
            response.status_code,
            query,
            collection,
        )
        return []

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("loc search returned non-JSON for %r: %s", query, exc)
        return []

    raw_hits = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(raw_hits, list):
        return []

    out: list[SearchResult] = []
    for hit in raw_hits[:max_results]:
        if not isinstance(hit, dict):
            continue
        result = _build_search_result(hit, collection=collection)
        if result is not None:
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def _classify_url(url: str) -> str | None:
    """Return a kind tag (``"item"``, ``"resource"``, ``"collection"``) when
    ``url`` is a fetch-able loc.gov page, else ``None``.

    Strict host match: only ``www.loc.gov`` passes. Look-alike hosts like
    ``www.loc.gov.attacker.example`` would otherwise leak through and
    the resulting :class:`Source` would carry the attacker domain in
    :attr:`Source.url`.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    if host != _HOST:
        return None
    path = (parsed.path or "").lower()
    if path.startswith("/item/"):
        return "item"
    if path.startswith("/resource/"):
        return "resource"
    if path.startswith("/collections/"):
        return "collection"
    return None


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return _CACHE_DIR / f"{digest}.json"


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def _load_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _http_get_json(
    url: str, params: dict[str, Any] | None, timeout: float
) -> tuple[int | None, dict[str, Any] | None]:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("loc fetch failed for %s: %s", url, exc)
        return None, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


def _resource_canonical_url(url: str) -> str:
    """Strip the ``?fo=json`` query so the cached / Wayback-saved URL is
    the human-facing one, not the API view of it."""
    parsed = urlparse(url)
    if parsed.query and "fo=json" in parsed.query:
        # Reconstruct the URL minus the API knob.
        from urllib.parse import parse_qsl, urlencode, urlunparse

        kept = [(k, v) for k, v in parse_qsl(parsed.query) if k != "fo"]
        new_query = urlencode(kept)
        return urlunparse(parsed._replace(query=new_query))
    return url


async def _fetch_chronam_ocr(
    payload: dict[str, Any], timeout: float
) -> str:
    """For a chronam page payload, follow ``fulltext_service`` and return
    the concatenated OCR text. Returns ``""`` when no OCR is available."""
    ft_url = payload.get("fulltext_service")
    if not isinstance(ft_url, str) or not ft_url:
        return ""
    await _rate_limit_gate()
    status, ft_payload = await _http_get_json(ft_url, None, timeout)
    if not isinstance(ft_payload, dict):
        return ""
    pieces: list[str] = []
    for value in ft_payload.values():
        if isinstance(value, dict):
            text = value.get("full_text")
            if isinstance(text, str) and text.strip():
                pieces.append(text.strip())
    return "\n\n".join(pieces)


def _spawn_wayback_save(canonical_url: str) -> None:
    """Fire a non-blocking Wayback Save Page Now against ``canonical_url``.

    Mirrors ``web_fetch._spawn_archive_task``'s fire-and-forget contract:
    failures land in the debug log, and the task never blocks ``fetch``.
    Returns immediately when no event loop is running (e.g. unit tests
    that exercise ``fetch`` outside ``asyncio.run``).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _runner() -> None:
        try:
            await archive.save(canonical_url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("loc wayback save raised for %s: %s", canonical_url, exc)

    loop.create_task(_runner())


def _extract_image_metadata(
    payload: dict[str, Any], canonical_url: str, kind: str
) -> dict[str, str]:
    """Return ``{"image_url": ..., "image_iiif_manifest": ...}`` (empty
    strings when absent) for image-bearing surfaces."""
    item = payload.get("item") if isinstance(payload, dict) else None
    if not isinstance(item, dict):
        item = {}

    image_url = ""
    raw_imgs = item.get("image_url") or payload.get("image_url") or []
    if isinstance(raw_imgs, list) and raw_imgs:
        image_url = _strip_image_url(raw_imgs[0])
    elif isinstance(raw_imgs, str) and raw_imgs:
        image_url = _strip_image_url(raw_imgs)

    if not image_url:
        resources = payload.get("resources") or []
        if isinstance(resources, list) and resources:
            first = resources[0]
            if isinstance(first, dict):
                cand = first.get("image") or first.get("url") or ""
                if isinstance(cand, str):
                    image_url = _strip_image_url(cand)

    iiif_manifest = ""
    # IIIF Presentation manifests on loc.gov live at
    # ``/item/<id>/manifest.json``; for ``/resource/...`` pages there's
    # no per-page manifest, so skip those.
    if kind == "item":
        iiif_manifest = canonical_url.rstrip("/") + "/manifest.json"

    return {
        "image_url": image_url,
        "image_iiif_manifest": iiif_manifest,
    }


def _build_item_markdown(
    payload: dict[str, Any], canonical_url: str
) -> tuple[str, str]:
    """For non-chronam pages, build (title, cleaned_text) from the JSON."""
    item = payload.get("item") if isinstance(payload, dict) else None
    if not isinstance(item, dict):
        item = {}
    title = _first_str(item.get("title")) or canonical_url
    parts: list[str] = [f"# {title}"]
    dates = item.get("dates") or item.get("date") or item.get("created_published")
    date_str = _first_str(dates)
    if date_str:
        parts.append(f"_{date_str}_")
    description = item.get("description") or []
    if isinstance(description, list):
        for entry in description:
            if isinstance(entry, str) and entry.strip():
                parts.append(entry.strip())
    elif isinstance(description, str) and description.strip():
        parts.append(description.strip())
    summary = _first_str(item.get("summary"))
    if summary and summary not in parts:
        parts.append(summary)
    subjects = item.get("subjects") or item.get("subject_headings") or []
    if isinstance(subjects, list) and subjects:
        rendered = ", ".join(s for s in subjects if isinstance(s, str))
        if rendered:
            parts.append(f"**Subjects:** {rendered}")
    return title, "\n\n".join(p for p in parts if p)


async def fetch(url: str, timeout: float = 30.0) -> Source | None:
    """Open a loc.gov item or resource page and return a :class:`Source`.

    Routing:

    * ``/item/<id>/`` → JSON metadata + optional IIIF image URLs.
    * ``/resource/.../`` (typically chronam) → JSON metadata plus the
      per-page OCR text fetched from ``fulltext_service``. The OCR
      lands in ``Source.cleaned_text`` (not ``metadata``) so FTS5 /
      embeddings can retrieve it.
    * ``/collections/<slug>/`` → collection landing page.

    URLs outside ``www.loc.gov`` (or with non-routable paths) return
    ``None`` rather than raising.
    """
    if not url:
        return None
    kind = _classify_url(url)
    if kind is None:
        return None

    canonical = _resource_canonical_url(url)
    cache = _cache_path(canonical)
    payload = _load_cache(cache)
    if payload is None:
        await _rate_limit_gate()
        status, payload = await _http_get_json(
            canonical, {"fo": "json"}, timeout
        )
        if status is None or status >= 400 or not isinstance(payload, dict):
            if status is not None and status >= 400:
                logger.warning("loc fetch HTTP %s for %s", status, canonical)
            return None
        _write_cache(cache, payload)

    cleaned_text = ""
    title = ""
    if kind == "resource":
        # Chronam page: pull the per-page OCR via fulltext_service.
        ocr = await _fetch_chronam_ocr(payload, timeout)
        item = payload.get("item") if isinstance(payload, dict) else None
        if isinstance(item, dict):
            title = _first_str(item.get("title"))
        if not title:
            title = canonical
        if ocr:
            cleaned_text = ocr
        else:
            # No OCR available — fall back to the descriptive metadata so
            # the Source body isn't empty.
            _, cleaned_text = _build_item_markdown(payload, canonical)
    else:
        title, cleaned_text = _build_item_markdown(payload, canonical)

    if not cleaned_text:
        return None

    metadata: dict[str, Any] = {
        "kind": kind,
        "loc_url": canonical,
    }
    images = _extract_image_metadata(payload, canonical, kind)
    if images.get("image_url"):
        metadata["image_url"] = images["image_url"]
    if images.get("image_iiif_manifest"):
        metadata["image_iiif_manifest"] = images["image_iiif_manifest"]

    # Wayback save attempt on first fetch for HTML surfaces. Skip when
    # the input URL was already a JSON-only API view (``?fo=json``) —
    # there's no human page at that exact URL to snapshot.
    if "fo=json" not in (urlparse(url).query or ""):
        _spawn_wayback_save(canonical)

    return Source(
        url=canonical,
        title=title,
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="loc",
        metadata=metadata,
    )


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


__all__ = ["KIND", "fetch", "reset_for_tests", "search"]
