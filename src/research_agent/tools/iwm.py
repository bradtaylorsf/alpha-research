"""Imperial War Museums public collections connector (issue #237).

Public surface:

* ``async def search(query, *, max_results=20, **knobs) -> list[SearchResult]``
  opens ``https://www.iwm.org.uk/collections/search?query=<query>`` through
  the shared Playwright session and parses public collection hits.
* ``async def fetch(url) -> Source | None`` opens an IWM collection item page
  and returns visible page text plus object metadata.

IWM Collections Search has no public API, so this is a read-only browser
connector. Robots check: ``https://www.iwm.org.uk/robots.txt`` was reviewed
for this issue; it disallows the top-level ``/search/`` path only, while
``/collections/search`` is not disallowed. The connector therefore uses only
public collection search and item pages, with no login, protected content, or
form side effects.

Public filter URLs expose object category and related-period facets as
``filters[webCategory][<category>]=on`` and
``filters[periodString][<period>]=on``. The connector exposes those as
``object_category`` and ``related_period`` knobs. Browser traffic is capped at
0.5 RPS per host, and selector-drift diagnostics are written to
``data/diagnostics/iwm/``.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode, urljoin, urlparse

import playwright.async_api
from lxml import html as lxml_html
from lxml.html import HtmlElement

from research_agent.tools import browser
from research_agent.tools._registry import (
    BaseSearchPayload as _BaseSearchPayload,
)
from research_agent.tools._registry import (
    register_kind as _register_kind,
)
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

KIND: Literal["iwm_search"] = "iwm_search"

_HOST = "www.iwm.org.uk"
_ACCEPTED_HOSTS = frozenset({"www.iwm.org.uk", "iwm.org.uk"})
_SITE_BASE = f"https://{_HOST}"
_SEARCH_PATH = "/collections/search"
_ITEM_PATH = "/collections/item/object/"
_PER_HOST_RPS = 0.5
_DIAGNOSTICS_DIR = Path("data/diagnostics/iwm")

_WS_RE = re.compile(r"\s+")
_YEAR_RE = re.compile(r"\b((?:18|19|20)\d{2})\b")
_ACTION_TEXT = frozenset(
    {
        "image",
        "view item: image",
        "download",
        "purchase & license",
        "list view",
        "image view",
    }
)
_KNOWN_LABELS = frozenset(
    {
        "object title",
        "object category",
        "category",
        "related period",
        "production date",
        "creator",
        "catalogue number",
        "catalogue id",
        "reference",
        "part of",
        "collection",
        "materials",
    }
)
_CATEGORY_ALIASES = {
    "oral history": "Sound",
    "oral histories": "Sound",
    "photo": "Photographs",
    "photos": "Photographs",
    "photograph": "Photographs",
    "photographs": "Photographs",
}
_PERIOD_ALIASES = {
    "ww1": "First World War",
    "world war i": "First World War",
    "first world war": "First World War",
    "ww2": "Second World War",
    "world war ii": "Second World War",
    "second world war": "Second World War",
    "postwar": "1945-1989",
    "post-war": "1945-1989",
    "cold war": "1945-1989",
}


def _register_host_rates() -> None:
    for host in _ACCEPTED_HOSTS:
        browser.set_host_rate(host, _PER_HOST_RPS)


_register_host_rates()


def _clean_text(value: str | None) -> str:
    return _WS_RE.sub(" ", value or "").strip()


def _fold(value: str | None) -> str:
    return _clean_text(value).casefold()


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _normalize_filter_value(value: str | None, aliases: dict[str, str]) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    return aliases.get(text.casefold(), text)


def build_search_url(
    query: str,
    *,
    object_category: str | None = None,
    related_period: str | None = None,
    records_with_media: bool | None = None,
    style: str | None = None,
    page_size: int | None = None,
) -> str:
    """Return the public IWM Collections Search URL for ``query``."""
    params: list[tuple[str, str]] = [("query", query.strip())]
    if page_size is not None and page_size > 0:
        params.append(("pageSize", str(page_size)))
    if style:
        params.append(("style", _clean_text(style)))
    if records_with_media:
        params.append(("media-records", "records-with-media"))

    category = _normalize_filter_value(object_category, _CATEGORY_ALIASES)
    if category:
        params.append((f"filters[webCategory][{category}]", "on"))

    period = _normalize_filter_value(related_period, _PERIOD_ALIASES)
    if period:
        params.append((f"filters[periodString][{period}]", "on"))

    return f"{_SITE_BASE}{_SEARCH_PATH}?{urlencode(params)}"


def _parse_html(raw_html: str) -> HtmlElement:
    return lxml_html.fromstring(raw_html or "<html></html>")


def _node_text(node: HtmlElement) -> str:
    return _clean_text(" ".join(node.itertext()))


def _text_lines(node: HtmlElement) -> list[str]:
    return [
        _clean_text(str(value))
        for value in node.xpath(".//text()")
        if _clean_text(str(value))
    ]


def _absolute_url(base: str, href: str | None) -> str:
    text = _clean_text(href)
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    absolute = urljoin(base, text)
    parsed = urlparse(absolute)
    if (parsed.hostname or "").casefold() in _ACCEPTED_HOSTS:
        return parsed._replace(scheme="https", fragment="").geturl()
    return absolute


def _is_iwm_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and (parsed.hostname or "").casefold() in _ACCEPTED_HOSTS
    )


def _is_iwm_item_url(url: str) -> bool:
    return _is_iwm_url(url) and urlparse(url).path.startswith(_ITEM_PATH)


def _first_meta(root: HtmlElement, *names: str) -> str:
    wanted = {_fold(name) for name in names}
    for meta in root.xpath("//meta[@name or @property]"):
        key = _fold(meta.get("name") or meta.get("property"))
        if key in wanted:
            value = _clean_text(meta.get("content"))
            if value:
                return value
    return ""


def _metadata_value(node: HtmlElement, *labels: str) -> str:
    wanted = {_fold(label).rstrip(":") for label in labels}

    for label_node in node.xpath(
        ".//*[self::dt or self::th or self::strong or self::b "
        "or contains(translate(@class, 'LABEL', 'label'), 'label')]"
    ):
        label_text = _fold(_node_text(label_node)).rstrip(":")
        if label_text not in wanted:
            continue
        if label_node.tag.casefold() == "dt":
            values = label_node.xpath("./following-sibling::dd[1]")
        elif label_node.tag.casefold() == "th":
            values = label_node.xpath("./following-sibling::td[1]")
        else:
            values = label_node.xpath("./following-sibling::*[1]")
        for value_node in values:
            value = _node_text(value_node)
            if value and _fold(value).rstrip(":") not in wanted:
                return value
        parent = label_node.getparent()
        if parent is not None:
            parent_text = _node_text(parent)
            raw_label = _node_text(label_node)
            if parent_text and raw_label and parent_text != raw_label:
                value = parent_text.replace(raw_label, "", 1).strip(" :")
                if value:
                    return value

    lines = _text_lines(node)
    for index, line in enumerate(lines):
        folded = _fold(line).rstrip(":")
        if folded in wanted and index + 1 < len(lines):
            values: list[str] = []
            for value in lines[index + 1 :]:
                value_folded = _fold(value).rstrip(":")
                if value_folded in _KNOWN_LABELS:
                    break
                if value_folded in _ACTION_TEXT:
                    continue
                values.append(value)
                break
            if values:
                return " ".join(values)
        for label in wanted:
            if folded.startswith(f"{label}:"):
                value = line.split(":", 1)[1].strip()
                if value:
                    return value
    return ""


def _candidate_cards(root: HtmlElement) -> list[HtmlElement]:
    candidates: list[HtmlElement] = []
    seen: set[int] = set()
    xpath = (
        "//article[.//a[contains(@href, '/collections/item/object/')]]"
        "|//li[.//a[contains(@href, '/collections/item/object/')]]"
        "|//div[.//a[contains(@href, '/collections/item/object/')] and "
        "(contains(translate(@class, 'RESULTCARDITEMNOTICE', 'resultcarditemnotice'), 'result') "
        "or contains(translate(@class, 'RESULTCARDITEMNOTICE', 'resultcarditemnotice'), 'card') "
        "or contains(translate(@class, 'RESULTCARDITEMNOTICE', 'resultcarditemnotice'), 'item') "
        "or contains(translate(@class, 'RESULTCARDITEMNOTICE', 'resultcarditemnotice'), 'notice'))]"
    )
    for node in root.xpath(xpath):
        ident = id(node)
        if ident in seen:
            continue
        text = _node_text(node)
        if 20 <= len(text) <= 6000:
            seen.add(ident)
            candidates.append(node)
    return candidates


def _card_for_link(link: HtmlElement) -> HtmlElement:
    node: HtmlElement = link
    best = link
    for _ in range(7):
        parent = node.getparent()
        if parent is None:
            break
        text = _node_text(parent)
        class_name = (parent.get("class") or "").casefold()
        ident = (parent.get("id") or "").casefold()
        if 20 <= len(text) <= 6000:
            best = parent
        if 20 <= len(text) <= 6000 and any(
            token in class_name or token in ident
            for token in ("result", "card", "item", "notice", "record")
        ):
            return parent
        node = parent
    return best


def _extract_title(card: HtmlElement, link_text: str) -> str:
    for value in (
        _metadata_value(card, "Object Title"),
        _clean_text(card.xpath("string((.//*[self::h1 or self::h2 or self::h3 or self::h4])[1])")),
        _clean_text(
            card.xpath(
                "string((.//*[contains(translate(@class, 'TITLE', 'title'), 'title')])[1])"
            )
        ),
        link_text,
    ):
        title = _clean_text(value)
        if title and _fold(title) not in _ACTION_TEXT and _fold(title) != "object title":
            return title
    return ""


def _catalogue_from_card(card: HtmlElement, title: str) -> str:
    direct = _metadata_value(card, "Catalogue number", "Catalogue ID", "Reference")
    if direct:
        return direct
    lines = _text_lines(card)
    title_folded = _fold(title)
    for index, line in enumerate(lines):
        if _fold(line) != title_folded:
            continue
        for value in lines[index + 1 :]:
            folded = _fold(value).rstrip(":")
            if folded in _KNOWN_LABELS or folded in _ACTION_TEXT:
                continue
            if folded == title_folded:
                continue
            return value
    return ""


def _snippet(text: str, title: str) -> str:
    snippet = _clean_text(text)
    if title and snippet.startswith(title):
        snippet = snippet[len(title) :].strip(" .,-")
    for action in _ACTION_TEXT:
        snippet = re.sub(re.escape(action), " ", snippet, flags=re.IGNORECASE)
    return _clean_text(snippet)[:600]


def _published_at_from_text(value: str) -> datetime | None:
    match = _YEAR_RE.search(value or "")
    if match is None:
        return None
    try:
        return datetime(int(match.group(1)), 1, 1, tzinfo=UTC)
    except ValueError:
        return None


def _result_from_card(
    card: HtmlElement,
    *,
    url: str,
    link_text: str,
) -> SearchResult | None:
    title = _extract_title(card, link_text)
    if not title:
        return None

    object_type = _metadata_value(card, "Object category", "Category")
    period = _metadata_value(card, "Related period")
    collection = _metadata_value(card, "Part of", "Collection")
    catalogue_id = _catalogue_from_card(card, title)
    production_date = _metadata_value(card, "Production date")
    creator = _metadata_value(card, "Creator")
    metadata: dict[str, Any] = {
        "object_type": object_type,
        "period": period,
        "collection": collection,
        "catalogue_id": catalogue_id,
        "production_date": production_date,
        "creator": creator,
    }
    card_text = _node_text(card)
    return SearchResult(
        url=url,
        title=title,
        snippet=_snippet(card_text, title),
        published_at=_published_at_from_text(production_date),
        source_kind=KIND,
        extras=metadata,
    )


def _parse_search_html(raw_html: str, *, base_url: str, max_results: int) -> list[SearchResult]:
    root = _parse_html(raw_html)
    cards = _candidate_cards(root)
    if not cards:
        for link in root.xpath("//a[contains(@href, '/collections/item/object/')]"):
            card = _card_for_link(link)
            if card not in cards:
                cards.append(card)

    results: list[SearchResult] = []
    seen: set[str] = set()
    for card in cards:
        links = card.xpath(".//a[contains(@href, '/collections/item/object/')]")
        if not links:
            continue
        link = links[0]
        url = _absolute_url(base_url, link.get("href"))
        if not _is_iwm_item_url(url) or url in seen:
            continue
        result = _result_from_card(
            card,
            url=url,
            link_text=_node_text(link),
        )
        if result is None:
            continue
        seen.add(result.url)
        results.append(result)
        if len(results) >= max_results:
            break
    return results


def _looks_like_empty_search(raw_html: str) -> bool:
    text = _fold(_parse_html(raw_html).text_content())
    return any(
        marker in text
        for marker in (
            "showing 0 records",
            "showing 0-0 of 0 records",
            "0 records",
            "no records",
            "no results",
            "no objects found",
        )
    )


async def _save_diagnostic_dump(page: Any, label: str, *, html: str | None = None) -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = _DIAGNOSTICS_DIR / f"{label}-{stamp}"
    try:
        base.parent.mkdir(parents=True, exist_ok=True)
        if html is None:
            try:
                html = await page.content()
            except Exception:  # noqa: BLE001
                html = ""
        tmp = base.with_suffix(".html.tmp")
        final = base.with_suffix(".html")
        tmp.write_text(html or "", encoding="utf-8")
        os.replace(tmp, final)
        try:
            await page.screenshot(path=str(base.with_suffix(".png")))
        except Exception as exc:  # noqa: BLE001
            logger.debug("iwm diagnostic screenshot failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("iwm diagnostic dump failed: %s", exc)


async def search(
    query: str,
    *,
    max_results: int = 20,
    object_category: str | None = None,
    related_period: str | None = None,
    records_with_media: bool | None = None,
    style: str | None = None,
    page_size: int | None = None,
    **knobs: Any,
) -> list[SearchResult]:
    """Search IWM public collection records via the shared browser session."""
    q = query.strip()
    if not q or max_results <= 0:
        return []
    object_category = (
        object_category
        or knobs.get("category")
        or knobs.get("web_category")
        or knobs.get("objectCategory")
    )
    related_period = related_period or knobs.get("period") or knobs.get("periodString")
    if records_with_media is None:
        raw_media = knobs.get("media_records") or knobs.get("recordsWithMedia")
        records_with_media = bool(raw_media) if raw_media is not None else None
    if page_size is None:
        raw_page_size = knobs.get("pageSize")
        if isinstance(raw_page_size, int):
            page_size = raw_page_size

    search_url = build_search_url(
        q,
        object_category=str(object_category) if object_category else None,
        related_period=str(related_period) if related_period else None,
        records_with_media=records_with_media,
        style=style,
        page_size=page_size,
    )

    try:
        async with browser.browser_session() as ctx:
            page = await ctx.new_page()
            try:
                await browser.navigate(page, search_url)
                raw_html = await page.content()
                try:
                    results = _parse_search_html(
                        raw_html,
                        base_url=search_url,
                        max_results=max_results,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "iwm search selector drift: parse failed on %s: %s",
                        search_url,
                        exc,
                    )
                    await _save_diagnostic_dump(page, "search-selector-drift", html=raw_html)
                    return []
                if results or _looks_like_empty_search(raw_html):
                    return results
                logger.warning(
                    "iwm search selector drift: no collection item cards parsed from %s",
                    search_url,
                )
                await _save_diagnostic_dump(page, "search-selector-drift", html=raw_html)
                return []
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
    except playwright.async_api.Error as exc:
        logger.warning("iwm search playwright error: %s", exc)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("iwm search unexpected error: %s", exc)
        return []


def _source_title(root: HtmlElement) -> str:
    for value in (
        _first_meta(root, "og:title", "dc.title", "citation_title"),
        _clean_text(root.xpath("string((//h1)[1])")),
        _clean_text(root.xpath("string((//h2)[1])")),
    ):
        title = _clean_text(value)
        if title:
            return title
    return ""


def _source_metadata(root: HtmlElement) -> dict[str, Any]:
    return {
        "object_type": _metadata_value(root, "Category", "Object category"),
        "period": _metadata_value(root, "Related period"),
        "collection": _metadata_value(root, "Part of", "Collection"),
        "catalogue_id": _metadata_value(root, "Catalogue number", "Catalogue ID"),
        "production_date": _metadata_value(root, "Production date"),
        "creator": _metadata_value(root, "Creator"),
    }


def _source_markdown(title: str, metadata: dict[str, Any], body_text: str) -> str:
    lines = [f"# {title}", ""]
    detail_lines: list[str] = []
    for label, key in (
        ("Object type", "object_type"),
        ("Related period", "period"),
        ("Collection", "collection"),
        ("Catalogue ID", "catalogue_id"),
        ("Production date", "production_date"),
        ("Creator", "creator"),
    ):
        value = metadata.get(key)
        if value:
            detail_lines.append(f"- {label}: {value}")
    if detail_lines:
        lines.extend(["## Object Details", "", *detail_lines])
    if body_text:
        lines.extend(["", "## Visible page text", "", body_text])
    return "\n".join(lines).strip()


def _parse_source_html(raw_html: str, *, url: str) -> Source | None:
    root = _parse_html(raw_html)
    title = _source_title(root)
    body = root.xpath("//body")
    body_node = body[0] if body else root
    body_text = _node_text(body_node)
    metadata = _source_metadata(root)
    cleaned_text = _source_markdown(title, metadata, body_text)
    if not title or not cleaned_text:
        return None
    return Source(
        url=url,
        title=title,
        cleaned_text=cleaned_text,
        raw_html=raw_html,
        fetched_at=datetime.now(UTC),
        source_kind=KIND,
        metadata=metadata,
    )


async def fetch(url: str) -> Source | None:
    """Fetch an IWM collection item page and return visible text + metadata."""
    if not url or not _is_iwm_item_url(url):
        return None

    try:
        async with browser.browser_session() as ctx:
            page = await ctx.new_page()
            try:
                await browser.navigate(page, url)
                raw_html = await page.content()
                try:
                    source = _parse_source_html(raw_html, url=url)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "iwm fetch selector drift: parse failed on %s: %s",
                        url,
                        exc,
                    )
                    await _save_diagnostic_dump(page, "fetch-selector-drift", html=raw_html)
                    return None
                if source is not None:
                    return source
                logger.warning("iwm fetch selector drift: no source parsed from %s", url)
                await _save_diagnostic_dump(page, "fetch-selector-drift", html=raw_html)
                return None
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
    except playwright.async_api.Error as exc:
        logger.warning("iwm fetch playwright error for %s: %s", url, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("iwm fetch unexpected error for %s: %s", url, exc)
        return None


def reset_for_tests() -> None:
    """Re-register host rates after browser.reset_for_tests(). Test-only."""
    _register_host_rates()


class _PayloadSchema(_BaseSearchPayload):
    max_results: int | None = None
    object_category: str | None = None
    related_period: str | None = None
    records_with_media: bool | None = None
    style: str | None = None
    page_size: int | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=tuple(sorted(_ACCEPTED_HOSTS)),
    skill_name="iwm",
    description=(
        "Imperial War Museums public collections: photographs, sound/oral "
        "histories, documents, film, objects (Playwright scrape, no auth)"
    ),
    optional_payload_knobs=(
        "`max_results`, `object_category`, `related_period`, "
        "`records_with_media`, `style`, `page_size`"
    ),
    example_query="Battle of Britain",
    module_name="iwm",
)


__all__ = ["KIND", "build_search_url", "fetch", "reset_for_tests", "search"]
