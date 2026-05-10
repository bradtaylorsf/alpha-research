"""Persee French academic journals connector (issue #239).

Public surface:

* ``async def search(query, *, max_results=20) -> list[SearchResult]`` opens
  ``https://www.persee.fr/search?ta=article&q=<query>`` through the shared
  Playwright session and parses article hits.
* ``async def fetch(url) -> Source | None`` opens a Persee article page and
  returns cleaned article text plus bibliographic metadata.

Persee's public API surface is partial, while the search UI exposes the
article cards operators need. This connector therefore treats Playwright as
the default path. No auth required. The host is rate-limited to 0.5 RPS and
selector-drift diagnostics are written under ``data/diagnostics/persee/``.
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

KIND: Literal["persee_search"] = "persee_search"

_HOST = "www.persee.fr"
_ACCEPTED_HOSTS = frozenset({"www.persee.fr", "persee.fr"})
_SITE_BASE = "https://www.persee.fr"
_SEARCH_URL = f"{_SITE_BASE}/search"
_PER_HOST_RPS = 0.5
_DIAGNOSTICS_DIR = Path("data/diagnostics/persee")

_WS_RE = re.compile(r"\s+")
_DOI_RE = re.compile(
    r"(?:doi\s*:\s*)?(?:https?://(?:dx\.)?doi\.org/)?"
    r"(10\.\d{4,9}/[^\s<>'\";,]+)",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b((?:18|19|20)\d{2})\b")
_URL_YEAR_RE = re.compile(r"_(18|19|20)\d{2}_")
_URL_VOLUME_RE = re.compile(r"_num_([^_/?#]+)")
_ARTICLE_TYPE_RE = re.compile(r"\s*\[[^\]]+\]\s*$")


def _register_host_rates() -> None:
    browser.set_host_rate(_HOST, _PER_HOST_RPS)
    browser.set_host_rate("persee.fr", _PER_HOST_RPS)


_register_host_rates()


def build_search_url(query: str) -> str:
    """Return the Persee article-search URL for ``query``."""
    return f"{_SEARCH_URL}?{urlencode({'ta': 'article', 'q': query.strip()})}"


def _clean_text(value: str | None) -> str:
    return _WS_RE.sub(" ", value or "").strip()


def _node_text(node: HtmlElement) -> str:
    return _clean_text(" ".join(node.itertext()))


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


def _absolute_url(base: str, href: str | None) -> str:
    text = _clean_text(href)
    if not text:
        return ""
    if text.startswith("//"):
        return "https:" + text
    return urljoin(base, text)


def _is_persee_article_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and (parsed.hostname or "").casefold() in _ACCEPTED_HOSTS
        and parsed.path.startswith("/doc/")
    )


def _normalize_doi(value: str) -> str:
    doi = _clean_text(value)
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    return doi.rstrip(").]")


def _extract_doi(node: HtmlElement) -> str:
    for href in node.xpath(".//a[contains(translate(@href, 'DOI', 'doi'), 'doi.org')]/@href"):
        match = _DOI_RE.search(str(href))
        if match is not None:
            return _normalize_doi(match.group(1))
    text = _node_text(node)
    match = _DOI_RE.search(text)
    return _normalize_doi(match.group(1)) if match is not None else ""


def _parse_year_from_url(url: str) -> str:
    match = _URL_YEAR_RE.search(urlparse(url).path)
    if match is None:
        return ""
    return match.group(0).strip("_")


def _parse_volume_from_url(url: str) -> str:
    match = _URL_VOLUME_RE.search(urlparse(url).path)
    return match.group(1) if match is not None else ""


def _lang_from_root(root: HtmlElement) -> str:
    lang = _clean_text(root.xpath("string((//html/@lang)[1])"))
    if not lang:
        return "fr"
    return lang.split("_", 1)[0].split("-", 1)[0].casefold()


def _meta_values(root: HtmlElement, *names: str) -> list[str]:
    values: list[str] = []
    lowered = {name.casefold() for name in names}
    for meta in root.xpath("//meta[@name or @property]"):
        key = _clean_text(meta.get("name") or meta.get("property")).casefold()
        if key not in lowered:
            continue
        values.append(_clean_text(meta.get("content")))
    return _dedupe(values)


def _first_meta(root: HtmlElement, *names: str) -> str:
    values = _meta_values(root, *names)
    return values[0] if values else ""


def _doi_from_values(values: list[str]) -> str:
    for value in values:
        match = _DOI_RE.search(value)
        if match is not None:
            return _normalize_doi(match.group(1))
    return ""


def _candidate_lines(node: HtmlElement) -> list[str]:
    lines: list[str] = []
    for child in node.xpath(".//*[self::p or self::div or self::span or self::li]"):
        text = _node_text(child)
        if 0 < len(text) <= 800:
            lines.append(text)
    text = _node_text(node)
    if text:
        lines.append(text[:1200])
    return _dedupe(lines)


def _first_xpath_text(node: HtmlElement, xpath: str) -> str:
    raw = node.xpath(xpath)
    if isinstance(raw, str):
        return _clean_text(raw)
    values = [_clean_text(str(value)) for value in raw if _clean_text(str(value))]
    return values[0] if values else ""


def _extract_title(card: HtmlElement, fallback: str) -> str:
    for xpath in (
        "string((.//*[self::h1 or self::h2 or self::h3 or self::h4])[1])",
        "string((.//*[contains(translate(@class, 'TITLE', 'title'), 'title')])[1])",
        "string((.//a[contains(@href, '/doc/') and not(contains(., 'www.persee.fr'))])[1])",
    ):
        title = _first_xpath_text(card, xpath)
        title = _ARTICLE_TYPE_RE.sub("", title).strip()
        if title and "www.persee.fr/doc/" not in title:
            return title
    fallback = _ARTICLE_TYPE_RE.sub("", _clean_text(fallback)).strip()
    return "" if "www.persee.fr/doc/" in fallback else fallback


def _extract_authors(card: HtmlElement, title: str) -> list[str]:
    class_texts = [
        _node_text(node)
        for node in card.xpath(
            ".//*[contains(translate(@class, 'AUTEHOR', 'autehor'), 'author') "
            "or contains(translate(@class, 'AUTEHOR', 'autehor'), 'auteur')]"
        )
    ]
    authors = _split_authors(" ; ".join(class_texts))
    if authors:
        return authors

    text = _node_text(card)
    if title:
        match = re.search(rf"^(.{{2,160}}?)\.\s+{re.escape(title[:80])}", text)
        if match is not None:
            return _split_authors(match.group(1))
    return []


def _split_authors(value: str) -> list[str]:
    text = _clean_text(value)
    if not text:
        return []
    text = re.sub(r"^(?:Auteur(?:s)?|Author(?:s)?)\s*:\s*", "", text, flags=re.I)
    if ";" in text:
        parts = text.split(";")
    elif " et " in text:
        parts = re.split(r"\s+et\s+", text)
    else:
        parts = [text]
    return _dedupe([part.strip(" ,.") for part in parts if part.strip(" ,.")])


def _extract_journal_volume_year(card: HtmlElement, url: str) -> tuple[str, str, str]:
    journal = ""
    url_volume = _parse_volume_from_url(url)
    volume = ""
    year = _parse_year_from_url(url)

    for line in _candidate_lines(card):
        match = re.search(
            r"(?P<journal>.+?)\s+(?:Ann[eé]e|Annee)\s+"
            r"(?P<year>(?:18|19|20)\d{2})"
            r"(?:\s+(?P<volume>[A-Za-z0-9_.-]+))?",
            line,
            flags=re.IGNORECASE,
        )
        if match is not None:
            journal = _clean_text(match.group("journal")).strip(" .,-")
            year = year or match.group("year")
            volume = volume or _clean_text(match.group("volume"))
            break

        match = re.search(
            r"\bIn\s*:\s*(?P<journal>.+?)(?:,\s*(?:n[°o]|tome|vol\.?|volume)"
            r"|\.\s+pp\.|\s+Ann[eé]e\b)",
            line,
            flags=re.IGNORECASE,
        )
        if match is not None and not journal:
            journal = _clean_text(match.group("journal")).strip(" .,-")

    if not year:
        text = _node_text(card)
        match = _YEAR_RE.search(text)
        year = match.group(1) if match is not None else ""
    if not volume:
        text = _node_text(card)
        for pattern in (
            r"(?:Vol\.?|volume|tome)\s+([A-Za-z0-9_.-]+)",
            r"n[°o]\s*([A-Za-z0-9_.-]+)",
        ):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match is not None:
                volume = _clean_text(match.group(1))
                break
    volume = volume or url_volume
    return journal, volume, year


def _card_for_link(link: HtmlElement) -> HtmlElement:
    node: HtmlElement = link
    best = link
    for _ in range(6):
        parent = node.getparent()
        if parent is None:
            break
        text = _node_text(parent)
        class_name = (parent.get("class") or "").casefold()
        if 60 <= len(text) <= 3000:
            best = parent
        if (
            60 <= len(text) <= 3000
            and any(
                token in class_name
                for token in ("result", "notice", "document", "record", "item")
            )
        ):
            return parent
        node = parent
    return best


def _snippet(text: str, title: str) -> str:
    snippet = _clean_text(text)
    if title and snippet.startswith(title):
        snippet = snippet[len(title) :].strip(" .,-")
    return snippet[:600]


def _result_from_card(
    card: HtmlElement,
    *,
    url: str,
    link_text: str,
    lang: str,
) -> SearchResult | None:
    title = _extract_title(card, link_text)
    if not title:
        return None
    journal, volume, pub_year = _extract_journal_volume_year(card, url)
    authors = _extract_authors(card, title)
    doi = _extract_doi(card)
    metadata: dict[str, Any] = {
        "doi": doi,
        "journal": journal,
        "volume": volume,
        "pub_year": pub_year,
        "authors": authors,
        "lang": lang,
    }
    published_at = None
    if pub_year:
        try:
            published_at = datetime(int(pub_year), 1, 1, tzinfo=UTC)
        except ValueError:
            published_at = None
    return SearchResult(
        url=url,
        title=title,
        snippet=_snippet(_node_text(card), title),
        published_at=published_at,
        source_kind=KIND,
        extras=metadata,
    )


def _parse_html(raw_html: str) -> HtmlElement:
    return lxml_html.fromstring(raw_html or "<html></html>")


def _parse_search_html(raw_html: str, *, base_url: str, max_results: int) -> list[SearchResult]:
    root = _parse_html(raw_html)
    lang = _lang_from_root(root)
    results: list[SearchResult] = []
    seen: set[str] = set()
    for link in root.xpath("//a[contains(@href, '/doc/')]"):
        href = _clean_text(link.get("href"))
        url = _absolute_url(base_url, href)
        if not _is_persee_article_url(url) or url in seen:
            continue
        card = _card_for_link(link)
        result = _result_from_card(
            card,
            url=url,
            link_text=_node_text(link),
            lang=lang,
        )
        if result is None:
            continue
        seen.add(url)
        results.append(result)
        if len(results) >= max_results:
            break
    return results


def _looks_like_empty_search(raw_html: str) -> bool:
    text = _clean_text(lxml_html.fromstring(raw_html or "<html></html>").text_content())
    lowered = text.casefold()
    return any(
        marker in lowered
        for marker in (
            "aucun résultat",
            "aucun resultat",
            "0 résultat",
            "0 resultat",
            "no results",
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
            logger.debug("persee diagnostic screenshot failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("persee diagnostic dump failed: %s", exc)


async def search(query: str, *, max_results: int = 20) -> list[SearchResult]:
    """Search Persee article records via the public Playwright-rendered UI."""
    q = query.strip()
    if not q or max_results <= 0:
        return []
    search_url = build_search_url(q)

    try:
        async with browser.browser_session() as ctx:
            page = await ctx.new_page()
            try:
                await browser.navigate(page, search_url)
                raw_html = await page.content()
                results = _parse_search_html(
                    raw_html,
                    base_url=search_url,
                    max_results=max_results,
                )
                if results or _looks_like_empty_search(raw_html):
                    return results
                logger.warning(
                    "persee search selector drift: no article cards parsed from %s",
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
        logger.warning("persee search playwright error: %s", exc)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("persee search unexpected error: %s", exc)
        return []


def _source_metadata(root: HtmlElement, fallback_node: HtmlElement, url: str) -> dict[str, Any]:
    title = _first_meta(root, "citation_title", "dc.title", "og:title")
    journal = _first_meta(root, "citation_journal_title")
    volume = _first_meta(root, "citation_volume")
    pub_year = _first_meta(root, "citation_publication_date", "citation_date")
    authors = _meta_values(root, "citation_author", "dc.creator")
    doi = _first_meta(root, "citation_doi")
    if doi:
        doi = _doi_from_values([doi]) or _normalize_doi(doi)
    else:
        doi = _doi_from_values(_meta_values(root, "dc.identifier"))
    lang = _first_meta(root, "citation_language", "dc.language") or _lang_from_root(root)

    text_metadata_node = fallback_node
    if not any((journal, volume, pub_year)):
        parsed_journal, parsed_volume, parsed_year = _extract_journal_volume_year(
            text_metadata_node,
            url,
        )
        journal = journal or parsed_journal
        volume = volume or parsed_volume
        pub_year = pub_year or parsed_year
    if not authors:
        authors = _extract_authors(text_metadata_node, title)
    if not doi:
        doi = _extract_doi(text_metadata_node)
    if pub_year:
        match = _YEAR_RE.search(pub_year)
        pub_year = match.group(1) if match is not None else pub_year
    return {
        "doi": doi,
        "journal": journal,
        "volume": volume,
        "pub_year": pub_year,
        "authors": authors,
        "lang": lang.split("_", 1)[0].split("-", 1)[0].casefold() if lang else "fr",
    }


def _source_title(root: HtmlElement) -> str:
    for value in (
        _first_meta(root, "citation_title", "dc.title", "og:title"),
        _first_xpath_text(root, "string((//h1)[1])"),
        _first_xpath_text(root, "string((//h2)[1])"),
    ):
        title = _ARTICLE_TYPE_RE.sub("", value).strip()
        if title:
            return title
    return "Persee article"


def _source_markdown(title: str, metadata: dict[str, Any], body_text: str) -> str:
    lines = [f"# {title}", ""]
    for label, key in (
        ("DOI", "doi"),
        ("Journal", "journal"),
        ("Volume", "volume"),
        ("Publication year", "pub_year"),
        ("Language", "lang"),
    ):
        value = metadata.get(key)
        if value:
            lines.append(f"- {label}: {value}")
    authors = metadata.get("authors")
    if isinstance(authors, list) and authors:
        lines.append(f"- Authors: {'; '.join(str(author) for author in authors)}")
    if body_text:
        lines.extend(["", "## Article text", "", body_text])
    return "\n".join(lines).strip()


def _parse_source_html(raw_html: str, *, url: str) -> Source | None:
    root = _parse_html(raw_html)
    title = _source_title(root)
    body = root.xpath("//body")
    body_node = body[0] if body else root
    metadata = _source_metadata(root, body_node, url)
    body_text = _node_text(body_node)
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
    """Fetch a Persee article page and return article text + metadata."""
    if not url or not _is_persee_article_url(url):
        return None

    try:
        async with browser.browser_session() as ctx:
            page = await ctx.new_page()
            try:
                await browser.navigate(page, url)
                raw_html = await page.content()
                source = _parse_source_html(raw_html, url=url)
                if source is not None:
                    return source
                logger.warning("persee fetch selector drift: no source parsed from %s", url)
                await _save_diagnostic_dump(page, "fetch-selector-drift", html=raw_html)
                return None
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
    except playwright.async_api.Error as exc:
        logger.warning("persee fetch playwright error for %s: %s", url, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("persee fetch unexpected error for %s: %s", url, exc)
        return None


def reset_for_tests() -> None:
    """Re-register host rates after browser.reset_for_tests(). Test-only."""
    _register_host_rates()


class _PayloadSchema(_BaseSearchPayload):
    max_results: int | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=("www.persee.fr", "persee.fr"),
    skill_name="persee",
    description=(
        "Persee French academic journals in humanities and social sciences "
        "(Playwright scrape, no auth)"
    ),
    optional_payload_knobs="`max_results`",
    example_query="guerre d'Algerie",
    module_name="persee",
)


__all__ = [
    "KIND",
    "build_search_url",
    "fetch",
    "reset_for_tests",
    "search",
]
