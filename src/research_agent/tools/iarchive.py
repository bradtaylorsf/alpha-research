"""Internet Archive connector (issue #225).

Public surface:

* ``async def search(query, *, mediatype=None, max_results=20) -> list[SearchResult]``
  hits ``archive.org/advancedsearch.php?output=json``. The optional
  ``mediatype`` knob narrows to ``texts`` (digitized books, periodicals),
  ``audio`` (period radio, oral histories), ``movies`` (archival film), or
  ``web`` (web-archive collections).
* ``async def fetch(url) -> Source | None`` opens an ``archive.org/details/<id>``
  permalink and returns a :class:`Source` whose metadata carries the item
  manifest. For ``texts`` items, ``metadata['fulltext_url']`` points at the
  derivative full-text file so the loop's ``web_fetch`` can pull it. For
  ``audio`` items, ``metadata['audio_files']`` is a list of canonical mp3 /
  flac URLs ready for the audio transcription pipeline.

No auth required. Polite per-host rate of 1 RPS.

Item metadata JSON is cached at ``corpus/.cache/iarchive/item-<identifier>.json``.
A Wayback save is fired (best-effort, non-blocking) on the first uncached
fetch of an HTML detail page.

Internet Archive (the *content* archive) is distinct from Wayback Machine
(the *web* archive). ``tools/archive.py`` covers the latter; this module
covers the former.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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

KIND = "iarchive_search"

_BASE_URL = "https://archive.org/advancedsearch.php"
_SITE_BASE = "https://archive.org/details/"
_METADATA_BASE = "https://archive.org/metadata/"
_DOWNLOAD_BASE = "https://archive.org/download/"
# AC: polite per-host rate of 1 RPS.
_RATE_LIMIT_INTERVAL = 1.0
_CACHE_DIR = Path("corpus/.cache/iarchive")

_VALID_MEDIATYPES = frozenset({"texts", "audio", "movies", "web"})

# Detail permalinks: ``archive.org/details/<identifier>`` (identifier is
# alphanumeric, underscore, dot, hyphen). A trailing slash is allowed.
_DETAIL_URL_RE = re.compile(r"^/details/(?P<identifier>[A-Za-z0-9._-]+)/?$")

# IA's ``format`` strings on file listings — the canonical audio derivatives.
_AUDIO_FORMATS: frozenset[str] = frozenset(
    {"VBR MP3", "MP3", "FLAC", "Ogg Vorbis", "Ogg Audio"}
)
# IA's ``format`` strings used to surface a derivative full-text file.
_TEXT_FORMATS: tuple[str, ...] = ("DjVuTXT", "Text", "Plain Text", "Tex")

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


# ---------------------------------------------------------------------------
# Helpers
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


def _parse_iso_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except ValueError:
            continue
    head = text.split("T", 1)[0]
    try:
        return datetime.strptime(head, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _truncate(text: str, limit: int = 280) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _identifier_permalink(identifier: str) -> str:
    return f"{_SITE_BASE}{identifier}"


def _stringify(value: Any) -> str:
    """Normalize an IA field that may be a string, a list of strings, or absent."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v is not None)
    return str(value)


def _build_search_result(doc: dict[str, Any]) -> SearchResult | None:
    identifier = (doc.get("identifier") or "").strip()
    if not identifier:
        return None
    title = _stringify(doc.get("title")).strip()
    if not title:
        return None

    description = _stringify(doc.get("description"))
    creator = _stringify(doc.get("creator"))
    mediatype = (doc.get("mediatype") or "").strip()
    item_date = _stringify(doc.get("date")).strip()
    publicdate = doc.get("publicdate")
    downloads = doc.get("downloads")

    snippet_parts: list[str] = []
    if creator:
        snippet_parts.append(creator)
    if item_date:
        snippet_parts.append(item_date)
    if mediatype:
        snippet_parts.append(mediatype)
    if description:
        snippet_parts.append(description)
    snippet = _truncate(" — ".join(p for p in snippet_parts if p) or title)

    extras: dict[str, Any] = {
        "identifier": identifier,
        "mediatype": mediatype,
        "downloads": downloads,
        "creator": creator,
        "date": item_date,
    }

    return SearchResult(
        url=_identifier_permalink(identifier),
        title=title,
        snippet=snippet,
        published_at=_parse_iso_date(publicdate),
        source_kind="iarchive",
        extras=extras,
    )


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def search(
    query: str,
    *,
    mediatype: str | None = None,
    max_results: int = 20,
    page: int = 1,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Run an Internet Archive advancedsearch and return up to ``max_results`` hits.

    ``mediatype`` is appended to the Lucene query as ``AND mediatype:<value>``
    when set to one of ``texts``, ``audio``, ``movies``, ``web``. Unknown
    values are ignored (logged at warning) so a planner-drift kwarg can't
    crash the search path.

    Returns ``[]`` on transport / HTTP error / non-JSON body — connector
    failures must never crash the planner.
    """
    q = (query or "").strip()
    if not q:
        return []

    if mediatype is not None:
        if mediatype in _VALID_MEDIATYPES:
            q = f"{q} AND mediatype:{mediatype}"
        else:
            logger.warning(
                "iarchive: ignoring unknown mediatype=%r (valid: %s)",
                mediatype,
                sorted(_VALID_MEDIATYPES),
            )

    # advancedsearch.php expects ``fl[]=<field>`` repeated per field. httpx
    # serializes a list value of a string-keyed param into the matching
    # repeat-key form, so a list of fields is the correct shape here.
    params: list[tuple[str, str | int | float | bool | None]] = [
        ("q", q),
        ("fl[]", "identifier"),
        ("fl[]", "title"),
        ("fl[]", "description"),
        ("fl[]", "creator"),
        ("fl[]", "date"),
        ("fl[]", "mediatype"),
        ("fl[]", "downloads"),
        ("fl[]", "publicdate"),
        ("sort[]", "downloads desc"),
        ("rows", str(max(1, int(max_results)))),
        ("page", str(max(1, int(page)))),
        ("output", "json"),
    ]

    await _rate_limit_gate()

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(_BASE_URL, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("iarchive search failed for %r: %s", query, exc)
        return []

    if response.status_code != 200:
        logger.warning(
            "iarchive search returned HTTP %s for %r",
            response.status_code,
            query,
        )
        return []

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("iarchive search returned non-JSON for %r: %s", query, exc)
        return []

    docs = (
        payload.get("response", {}).get("docs")
        if isinstance(payload, dict)
        else None
    ) or []

    out: list[SearchResult] = []
    for doc in docs[:max_results]:
        if not isinstance(doc, dict):
            continue
        result = _build_search_result(doc)
        if result is not None:
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def _classify_url(url: str) -> str | None:
    """Return the identifier when ``url`` is an ``archive.org/details/<id>`` page."""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    # Strict host match so look-alikes like ``archive.org.attacker.example``
    # don't pass — without it, a Source.url could leak the attacker domain
    # downstream while the body came from a different upstream entirely.
    if host != "archive.org" and host != "www.archive.org":
        return None
    m = _DETAIL_URL_RE.match(parsed.path or "")
    if not m:
        return None
    return m.group("identifier")


def _cache_path(identifier: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", identifier)
    return _CACHE_DIR / f"item-{safe}.json"


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
    url: str, timeout: float
) -> tuple[int | None, dict[str, Any] | None]:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("iarchive fetch failed for %s: %s", url, exc)
        return None, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


def _file_url(identifier: str, name: str) -> str:
    return f"{_DOWNLOAD_BASE}{identifier}/{name}"


def _pick_audio_files(
    identifier: str, files: list[dict[str, Any]]
) -> list[str]:
    out: list[str] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        fmt = (entry.get("format") or "").strip()
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        if fmt in _AUDIO_FORMATS:
            out.append(_file_url(identifier, name))
    return out


def _pick_fulltext_url(
    identifier: str, files: list[dict[str, Any]]
) -> str | None:
    """Return the canonical full-text derivative URL for a ``texts`` item."""
    by_format: dict[str, str] = {}
    by_suffix: dict[str, str] = {}
    for entry in files:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        fmt = (entry.get("format") or "").strip()
        if fmt and fmt not in by_format:
            by_format[fmt] = _file_url(identifier, name)
        lower = name.lower()
        for suffix in ("_djvu.txt", "_text.txt", ".txt"):
            if lower.endswith(suffix) and suffix not in by_suffix:
                by_suffix[suffix] = _file_url(identifier, name)
                break

    for fmt in _TEXT_FORMATS:
        if fmt in by_format:
            return by_format[fmt]
    for suffix in ("_djvu.txt", "_text.txt", ".txt"):
        if suffix in by_suffix:
            return by_suffix[suffix]
    return None


def _build_summary_markdown(
    title: str, item_metadata: dict[str, Any]
) -> str:
    lines = [f"# {title}"]
    creator = _stringify(item_metadata.get("creator"))
    if creator:
        lines.append(f"_Creator: {creator}_")
    item_date = _stringify(item_metadata.get("date"))
    if item_date:
        lines.append(f"_Date: {item_date}_")
    mediatype = _stringify(item_metadata.get("mediatype"))
    if mediatype:
        lines.append(f"_Mediatype: {mediatype}_")
    description = _stringify(item_metadata.get("description")).strip()
    if description:
        lines.append("")
        lines.append(description)
    return "\n".join(lines).strip()


async def fetch(url: str, timeout: float = 30.0) -> Source | None:
    """Open an Internet Archive detail page and return a :class:`Source`.

    The URL is classified via the path ``/details/<identifier>``; any URL
    not matching that shape (or pointing outside ``archive.org``) returns
    ``None``. The item metadata JSON is cached at
    ``corpus/.cache/iarchive/item-<identifier>.json`` so repeated fetches
    skip the network entirely.

    On the first uncached fetch, kicks off a fire-and-forget Wayback save
    of the detail page so the URL is captured even if the IA item is later
    taken down.

    Mediatype-specific metadata:

    * ``texts``  → ``metadata['fulltext_url']`` points at the canonical
      full-text derivative (so the loop's ``web_fetch`` can pull it).
    * ``audio``  → ``metadata['audio_files']`` is a list of mp3 / flac URLs.
    """
    if not url:
        return None
    identifier = _classify_url(url)
    if not identifier:
        return None

    cache = _cache_path(identifier)
    payload = _load_cache(cache)
    is_cache_miss = payload is None
    if is_cache_miss:
        await _rate_limit_gate()
        meta_url = f"{_METADATA_BASE}{identifier}"
        status, payload = await _http_get_json(meta_url, timeout)
        if status is None or status >= 400 or not isinstance(payload, dict):
            if status is not None and status >= 400:
                logger.warning(
                    "iarchive metadata HTTP %s for %s", status, meta_url
                )
            return None
        _write_cache(cache, payload)

    item_metadata = (
        payload.get("metadata") if isinstance(payload, dict) else None
    )
    if not isinstance(item_metadata, dict):
        return None

    title = _stringify(item_metadata.get("title")).strip() or identifier
    mediatype = (item_metadata.get("mediatype") or "").strip()
    files_raw = payload.get("files") if isinstance(payload, dict) else []
    files: list[dict[str, Any]] = (
        [f for f in files_raw if isinstance(f, dict)]
        if isinstance(files_raw, list)
        else []
    )

    cleaned_text = _build_summary_markdown(title, item_metadata)
    if not cleaned_text:
        return None

    metadata: dict[str, Any] = {
        "identifier": identifier,
        "mediatype": mediatype,
        "downloads": item_metadata.get("downloads"),
        "creator": _stringify(item_metadata.get("creator")),
        "date": _stringify(item_metadata.get("date")),
        "publicdate": _stringify(item_metadata.get("publicdate")),
        "collection": item_metadata.get("collection"),
    }

    if mediatype == "texts":
        fulltext = _pick_fulltext_url(identifier, files)
        if fulltext:
            metadata["fulltext_url"] = fulltext
    elif mediatype == "audio":
        metadata["audio_files"] = _pick_audio_files(identifier, files)

    if is_cache_miss:
        # Fire-and-forget Wayback save of the detail page. We don't await:
        # IA detail pages occasionally take 20+s to capture and the loop
        # never blocks on archival. Same fire-and-forget pattern as
        # ``web_fetch._spawn_archive_task``.
        try:
            asyncio.get_running_loop().create_task(archive.save(url))
        except RuntimeError:
            # No running loop (sync test context) — skip silently.
            pass

    return Source(
        url=url,
        title=title,
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="iarchive",
        metadata=metadata,
    )


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


__all__ = ["KIND", "fetch", "reset_for_tests", "search"]
