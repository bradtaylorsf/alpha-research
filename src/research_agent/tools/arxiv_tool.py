"""arXiv connector (issue #19).

Wraps the synchronous ``arxiv`` Python lib in an asyncio-friendly surface and
adds the same per-process rate limiting and PDF-text extraction the rest of
the pipeline relies on.

Public surface:

* ``async def search(query, max_results=20, sort_by='relevance')`` — return a
  list of :class:`SearchResult`. Snippet is the arXiv abstract.
* ``async def fetch(arxiv_id_or_url)`` — download the PDF to
  ``corpus/.cache/arxiv/<id>.pdf`` (cached) and return a :class:`Source` with
  ``source_kind='arxiv'`` whose ``cleaned_text`` is the extracted PDF text.

arXiv's API guidelines ask callers to leave at least three seconds between
requests; both ``search`` and the PDF download go through ``_rate_limit_gate``
to honour that.

The ``arxiv`` lib is sync, so its ``Client.results`` invocation runs inside
``asyncio.to_thread``. PDF text extraction reuses
``local_corpus._extract_pdf`` so the pypdf → unstructured fallback that the
local indexer uses is shared with arXiv-fetched PDFs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import arxiv
import httpx

from research_agent import config
from research_agent.tools import local_corpus
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_DEFAULT_USER_AGENT = "research-agent/0.1"
_RATE_LIMIT_INTERVAL = 3.0
_CACHE_DIR = Path("corpus/.cache/arxiv")

# Match either a bare arXiv id (with optional version) or an arxiv.org abs/pdf URL.
# Modern ids look like ``2401.12345`` or ``2401.12345v2``; legacy ids look like
# ``quant-ph/0201082`` or ``quant-ph/0201082v1``.
_ARXIV_ID_RE = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf)/)?"
    r"(?P<id>(?:[a-z\-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5}))"
    r"(?P<version>v\d+)?"
    r"(?:\.pdf)?",
    re.IGNORECASE,
)

_SORT_BY_MAP: dict[str, arxiv.SortCriterion] = {
    "relevance": arxiv.SortCriterion.Relevance,
    "submittedDate": arxiv.SortCriterion.SubmittedDate,
    "lastUpdatedDate": arxiv.SortCriterion.LastUpdatedDate,
}

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


def _resolve_user_agent() -> str:
    return config.get("RESEARCH_USER_AGENT") or _DEFAULT_USER_AGENT


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


def _normalize_id(arxiv_id_or_url: str) -> str:
    """Extract the bare arXiv id (without version suffix) from any accepted form."""
    if not arxiv_id_or_url:
        raise ValueError("arxiv_id_or_url must be non-empty")
    match = _ARXIV_ID_RE.search(arxiv_id_or_url.strip())
    if not match:
        raise ValueError(f"could not parse arXiv id from {arxiv_id_or_url!r}")
    return match.group("id")


def _build_search_result(result: arxiv.Result) -> SearchResult:
    short_id = result.get_short_id()
    return SearchResult(
        url=result.entry_id,
        title=(result.title or "").strip(),
        snippet=(result.summary or "").strip(),
        published_at=result.published,
        source_kind="arxiv",
        extras={
            "arxiv_id": short_id,
            "authors": [a.name for a in result.authors],
            "pdf_url": result.pdf_url,
            "categories": list(result.categories or []),
            "primary_category": result.primary_category,
        },
    )


async def search(
    query: str,
    max_results: int = 20,
    sort_by: str = "relevance",
) -> list[SearchResult]:
    """Search arXiv and return up to ``max_results`` :class:`SearchResult` hits.

    ``sort_by`` accepts ``'relevance'`` or ``'submittedDate'`` (and the less
    common ``'lastUpdatedDate'``). The ``arxiv`` lib is synchronous, so the
    actual HTTP call runs in a worker thread; the rate-limit gate ensures we
    leave ≥3 s between successive arXiv API calls per process.
    """
    if sort_by not in _SORT_BY_MAP:
        raise ValueError(f"sort_by must be one of {sorted(_SORT_BY_MAP)}; got {sort_by!r}")

    sort_criterion = _SORT_BY_MAP[sort_by]
    arxiv_search = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=sort_criterion,
    )

    await _rate_limit_gate()

    def _run() -> list[arxiv.Result]:
        client = arxiv.Client()
        return list(client.results(arxiv_search))

    try:
        results = await asyncio.to_thread(_run)
    except (arxiv.ArxivError, arxiv.HTTPError, arxiv.UnexpectedEmptyPageError) as exc:
        logger.warning("arxiv search failed for %r: %s", query, exc)
        return []

    return [_build_search_result(r) for r in results]


async def _download_pdf(pdf_url: str, dest: Path, timeout: float) -> bool:
    """Download ``pdf_url`` to ``dest`` atomically. Returns True on success."""
    headers = {"User-Agent": _resolve_user_agent()}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:
            response = await client.get(pdf_url)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("arxiv pdf download failed for %s: %s", pdf_url, exc)
        return False

    if response.status_code >= 400:
        logger.warning(
            "arxiv pdf download returned HTTP %s for %s",
            response.status_code,
            pdf_url,
        )
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(response.content)
    tmp.replace(dest)
    return True


async def _resolve_title(arxiv_id: str) -> str | None:
    """Fetch a single result's metadata to recover its title. None on failure."""
    await _rate_limit_gate()

    def _run() -> arxiv.Result | None:
        client = arxiv.Client()
        try:
            return next(client.results(arxiv.Search(id_list=[arxiv_id])))
        except StopIteration:
            return None

    try:
        result = await asyncio.to_thread(_run)
    except (arxiv.ArxivError, arxiv.HTTPError, arxiv.UnexpectedEmptyPageError) as exc:
        logger.debug("arxiv title lookup failed for %s: %s", arxiv_id, exc)
        return None

    if result is None:
        return None
    return (result.title or "").strip() or None


async def fetch(arxiv_id_or_url: str, timeout: float = 30.0) -> Source | None:
    """Download the arXiv PDF for ``arxiv_id_or_url`` and return a :class:`Source`.

    PDF bytes are cached at ``corpus/.cache/arxiv/<id>.pdf`` so repeated calls
    skip the network entirely. Text extraction reuses
    ``local_corpus._extract_pdf``. Returns None if the download fails.
    """
    try:
        arxiv_id = _normalize_id(arxiv_id_or_url)
    except ValueError as exc:
        logger.warning("arxiv fetch rejected %r: %s", arxiv_id_or_url, exc)
        return None

    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    cache_path = _CACHE_DIR / f"{arxiv_id.replace('/', '_')}.pdf"

    if not cache_path.exists():
        await _rate_limit_gate()
        ok = await _download_pdf(pdf_url, cache_path, timeout)
        if not ok:
            return None

    cleaned_text = await asyncio.to_thread(local_corpus._extract_pdf, cache_path)

    title = await _resolve_title(arxiv_id) or arxiv_id

    return Source(
        url=f"https://arxiv.org/abs/{arxiv_id}",
        title=title,
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="arxiv",
        metadata={
            "arxiv_id": arxiv_id,
            "pdf_path": str(cache_path),
            "pdf_url": pdf_url,
        },
    )


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


__all__ = ["fetch", "reset_for_tests", "search"]
