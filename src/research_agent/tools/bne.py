"""BNE Hemeroteca Digital connector (issue #240).

Public surface:

* ``async def search(query, *, max_results=20) -> list[SearchResult]`` opens
  ``https://hemerotecadigital.bne.es/hd/es/results?text=<query>`` through the
  shared Playwright session and parses BNE Digital/Hemeroteca result cards.
* ``async def fetch(url) -> Source | None`` opens a BNE result/detail/viewer
  page and returns visible metadata plus a PDF/download URL for downstream
  extraction.

The Hemeroteca Digital has no stable public search API and was migrated under
the newer BNE Digital platform in 2024-2025, so this connector treats
Playwright as the canonical path and writes selector-drift diagnostics under
``data/diagnostics/bne/``. No auth required. The host is rate-limited to
0.5 RPS.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

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

KIND: Literal["bne_search"] = "bne_search"

_CANONICAL_HOST = "hemerotecadigital.bne.es"
_ACCEPTED_HOSTS = frozenset(
    {
        "hemerotecadigital.bne.es",
        "www.hemerotecadigital.bne.es",
        "bnedigital.bne.es",
        "www.bnedigital.bne.es",
    }
)
_SITE_BASE = f"https://{_CANONICAL_HOST}"
_SEARCH_PATH = "/hd/es/results"
_PER_HOST_RPS = 0.5
_DIAGNOSTICS_DIR = Path("data/diagnostics/bne")

_WS_RE = re.compile(r"\s+")
_RESULT_PATH_RE = re.compile(r"^/hd/(?:es|ca|gl|eu|en)/(?:results|viewer|issn|datos)/?")
_DATE_DMY_RE = re.compile(r"\b(?P<d>\d{1,2})[/-](?P<m>\d{1,2})[/-](?P<y>\d{4})\b")
_DATE_ISO_RE = re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b")
_YEAR_RE = re.compile(r"\b((?:16|17|18|19|20)\d{2})\b")
_BRACKET_SUFFIX_RE = re.compile(r"\s*\[[^\]]+\]\s*$")

_ACTION_TEXT = frozenset(
    {
        "abrir",
        "abrir el ejemplar",
        "descargar",
        "descarga",
        "ver",
        "ver titulo",
        "ver el titulo",
        "calendario",
        "registro bibliografico",
    }
)


def _register_host_rates() -> None:
    for host in _ACCEPTED_HOSTS:
        browser.set_host_rate(host, _PER_HOST_RPS)


_register_host_rates()


def build_search_url(
    query: str,
    *,
    fechaDesde: str | None = None,
    fechaHasta: str | None = None,
    localizacion: str | None = None,
) -> str:
    """Return the BNE Digital/Hemeroteca search URL for ``query``."""
    params = {"text": query.strip()}
    for key, value in (
        ("fechaDesde", fechaDesde),
        ("fechaHasta", fechaHasta),
        ("localizacion", localizacion),
    ):
        text = _clean_text(value)
        if text:
            params[key] = text
    return f"{_SITE_BASE}{_SEARCH_PATH}?{urlencode(params)}"


def _clean_text(value: str | None) -> str:
    return _WS_RE.sub(" ", value or "").strip()


def _fold(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    ascii_text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _clean_text(ascii_text).casefold()


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


def _lang_from_root(root: HtmlElement) -> str:
    lang = _clean_text(root.xpath("string((//html/@lang)[1])"))
    if not lang:
        return "es"
    return lang.split("_", 1)[0].split("-", 1)[0].casefold()


def _normalize_lang(value: str | None, *, fallback: str = "es") -> str:
    folded = _fold(value)
    if not folded:
        return fallback
    if folded in {"es", "spa", "spanish", "espanol", "castellano"}:
        return "spa"
    if folded in {"ca", "cat", "catalan", "catala"}:
        return "cat"
    if folded in {"gl", "glg", "gallego", "galego"}:
        return "glg"
    if folded in {"eu", "baq", "eus", "vasco", "euskera"}:
        return "eus"
    if folded in {"en", "eng", "english", "ingles"}:
        return "eng"
    return folded.split(" ", 1)[0]


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


def _is_bne_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    return parsed.scheme in {"http", "https"} and host in _ACCEPTED_HOSTS


def _is_fetchable_bne_url(url: str) -> bool:
    if not _is_bne_url(url):
        return False
    path = urlparse(url).path
    return path.startswith("/hd/")


def _is_resultish_url(url: str) -> bool:
    if not _is_bne_url(url):
        return False
    return bool(_RESULT_PATH_RE.match(urlparse(url).path))


def _is_fulltext_href(text: str, href: str) -> bool:
    folded_text = _fold(text)
    folded_href = _fold(href)
    return (
        "descargar" in folded_text
        or "download" in folded_href
        or "descarga" in folded_href
        or ".pdf" in folded_href
        or "/pdf" in folded_href
    )


def _is_viewer_href(text: str, href: str) -> bool:
    folded_text = _fold(text)
    folded_href = _fold(href)
    return "abrir" in folded_text or "/viewer" in folded_href or "/view" in folded_href


def _is_action_anchor(text: str, href: str) -> bool:
    folded_text = _fold(text).strip(" :")
    if folded_text in _ACTION_TEXT:
        return True
    return _is_fulltext_href(text, href) or _is_viewer_href(text, href)


def _parse_html(raw_html: str) -> HtmlElement:
    return lxml_html.fromstring(raw_html or "<html></html>")


def _text_lines(node: HtmlElement) -> list[str]:
    return [
        _clean_text(str(value))
        for value in node.xpath(".//text()")
        if _clean_text(str(value))
    ]


def _metadata_value(node: HtmlElement, *labels: str) -> str:
    wanted = {_fold(label).rstrip(":") for label in labels}

    for meta in node.xpath(".//meta[@name or @property]"):
        key = _fold(meta.get("name") or meta.get("property")).rstrip(":")
        if key in wanted:
            value = _clean_text(meta.get("content"))
            if value:
                return value

    for label_node in node.xpath(
        ".//*[self::dt or self::th or self::strong or self::b "
        "or contains(translate(@class, 'LABEL', 'label'), 'label')]"
    ):
        label_text = _fold(_node_text(label_node)).rstrip(":")
        if label_text not in wanted:
            continue
        if label_node.tag.casefold() == "th":
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
            return lines[index + 1]
        for label in wanted:
            if folded.startswith(f"{label}:"):
                value = line.split(":", 1)[1].strip()
                if value:
                    return value
    return ""


def _first_meta(root: HtmlElement, *names: str) -> str:
    wanted = {_fold(name) for name in names}
    for meta in root.xpath("//meta[@name or @property]"):
        key = _fold(meta.get("name") or meta.get("property"))
        if key in wanted:
            value = _clean_text(meta.get("content"))
            if value:
                return value
    return ""


def _title_without_kind(title: str) -> str:
    text = _BRACKET_SUFFIX_RE.sub("", _clean_text(title)).strip(" .,-")
    return text


def _publication_from_title(title: str) -> str:
    text = _title_without_kind(title)
    text = _DATE_DMY_RE.sub("", text)
    text = _DATE_ISO_RE.sub("", text)
    return _clean_text(text).strip(" .,-")


def _place_from_title(title: str) -> str:
    match = re.search(r"\((?P<place>[^()]+)\)", title or "")
    if match is None:
        return ""
    place = match.group("place").split(".", 1)[0].split(",", 1)[0]
    return _clean_text(place).strip(" .,-")


def _pub_date_from_text(text: str) -> str:
    match = _DATE_DMY_RE.search(text or "")
    if match is not None:
        day = int(match.group("d"))
        month = int(match.group("m"))
        year = int(match.group("y"))
        try:
            return datetime(year, month, day, tzinfo=UTC).date().isoformat()
        except ValueError:
            return match.group(0)
    match = _DATE_ISO_RE.search(text or "")
    if match is not None:
        day = int(match.group("d"))
        month = int(match.group("m"))
        year = int(match.group("y"))
        try:
            return datetime(year, month, day, tzinfo=UTC).date().isoformat()
        except ValueError:
            return match.group(0)
    match = _YEAR_RE.search(text or "")
    return match.group(1) if match is not None else ""


def _published_at(pub_date: str) -> datetime | None:
    if not pub_date:
        return None
    for fmt in ("%Y-%m-%d", "%Y"):
        try:
            if fmt == "%Y":
                return datetime(int(pub_date), 1, 1, tzinfo=UTC)
            return datetime.strptime(pub_date, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _first_fulltext_url(node: HtmlElement, *, base_url: str) -> str:
    meta = _first_meta(
        node,
        "citation_pdf_url",
        "dc.identifier.pdf",
        "og:pdf",
    )
    if meta:
        return _absolute_url(base_url, meta)

    viewer_url = ""
    for link in node.xpath(".//a[@href]"):
        href = _clean_text(link.get("href"))
        text = _node_text(link)
        absolute = _absolute_url(base_url, href)
        if not _is_bne_url(absolute):
            continue
        if _is_fulltext_href(text, href):
            return absolute
        if not viewer_url and _is_viewer_href(text, href):
            viewer_url = absolute
    return viewer_url


def _primary_url_from_card(card: HtmlElement, *, base_url: str) -> str:
    candidates: list[tuple[str, str, str]] = []
    for link in card.xpath(".//a[@href]"):
        href = _clean_text(link.get("href"))
        text = _node_text(link)
        absolute = _absolute_url(base_url, href)
        if not _is_resultish_url(absolute):
            continue
        candidates.append((absolute, text, href))

    for absolute, text, href in candidates:
        if not _is_action_anchor(text, href):
            return absolute
    for absolute, text, href in candidates:
        if not _is_fulltext_href(text, href):
            return absolute
    return ""


def _extract_title(card: HtmlElement) -> str:
    for xpath in (
        "string((.//*[self::h1 or self::h2 or self::h3 or self::h4])[1])",
        "string((.//*[contains(translate(@class, 'TITLE', 'title'), 'title')])[1])",
    ):
        value = _clean_text(card.xpath(xpath))
        if value and _fold(value) not in {"resultado", "resultados"}:
            return value
    for link in card.xpath(".//a[@href]"):
        text = _node_text(link)
        href = _clean_text(link.get("href"))
        if text and not _is_action_anchor(text, href):
            return text
    return ""


def _candidate_cards(root: HtmlElement) -> list[HtmlElement]:
    candidates: list[HtmlElement] = []
    seen: set[int] = set()
    xpath = (
        "//article[.//a[@href]]"
        "|//li[.//a[contains(@href, '/hd/')]]"
        "|//div[.//a[contains(@href, '/hd/')] and "
        "(contains(translate(@class, 'RESULTCARDITEMNOTICE', 'resultcarditemnotice'), 'result') "
        "or contains(translate(@class, 'RESULTCARDITEMNOTICE', 'resultcarditemnotice'), 'card') "
        "or contains(translate(@class, 'RESULTCARDITEMNOTICE', 'resultcarditemnotice'), 'item') "
        "or contains(translate(@class, 'RESULTCARDITEMNOTICE', 'resultcarditemnotice'), 'notice') "
        "or contains(translate(@id, 'RESULTCARDITEMNOTICE', 'resultcarditemnotice'), 'result'))]"
    )
    for node in root.xpath(xpath):
        ident = id(node)
        if ident in seen:
            continue
        text = _node_text(node)
        if 30 <= len(text) <= 5000:
            seen.add(ident)
            candidates.append(node)
    return candidates


def _card_for_link(link: HtmlElement) -> HtmlElement:
    node: HtmlElement = link
    best = link
    for _ in range(6):
        parent = node.getparent()
        if parent is None:
            break
        text = _node_text(parent)
        class_name = (parent.get("class") or "").casefold()
        ident = (parent.get("id") or "").casefold()
        if 30 <= len(text) <= 5000:
            best = parent
        if 30 <= len(text) <= 5000 and any(
            token in class_name or token in ident
            for token in ("result", "card", "item", "notice", "record")
        ):
            return parent
        node = parent
    return best


def _snippet(text: str, title: str) -> str:
    snippet = _clean_text(text)
    if title and snippet.startswith(title):
        snippet = snippet[len(title) :].strip(" .,-")
    for action in ("Abrir el ejemplar", "Descargar", "Ver el titulo", "Ver el título"):
        snippet = snippet.replace(action, " ")
    return _clean_text(snippet)[:600]


def _result_from_card(
    card: HtmlElement,
    *,
    base_url: str,
    default_lang: str,
) -> SearchResult | None:
    title = _extract_title(card)
    url = _primary_url_from_card(card, base_url=base_url)
    if not title or not url:
        return None

    fulltext_url = _first_fulltext_url(card, base_url=base_url)
    publication = (
        _metadata_value(card, "publication", "publicacion", "publicación", "titulo", "título")
        or _publication_from_title(title)
    )
    pub_date = _pub_date_from_text(
        _metadata_value(card, "fecha", "date", "fecha de publicacion")
        or title
        or _node_text(card)
    )
    place = (
        _metadata_value(
            card,
            "lugar de publicacion",
            "lugar de publicación",
            "localizacion",
            "localización",
            "place",
        )
        or _place_from_title(title)
    )
    lang = _normalize_lang(
        _metadata_value(card, "idioma", "lengua", "language"),
        fallback=default_lang,
    )
    extras: dict[str, Any] = {
        "publication": publication,
        "pub_date": pub_date,
        "place": place,
        "lang": lang,
        "fulltext_url": fulltext_url,
    }
    return SearchResult(
        url=url,
        title=title,
        snippet=_snippet(_node_text(card), title),
        published_at=_published_at(pub_date),
        source_kind=KIND,
        extras=extras,
    )


def _parse_search_html(raw_html: str, *, base_url: str, max_results: int) -> list[SearchResult]:
    root = _parse_html(raw_html)
    default_lang = _normalize_lang(_lang_from_root(root), fallback="es")
    cards = _candidate_cards(root)
    if not cards:
        for link in root.xpath("//a[contains(@href, '/hd/')]"):
            card = _card_for_link(link)
            if card not in cards:
                cards.append(card)

    results: list[SearchResult] = []
    seen: set[str] = set()
    for card in cards:
        result = _result_from_card(card, base_url=base_url, default_lang=default_lang)
        if result is None or result.url in seen:
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
            "no se han encontrado resultados",
            "no hay resultados",
            "sin resultados",
            "0 resultados",
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
            logger.debug("bne diagnostic screenshot failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("bne diagnostic dump failed: %s", exc)


async def search(
    query: str,
    *,
    max_results: int = 20,
    fechaDesde: str | None = None,
    fechaHasta: str | None = None,
    localizacion: str | None = None,
) -> list[SearchResult]:
    """Search BNE Hemeroteca Digital result cards through Playwright."""
    q = query.strip()
    if not q or max_results <= 0:
        return []
    search_url = build_search_url(
        q,
        fechaDesde=fechaDesde,
        fechaHasta=fechaHasta,
        localizacion=localizacion,
    )

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
                    "bne search selector drift: no result cards parsed from %s",
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
        logger.warning("bne search playwright error: %s", exc)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("bne search unexpected error: %s", exc)
        return []


def _source_title(root: HtmlElement) -> str:
    for value in (
        _first_meta(root, "og:title", "dc.title", "citation_title"),
        _clean_text(root.xpath("string((//h1)[1])")),
        _clean_text(root.xpath("string((//h2)[1])")),
        _metadata_value(root, "titulo", "título", "publicacion", "publicación"),
    ):
        title = _clean_text(value)
        if title:
            return title
    return ""


def _metadata_from_url(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    pub_date = ""
    dates = query.get("d") or []
    if dates:
        for value in dates:
            pub_date = _pub_date_from_text(value)
            if pub_date:
                break
    return {"pub_date": pub_date}


def _source_metadata(root: HtmlElement, *, title: str, url: str) -> dict[str, Any]:
    url_metadata = _metadata_from_url(url)
    publication = (
        _metadata_value(root, "publicacion", "publicación", "titulo", "título")
        or _publication_from_title(title)
    )
    pub_date = (
        _pub_date_from_text(
            _metadata_value(root, "fecha", "date", "fecha de publicacion")
            or title
            or _node_text(root)
        )
        or url_metadata["pub_date"]
    )
    place = (
        _metadata_value(
            root,
            "lugar de publicacion",
            "lugar de publicación",
            "localizacion",
            "localización",
            "ambito geografico",
            "ámbito geográfico",
            "place",
        )
        or _place_from_title(title)
    )
    lang = _normalize_lang(
        _metadata_value(root, "idioma", "lengua", "language"),
        fallback=_normalize_lang(_lang_from_root(root), fallback="es"),
    )
    fulltext_url = _first_fulltext_url(root, base_url=url)
    if not fulltext_url and (
        "download" in _fold(url) or "descarga" in _fold(url) or urlparse(url).path.endswith(".pdf")
    ):
        fulltext_url = url
    return {
        "publication": publication,
        "pub_date": pub_date,
        "place": place,
        "lang": lang,
        "fulltext_url": fulltext_url,
    }


def _source_markdown(title: str, metadata: dict[str, Any], body_text: str) -> str:
    lines = [f"# {title}", ""]
    for label, key in (
        ("Publication", "publication"),
        ("Publication date", "pub_date"),
        ("Place", "place"),
        ("Language", "lang"),
        ("Full text/PDF", "fulltext_url"),
    ):
        value = metadata.get(key)
        if value:
            lines.append(f"- {label}: {value}")
    if body_text:
        lines.extend(["", "## Visible page text", "", body_text])
    return "\n".join(lines).strip()


def _parse_source_html(raw_html: str, *, url: str) -> Source | None:
    root = _parse_html(raw_html)
    title = _source_title(root)
    body = root.xpath("//body")
    body_node = body[0] if body else root
    metadata = _source_metadata(root, title=title, url=url)
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
    """Fetch a BNE page and return visible metadata plus fulltext URL."""
    if not url or not _is_fetchable_bne_url(url):
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
                logger.warning("bne fetch selector drift: no source parsed from %s", url)
                await _save_diagnostic_dump(page, "fetch-selector-drift", html=raw_html)
                return None
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
    except playwright.async_api.Error as exc:
        logger.warning("bne fetch playwright error for %s: %s", url, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("bne fetch unexpected error for %s: %s", url, exc)
        return None


def reset_for_tests() -> None:
    """Re-register host rates after browser.reset_for_tests(). Test-only."""
    _register_host_rates()


class _PayloadSchema(_BaseSearchPayload):
    max_results: int | None = None
    fechaDesde: str | None = None
    fechaHasta: str | None = None
    localizacion: str | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=tuple(sorted(_ACCEPTED_HOSTS)),
    skill_name="bne",
    description=(
        "BNE Hemeroteca Digital Spanish historical press "
        "(Playwright scrape, no auth)"
    ),
    optional_payload_knobs="`max_results`, `fechaDesde`, `fechaHasta`, `localizacion`",
    example_query="guerra civil 1936",
    module_name="bne",
)
