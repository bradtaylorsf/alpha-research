"""HTTP fetch + content extraction connector (issue #15).

Pipeline per call:

1. Robots.txt check (skipped if ``RESEARCH_IGNORE_ROBOTS=1``).
2. ``httpx`` GET — unless the planner already declared ``requires_js=True``.
3. Run trafilatura over the HTML; if the cleaned text is too short, retry
   with ``readability-lxml`` for academic sites where trafilatura under-
   extracts.
4. If the result is still tiny (< 500 chars), or the server replied with a
   classic anti-bot status (403/429/503), or the planner asked for JS up
   front, fall back to the shared Playwright session and re-extract.
5. Spawn a background Wayback Save Page Now task — failures never block the
   return value.

We deliberately do NOT spawn Playwright per-call: the shared
``tools/browser.py`` session is reused so we don't pay a Chromium launch on
every fetch.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.robotparser
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
import trafilatura
from readability import Document

from research_agent import config
from research_agent.tools import archive, browser
from research_agent.tools.models import Source

logger = logging.getLogger(__name__)

_DEFAULT_USER_AGENT = "research-agent/0.1"
_MIN_TRAFILATURA_CHARS = 200
_MIN_TEXT_CHARS = 500
_BROWSER_FALLBACK_STATUSES = frozenset({403, 429, 503})
_ROBOTS_TIMEOUT = 5.0
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Per-host robots cache. Keyed by ``scheme://host`` so http/https are
# treated separately (they are technically different "origins" for robots).
_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_robots_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _resolve_user_agent() -> str:
    return config.get("RESEARCH_USER_AGENT") or _DEFAULT_USER_AGENT


def _ignore_robots() -> bool:
    raw = config.get("RESEARCH_IGNORE_ROBOTS")
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


async def _fetch_robots_text(robots_url: str, user_agent: str) -> str | None:
    headers = {"User-Agent": user_agent}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=_ROBOTS_TIMEOUT,
            headers=headers,
        ) as client:
            response = await client.get(robots_url)
    except (httpx.HTTPError, OSError):
        return None
    if response.status_code >= 400:
        # RFC 9309: a 4xx generally means "no robots.txt, allow everything."
        return ""
    return response.text


async def _robots_allows(url: str, user_agent: str) -> bool:
    """Return True when ``url`` is fetchable per the host's robots.txt.

    On any fetch error we default to "allow" (the RFC-recommended behaviour
    when robots.txt is unreachable). Cached per scheme+host so we don't
    re-fetch for every page on a site.
    """
    parsed = urlparse(url)
    if not parsed.netloc:
        return True
    cache_key = f"{parsed.scheme}://{parsed.netloc}"

    async with _robots_lock:
        if cache_key in _robots_cache:
            parser = _robots_cache[cache_key]
        else:
            parser = None
            text = await _fetch_robots_text(f"{cache_key}/robots.txt", user_agent)
            if text is not None:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(text.splitlines())
            _robots_cache[cache_key] = parser

    if parser is None:
        return True
    return parser.can_fetch(user_agent, url)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _strip_html(html: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()


def _extract(html: str) -> tuple[str, str]:
    """Return ``(title, cleaned_text)``.

    Trafilatura first; if it returns suspiciously little (<200 chars) we
    fall back to readability-lxml's ``Document.summary()`` and strip tags.
    Pure function — no I/O — so unit tests can hit it directly.
    """
    if not html:
        return "", ""

    title = ""
    text = ""

    # Trafilatura's metadata extractor is best for the title; the extracted
    # body itself rarely contains the page <title>.
    try:
        meta = trafilatura.extract_metadata(html)
        if meta and getattr(meta, "title", None):
            title = (meta.title or "").strip()
    except Exception:  # noqa: BLE001 — metadata parsing must never crash
        title = ""

    try:
        extracted = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        )
    except Exception:  # noqa: BLE001
        extracted = None
    if extracted:
        text = extracted.strip()

    if len(text) < _MIN_TRAFILATURA_CHARS:
        # readability tends to win on academic pages with heavy boilerplate
        # that trafilatura over-prunes.
        try:
            doc = Document(html)
            summary_html = doc.summary() or ""
            readable_text = _strip_html(summary_html)
            if len(readable_text) > len(text):
                text = readable_text
            if not title:
                title = (doc.short_title() or doc.title() or "").strip()
        except Exception:  # noqa: BLE001
            pass

    return title, text


def _should_use_browser(text_len: int, status_code: int | None, requires_js: bool) -> bool:
    if requires_js:
        return True
    if status_code is not None and status_code in _BROWSER_FALLBACK_STATUSES:
        return True
    return text_len < _MIN_TEXT_CHARS


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


async def _fetch_via_httpx(
    url: str,
    timeout: float,
    user_agent: str,
) -> tuple[int | None, str | None, bytes | None, str | None]:
    """Return ``(status_code, html, content_bytes, content_type)``.

    ``status_code`` is None on transport error. ``content_bytes`` carries the
    raw response body for callers (PDF detection) that need bytes rather
    than the decoded text.
    """
    headers = {"User-Agent": user_agent}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:
            response = await client.get(url)
    except (httpx.HTTPError, OSError) as exc:
        logger.debug("httpx fetch failed for %s: %s", url, exc)
        return None, None, None, None

    content_type = response.headers.get("content-type")
    if response.status_code >= 400:
        return response.status_code, None, None, content_type
    return response.status_code, response.text, response.content, content_type


async def _fetch_via_playwright(url: str, timeout: float) -> str | None:
    """Render ``url`` through the shared Chromium session, return HTML."""
    try:
        async with browser.browser_session() as ctx:
            page = await ctx.new_page()
            try:
                await browser.navigate(page, url, timeout_ms=int(timeout * 1000))
                return await page.content()
            finally:
                await page.close()
    except Exception as exc:  # noqa: BLE001 — never crash the pipeline
        logger.warning("playwright fetch failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Background Wayback save
# ---------------------------------------------------------------------------


def _spawn_archive_task(source: Source) -> asyncio.Task[None] | None:
    """Kick off Wayback save (with archive.today fallback) and write the
    result back onto ``source``.

    Wayback Save Page Now is the primary; on None (404, robots-block, repeated
    timeout) we try archive.today before giving up. Both stay inside the same
    fire-and-forget background task so ``fetch`` still returns immediately.
    Returns the spawned task so tests can opt into awaiting it.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None

    async def _runner() -> None:
        try:
            archive_url = await archive.save(source.url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("wayback save raised for %s: %s", source.url, exc)
            archive_url = None

        if archive_url is None:
            try:
                archive_url = await archive.archive_today_save(source.url)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "archive.today save raised for %s: %s", source.url, exc
                )
                archive_url = None

        if archive_url:
            source.archive_url = archive_url
            return

        logger.warning(
            "archive_failed url=%s wayback=failed archive_today=failed",
            source.url,
        )

    return loop.create_task(_runner())


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


_REDDIT_HOSTS = frozenset(
    {"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com"}
)

_YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
)

# Connector host-dispatch (issue #174). Each set covers the public netlocs
# its module's ``fetch(url)`` recognises — see the connector's own host-gate
# for the authoritative list. The planner emits ``site:<domain>`` queries to
# steer hits onto these hosts; without dispatch, the generic httpx +
# trafilatura path eats the page and the connector never runs.
#
# Search-only connectors (no URL-fetch contract — they expose only API search):
#   - arxiv_tool: ArXiv abstract fetch lives behind arxiv_fetch tasks, not URL
#   - news: RSS aggregator, not a per-URL fetcher
#   - web_search: itself
# These do not appear here; the planner emits search tasks for them.

_CONGRESS_HOSTS = frozenset({"www.congress.gov", "congress.gov"})
# Bill-text content URLs (e.g. ``/117/bills/hr5376/BILLS-117hr5376enr.htm``)
# are raw HTML/XML/PDF bodies that ``congress.fetch`` does not handle — its
# URL classifier only matches the canonical bill / member / hearing pages.
# Without this carve-out, the bill-text fan-out (issue #193) routes these
# URLs to ``congress.fetch``, which returns ``None``, and the loop FatalErrors
# instead of reading the legislative text. The PDF variant is already caught
# by ``_is_pdf_url`` ahead of host-dispatch; this pattern handles the
# ``Formatted Text`` / ``Formatted XML`` HTML variants that are the preferred
# format per ``_bill_text_pick``.
_CONGRESS_BILL_TEXT_PATH_RE = re.compile(
    r"^/\d+/bills?/[^/]+/BILLS-", re.IGNORECASE
)
_FEC_HOSTS = frozenset({"www.fec.gov", "fec.gov"})
_EDGAR_HOSTS = frozenset({"www.sec.gov", "sec.gov"})
_FEDREGISTER_HOSTS = frozenset(
    {"www.federalregister.gov", "federalregister.gov"}
)
_COURTLISTENER_HOSTS = frozenset({"courtlistener.com", "www.courtlistener.com"})
_LDA_HOSTS = frozenset({"lda.senate.gov", "lda.gov", "www.lda.gov"})
_USASPENDING_HOSTS = frozenset(
    {"api.usaspending.gov", "usaspending.gov", "www.usaspending.gov"}
)
_LITTLESIS_HOSTS = frozenset({"littlesis.org", "www.littlesis.org"})
_NONPROFITS_HOSTS = frozenset({"projects.propublica.org"})
_SANCTIONS_HOSTS = frozenset(
    {
        "sanctionssearch.ofac.treas.gov",
        "home.treasury.gov",
        "www.treasury.gov",
        "webgate.ec.europa.eu",
        "ofsistorage.blob.core.windows.net",
        "www.gov.uk",
    }
)
_CALACCESS_HOSTS = frozenset({"powersearch.sos.ca.gov"})
# licensing module: CA (CSLB) wired; TX/FL/NY ship as stubs that return None.
# Including the stubs in dispatch costs nothing (the module already host-gates
# them) and makes future state wires routing-only.
_LICENSING_HOSTS = frozenset(
    {
        "www.cslb.ca.gov",
        "cslb.ca.gov",
        "www.tdlr.texas.gov",
        "www.myfloridalicense.com",
        "www.dos.ny.gov",
    }
)
# sos module: CA wired; DE/NV/WY/FL/NY stubs return None. Same dispatch
# rationale as _LICENSING_HOSTS.
_SOS_HOSTS = frozenset(
    {
        "bizfileonline.sos.ca.gov",
        "icis.corp.delaware.gov",
        "esos.nv.gov",
        "wyobiz.wyo.gov",
        "search.sunbiz.org",
        "apps.dos.ny.gov",
    }
)
_BBB_HOSTS = frozenset({"www.bbb.org", "bbb.org"})
_OPENCORPORATES_HOSTS = frozenset(
    {"opencorporates.com", "www.opencorporates.com"}
)
# ``archive.org`` is a multi-tenant host (details/, download/, web/, …).
# Only the ``/details/<identifier>`` path is owned by the iarchive
# connector — leave web.archive.org Wayback URLs and bare downloads on the
# generic httpx + trafilatura path.
_IARCHIVE_HOSTS = frozenset({"archive.org", "www.archive.org"})

_PDF_CONTENT_TYPE = "application/pdf"

_IMAGE_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif"}
)
_IMAGE_EXTENSIONS: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _is_pdf_url(url: str) -> bool:
    """Cheap path-based PDF detection — true when the URL ends in ``.pdf``.

    Strips query/fragment so EDGAR-style ``...10k.pdf?token=...`` still
    matches. Case-insensitive because some sites publish ``.PDF``.
    """
    parsed = urlparse(url)
    return parsed.path.lower().endswith(".pdf")


def _is_pdf_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower() == _PDF_CONTENT_TYPE


def _is_audio_url(url: str) -> bool:
    """Path-based audio detection. Mirrors :func:`_is_pdf_url`."""
    from research_agent.tools import audio

    parsed = urlparse(url)
    path = parsed.path.lower()
    return any(path.endswith(ext) for ext in audio.SUPPORTED_AUDIO_EXTENSIONS)


def _is_audio_content_type(content_type: str | None) -> bool:
    from research_agent.tools import audio

    if not content_type:
        return False
    head = content_type.split(";", 1)[0].strip().lower()
    return head in audio.AUDIO_CONTENT_TYPES


def _is_image_url(url: str) -> bool:
    """Path-based image detection. Mirrors :func:`_is_pdf_url`."""
    parsed = urlparse(url)
    path = parsed.path.lower()
    return any(path.endswith(ext) for ext in _IMAGE_EXTENSIONS)


def _is_image_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    head = content_type.split(";", 1)[0].strip().lower()
    return head in _IMAGE_CONTENT_TYPES


async def _build_pdf_source(
    url: str,
    *,
    status_code: int | None,
    content: bytes | None,
) -> Source | None:
    """Run :func:`pdf.extract` (or ``extract_from_bytes``) and wrap the result.

    Returns None when extraction yielded no usable text — same contract the
    HTML path has when both fetch layers come back empty.
    """
    from research_agent.tools import pdf

    if content:
        text = pdf.extract_from_bytes(content, source_label=url)
    else:
        text = await pdf.extract(url)

    if not text.strip():
        return None

    title = url.rsplit("/", 1)[-1] or url
    metadata: dict[str, Any] = {
        "fetched_via": "pdf",
        "status_code": status_code,
    }
    return Source(
        url=url,
        title=title,
        cleaned_text=text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="pdf",
        metadata=metadata,
    )


async def _build_image_source(
    url: str,
    *,
    status_code: int | None,
    content: bytes | None,
) -> Source | None:
    """Run :func:`ocr.extract` (or ``extract_from_bytes``) and wrap the result.

    Mirrors :func:`_build_pdf_source`: returns None when OCR yielded no
    usable text (typically: tesseract isn't installed and no VLM is
    available) so the web_fetch contract stays uniform across kinds.
    """
    from research_agent.tools import ocr

    if content:
        suffix = ocr._suffix_for(url)
        text = ocr.extract_from_bytes(content, suffix=suffix, source_label=url)
    else:
        text = await ocr.extract(url)

    if not text.strip():
        return None

    title = url.rsplit("/", 1)[-1] or url
    metadata: dict[str, Any] = {
        "fetched_via": "ocr",
        "status_code": status_code,
    }
    return Source(
        url=url,
        title=title,
        cleaned_text=text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="image",
        metadata=metadata,
    )


async def _build_audio_source(
    url: str,
    *,
    status_code: int | None,
    content: bytes | None,
) -> Source | None:
    """Run :func:`audio.transcribe` (or ``transcribe_from_bytes``) and wrap result.

    Mirrors :func:`_build_pdf_source` — returns None when transcription
    yielded no usable text (typically: no whisper backend installed) so the
    web_fetch contract stays uniform.
    """
    from research_agent.tools import audio

    if content:
        suffix = audio._suffix_for(url)
        text = audio.transcribe_from_bytes(content, suffix=suffix)
    else:
        text = await audio.transcribe(url)

    if not text.strip():
        return None

    title = url.rsplit("/", 1)[-1] or url
    metadata: dict[str, Any] = {
        "fetched_via": "audio",
        "status_code": status_code,
    }
    return Source(
        url=url,
        title=title,
        cleaned_text=text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="audio",
        metadata=metadata,
    )


async def fetch(
    url: str,
    requires_js: bool = False,
    timeout: float = 30.0,
) -> Source | None:
    """Fetch ``url``, extract its content, and return a :class:`Source`.

    Returns None when robots.txt forbids the fetch, both fetch paths fail to
    yield enough text, or the URL is malformed. Wayback archival happens in
    a background task; the returned ``Source.archive_url`` is None unless the
    save completes before the consumer reads it.

    Host-based dispatch: ``reddit.com`` URLs route to
    :func:`research_agent.tools.reddit.fetch`, which uses Reddit's JSON
    endpoint and returns post-body + top-level comments. The generic
    Playwright + trafilatura path strips reddit pages to empty content
    (their SPA shell defeats readability extractors), so without this
    dispatch every reddit follow-up would task_failed.

    The connector roster (issue #174) extends the same pattern to the
    free-tier authoritative-source modules (Congress, FEC, EDGAR,
    Federal Register, CourtListener, LDA, USAspending, LittleSis,
    ProPublica nonprofits, OFAC sanctions, Cal-Access, CSLB, CA SoS,
    BBB, OpenCorporates) so a planner-emitted ``site:<domain>`` query
    that yields one of those URLs is handled by the connector that
    knows the page shape rather than being eaten by the generic HTML
    extractor.
    """
    if not url or not urlparse(url).netloc:
        return None

    # Binary URL shortcuts run BEFORE connector dispatch: a `.pdf` on
    # ``sec.gov`` (EDGAR exhibits, 10-K appendices, …) must go through the
    # PDF extractor, not edgar.fetch which expects an HTML index page.
    # Same logic for `.mp3` / image suffixes on connector-owned hosts.
    if _is_pdf_url(url):
        source = await _build_pdf_source(url, status_code=None, content=None)
        if source is not None:
            _spawn_archive_task(source)
        return source

    if _is_audio_url(url):
        source = await _build_audio_source(url, status_code=None, content=None)
        if source is not None:
            _spawn_archive_task(source)
        return source

    if _is_image_url(url):
        source = await _build_image_source(url, status_code=None, content=None)
        if source is not None:
            _spawn_archive_task(source)
        return source

    netloc = urlparse(url).netloc.lower().split(":", 1)[0]
    if netloc in _REDDIT_HOSTS:
        from research_agent.tools import reddit

        return await reddit.fetch(url)

    if netloc in _YOUTUBE_HOSTS:
        from research_agent.tools import youtube

        return await youtube.fetch(url)

    if netloc in _CONGRESS_HOSTS:
        # Bill-text content URLs slip past congress.fetch (which only handles
        # canonical /bill/, /member/, /hearing/ permalinks) and fall through
        # to the generic httpx + trafilatura extractor below. Issue #193:
        # without this carve-out, the bill-text fan-out FatalErrors on the
        # preferred ``Formatted Text`` (HTML) format.
        if not _CONGRESS_BILL_TEXT_PATH_RE.match(urlparse(url).path):
            from research_agent.tools import congress

            return await congress.fetch(url)

    if netloc in _FEC_HOSTS:
        from research_agent.tools import fec

        return await fec.fetch(url)

    if netloc in _EDGAR_HOSTS:
        from research_agent.tools import edgar

        return await edgar.fetch(url)

    if netloc in _FEDREGISTER_HOSTS:
        from research_agent.tools import fedregister

        return await fedregister.fetch(url)

    if netloc in _COURTLISTENER_HOSTS:
        from research_agent.tools import courtlistener

        return await courtlistener.fetch(url)

    if netloc in _LDA_HOSTS:
        from research_agent.tools import lda

        return await lda.fetch(url)

    if netloc in _USASPENDING_HOSTS:
        from research_agent.tools import usaspending

        return await usaspending.fetch(url)

    if netloc in _LITTLESIS_HOSTS:
        from research_agent.tools import littlesis

        return await littlesis.fetch(url)

    # ``projects.propublica.org`` is a multi-tenant subdomain (Nonprofit
    # Explorer, Electionland, Dollars for Docs, …). Only the
    # ``/nonprofits/`` path is owned by this connector — leave other
    # ProPublica pages on the generic httpx + trafilatura path.
    if netloc in _NONPROFITS_HOSTS and urlparse(url).path.startswith("/nonprofits/"):
        from research_agent.tools import nonprofits

        return await nonprofits.fetch(url)

    if netloc in _SANCTIONS_HOSTS:
        from research_agent.tools import sanctions

        return await sanctions.fetch(url)

    if netloc in _CALACCESS_HOSTS:
        from research_agent.tools import calaccess

        return await calaccess.fetch(url)

    if netloc in _LICENSING_HOSTS:
        from research_agent.tools import licensing

        return await licensing.fetch(url)

    if netloc in _SOS_HOSTS:
        from research_agent.tools import sos

        return await sos.fetch(url)

    if netloc in _BBB_HOSTS:
        from research_agent.tools import bbb

        return await bbb.fetch(url)

    if netloc in _OPENCORPORATES_HOSTS:
        from research_agent.tools import opencorporates

        return await opencorporates.fetch(url)

    if netloc in _IARCHIVE_HOSTS and urlparse(url).path.startswith("/details/"):
        from research_agent.tools import iarchive

        return await iarchive.fetch(url)

    user_agent = _resolve_user_agent()

    if not _ignore_robots():
        if not await _robots_allows(url, user_agent):
            logger.info("web_fetch skipped %s — disallowed by robots.txt", url)
            return None

    html: str | None = None
    status_code: int | None = None
    content_bytes: bytes | None = None
    content_type: str | None = None
    fetched_via: str = "httpx"

    if not requires_js:
        status_code, html, content_bytes, content_type = await _fetch_via_httpx(
            url, timeout, user_agent
        )

    # Server-declared PDF (e.g. ``Content-Disposition: attachment; ...10k.pdf``
    # behind a redirect that hides the suffix). We already have the bytes —
    # feed them straight into pdf.extract_from_bytes.
    if _is_pdf_content_type(content_type) and content_bytes:
        source = await _build_pdf_source(
            url, status_code=status_code, content=content_bytes
        )
        if source is not None:
            _spawn_archive_task(source)
        return source

    # Same idea for server-declared audio (some podcast CDNs don't publish a
    # ``.mp3`` suffix). Reuse the bytes we already pulled.
    if _is_audio_content_type(content_type) and content_bytes:
        source = await _build_audio_source(
            url, status_code=status_code, content=content_bytes
        )
        if source is not None:
            _spawn_archive_task(source)
        return source

    # Same idea for server-declared images (URL has no suffix but the
    # response is ``image/png`` etc). Reuse the bytes we already pulled.
    if _is_image_content_type(content_type) and content_bytes:
        source = await _build_image_source(
            url, status_code=status_code, content=content_bytes
        )
        if source is not None:
            _spawn_archive_task(source)
        return source

    title, text = _extract(html or "")

    if _should_use_browser(len(text), status_code, requires_js):
        rendered = await _fetch_via_playwright(url, timeout)
        if rendered:
            html = rendered
            title, text = _extract(rendered)
            fetched_via = "playwright"
        elif requires_js or html is None:
            # We needed JS or had no httpx html and Playwright also failed —
            # nothing to return.
            return None

    if len(text) < _MIN_TEXT_CHARS and not html:
        return None

    metadata: dict[str, Any] = {
        "fetched_via": fetched_via,
        "status_code": status_code,
    }

    source = Source(
        url=url,
        title=title or url,
        cleaned_text=text,
        raw_html=html,
        fetched_at=datetime.now(UTC),
        source_kind="web",
        metadata=metadata,
    )

    _spawn_archive_task(source)

    return source


def reset_for_tests() -> None:
    """Clear the per-host robots cache. Test-only."""
    _robots_cache.clear()


__all__ = ["fetch", "reset_for_tests"]
