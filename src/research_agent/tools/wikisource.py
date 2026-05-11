"""Wikisource connector (issue #234, A12).

Public surface:

* ``async def search(query, *, lang="en", max_results=20)`` hits the
  per-language Wikisource MediaWiki Action API with
  ``action=query&list=search``.
* ``async def fetch(url)`` resolves ``<lang>.wikisource.org/wiki/...`` URLs
  and returns the full transcribed page body in ``Source.cleaned_text``.

No auth required. Wikimedia traffic goes through the shared MediaWiki helper's
project-identifying User-Agent and 1 RPS host-family limiter.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from research_agent.tools import _mediawiki
from research_agent.tools._registry import (
    BaseSearchPayload as _BaseSearchPayload,
)
from research_agent.tools._registry import (
    register_kind as _register_kind,
)
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

KIND = "wikisource_search"

SUPPORTED_LANGS: frozenset[str] = frozenset(
    {"en", "fr", "es", "de", "it", "pt", "nl", "ru", "zh", "ja", "ar"}
)

_HOST_RE = re.compile(r"^(?P<lang>[a-z]{2,3})\.wikisource\.org$", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t\r\f\v]+")


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


def _normalize_lang(lang: str) -> str:
    normalized = (lang or "").strip().lower()
    if normalized not in SUPPORTED_LANGS:
        logger.warning("wikisource unsupported lang=%r", lang)
        return ""
    return normalized


def _api_url(lang: str) -> str:
    return f"https://{lang}.wikisource.org/w/api.php"


def _page_url(lang: str, title: str) -> str:
    encoded = quote(title.strip().replace(" ", "_"), safe="/:")
    return f"https://{lang}.wikisource.org/wiki/{encoded}"


def _title_from_url(url: str) -> tuple[str, str] | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None

    host = (parsed.hostname or "").lower()
    match = _HOST_RE.match(host)
    if match is None:
        return None
    lang = _normalize_lang(match.group("lang"))
    if not lang:
        return None

    title = ""
    if parsed.path.startswith("/wiki/"):
        title = parsed.path[len("/wiki/") :]
    elif parsed.path == "/w/index.php":
        values = parse_qs(parsed.query).get("title") or []
        title = values[0] if values else ""
    if not title:
        return None

    normalized_title = unquote(title).replace("_", " ").strip()
    if not normalized_title:
        return None
    return lang, normalized_title


def _parse_text_from_payload(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        raw = value.get("*")
        return raw if isinstance(raw, str) else ""
    return ""


def _clean_body_html(raw_html: str) -> str:
    """Extract page-body text from MediaWiki parse HTML.

    Wikisource is valuable because the transcribed primary-document body is
    searchable. Prefer BeautifulSoup so headings and paragraph boundaries
    survive; fall back to the lightweight shared tag-stripper if the optional
    parser is unavailable.
    """
    if not raw_html:
        return ""

    try:
        from bs4 import BeautifulSoup  # type: ignore[import-untyped]

        soup = BeautifulSoup(raw_html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        for tag in soup.select(".mw-editsection, .noprint, .metadata"):
            tag.decompose()
        root = soup.select_one(".mw-parser-output") or soup
        text = root.get_text(separator="\n")
    except Exception as exc:  # noqa: BLE001
        logger.debug("wikisource bs4 parse failed: %s", exc)
        text = _mediawiki.clean_text(raw_html)

    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _search_result_from_hit(hit: dict[str, Any], *, lang: str) -> SearchResult | None:
    title = str(hit.get("title") or "").strip()
    if not title:
        return None
    snippet = (
        _mediawiki.clean_text(hit.get("snippet"))
        or _mediawiki.clean_text(hit.get("titlesnippet"))
        or title
    )
    page_id = hit.get("pageid")
    return SearchResult(
        url=_page_url(lang, title),
        title=title,
        snippet=snippet,
        published_at=_parse_timestamp(hit.get("timestamp")),
        source_kind=KIND,
        extras={
            "wikisource_lang": lang,
            "page_title": title,
            "page_id": page_id,
            "word_count": hit.get("wordcount"),
            "size": hit.get("size"),
        },
    )


async def search(
    query: str,
    *,
    lang: str = "en",
    max_results: int = 20,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Search one Wikisource language host for transcribed source documents."""
    q = query.strip()
    lang_code = _normalize_lang(lang)
    if not q or max_results <= 0 or not lang_code:
        return []

    payload = await _mediawiki.request_json(
        _api_url(lang_code),
        params={
            "action": "query",
            "list": "search",
            "srsearch": q,
            "srlimit": min(max_results, 50),
            "srprop": "snippet|timestamp|titlesnippet|size|wordcount",
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
        logger.warning("wikisource search response missing query.search")
        return []

    results: list[SearchResult] = []
    seen: set[str] = set()
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        result = _search_result_from_hit(hit, lang=lang_code)
        if result is None or result.title in seen:
            continue
        seen.add(result.title)
        results.append(result)
        if len(results) >= max_results:
            break
    return results


async def fetch(url: str, *, timeout: float = 15.0) -> Source | None:
    """Fetch a Wikisource page and return its full transcribed body."""
    parsed = _title_from_url(url)
    if parsed is None:
        return None
    lang, title = parsed

    payload = await _mediawiki.request_json(
        _api_url(lang),
        params={
            "action": "parse",
            "page": title,
            "prop": "text|revid|displaytitle",
            "redirects": "1",
            "format": "json",
            "formatversion": "2",
        },
        timeout=timeout,
    )
    if payload is None:
        return None

    parse_root = payload.get("parse")
    if not isinstance(parse_root, dict):
        logger.warning("wikisource parse response missing parse root for %s", url)
        return None

    resolved_title = str(parse_root.get("title") or title).strip()
    raw_body = _parse_text_from_payload(parse_root.get("text"))
    body = _clean_body_html(raw_body)
    if not body:
        return None

    revision_id = parse_root.get("revid")
    page_id = parse_root.get("pageid")
    metadata: dict[str, Any] = {
        "wikisource_lang": lang,
        "page_title": resolved_title,
        "revision_id": revision_id,
        "page_id": page_id,
    }

    return Source(
        url=_page_url(lang, resolved_title),
        title=resolved_title,
        cleaned_text=f"# {resolved_title}\n\n{body}".strip(),
        raw_html=raw_body,
        fetched_at=datetime.now(UTC),
        source_kind=KIND,
        metadata=metadata,
    )


def reset_for_tests() -> None:
    """Clear shared MediaWiki limiter state. Test-only."""
    _mediawiki.reset_for_tests()


class _PayloadSchema(_BaseSearchPayload):
    lang: str | None = None
    max_results: int | None = None
    timeout: float | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=tuple(f"{lang}.wikisource.org" for lang in sorted(SUPPORTED_LANGS)),
    skill_name="wikisource",
    description=(
        "Wikisource transcribed primary documents across per-language hosts;"
        " fetch returns the full source text in cleaned_text"
    ),
    optional_payload_knobs="`lang: en|fr|es|de|it|pt|nl|ru|zh|ja|ar`, `max_results`",
    example_query="Treaty of Versailles",
    module_name="wikisource",
)


__all__ = ["KIND", "SUPPORTED_LANGS", "fetch", "reset_for_tests", "search"]
