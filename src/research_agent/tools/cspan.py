"""C-SPAN Video Library connector (issue #242).

Public surface:

* ``async def search(query, *, max_results=20) -> list[SearchResult]`` opens
  ``https://www.c-span.org/search/?searchtype=Videos&query=<query>`` through
  the shared Playwright session and parses video-library hits.
* ``async def fetch(url) -> Source | None`` opens a C-SPAN program or clip page
  and returns program metadata plus transcript text in ``Source.cleaned_text``.

C-SPAN does not expose a stable public API for this workflow, and transcript
availability varies by program. This connector therefore treats Playwright as
the canonical path, rate-limits C-SPAN hosts to 0.5 RPS, and writes
selector-drift diagnostics under ``data/diagnostics/cspan/``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
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

KIND: Literal["cspan_search"] = "cspan_search"

_CANONICAL_HOST = "www.c-span.org"
_ACCEPTED_HOSTS = frozenset({"www.c-span.org", "c-span.org", "www.cspan.org", "cspan.org"})
_SITE_BASE = f"https://{_CANONICAL_HOST}"
_SEARCH_PATH = "/search/"
_PER_HOST_RPS = 0.5
_DIAGNOSTICS_DIR = Path("data/diagnostics/cspan")

_WS_RE = re.compile(r"\s+")
_PROGRAM_ID_RE = re.compile(r"(?:^|[^\d])(\d{5,})(?:[^\d]|$)")
_DATE_ISO_RE = re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b")
_DATE_MDY_SLASH_RE = re.compile(r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})\b")
_DATE_MDY_TEXT_RE = re.compile(
    r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\.?\s+(?P<d>\d{1,2}),\s+(?P<y>\d{4})\b",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"\b(?:(?P<h>\d{1,2}):)?(?P<m>\d{1,2}):(?P<s>\d{2})\b")
_ISO_DURATION_RE = re.compile(
    r"^P(?:T)?(?:(?P<h>\d+(?:\.\d+)?)H)?(?:(?P<m>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)S)?$",
    re.IGNORECASE,
)
_HOURS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|hr)\b", re.IGNORECASE)
_MINUTES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|min)\b", re.IGNORECASE)
_SECONDS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|sec)\b", re.IGNORECASE)
_TRANSCRIPT_KEY_RE = re.compile(r"(transcript|caption|closedcaption|segment)", re.I)
_TIME_LINE_RE = re.compile(
    r"^(?P<time>(?:\d{1,2}:)?\d{1,2}:\d{2})(?:\s+(?P<rest>.+))?$"
)
_TRANSCRIPT_BOUNDARY_RE = re.compile(
    r"^(?:people in this video|hosting organization|more videos from|related video|"
    r"user created clips|featured clips|more information about|purchase a dvd|about c-span|"
    r"resources|follow c-span|channel finder)\b",
    re.IGNORECASE,
)
_TRANSCRIPT_NOTE_RE = re.compile(
    r"this transcript was compiled from uncorrected closed captioning",
    re.IGNORECASE,
)
_TRANSCRIPT_UI_TEXT = frozenset(
    {
        "all speakers",
        "bookmark to myc-span",
        "clip",
        "embed",
        "filter by speaker",
        "report video issue",
        "search this transcript",
        "show full text",
        "show less",
        "text",
        "transcript type",
    }
)
_ACTION_TEXT = frozenset(
    {
        "c-span",
        "donate",
        "global search",
        "quick guide",
        "search",
        "share",
        "video",
        "watch",
    }
)


@dataclass(frozen=True)
class _TranscriptSegment:
    time_start: str
    speaker: str
    text: str


def _register_host_rates() -> None:
    for host in _ACCEPTED_HOSTS:
        browser.set_host_rate(host, _PER_HOST_RPS)


_register_host_rates()


def build_search_url(query: str, *, type: str | None = None) -> str:  # noqa: A002
    """Return the C-SPAN Video Library search URL for ``query``."""
    params = {"searchtype": "Videos", "query": query.strip()}
    video_type = _clean_text(type)
    if video_type:
        params["type"] = video_type
    return f"{_SITE_BASE}{_SEARCH_PATH}?{urlencode(params)}"


def _clean_text(value: str | None) -> str:
    return _WS_RE.sub(" ", value or "").strip()


def _fold(value: str | None) -> str:
    return _clean_text(value).casefold()


def _node_text(node: HtmlElement) -> str:
    return _clean_text(" ".join(node.itertext()))


def _dedupe(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(str(value))
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _parse_html(raw_html: str) -> HtmlElement:
    return lxml_html.fromstring(raw_html or "<html></html>")


def _absolute_url(base: str, href: str | None) -> str:
    text = _clean_text(href)
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    absolute = urljoin(base, text)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").casefold()
    if host in {"www.cspan.org", "cspan.org"}:
        host = _CANONICAL_HOST
    if host in _ACCEPTED_HOSTS:
        return parsed._replace(scheme="https", netloc=host, fragment="").geturl()
    return absolute


def _is_cspan_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and (parsed.hostname or "").casefold() in _ACCEPTED_HOSTS
    )


def _is_video_url(url: str) -> bool:
    if not _is_cspan_url(url):
        return False
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") + "/"
    if path.startswith(("/program/", "/clip/", "/video/")):
        return True
    query = parse_qs(parsed.query)
    query_parts = [parsed.query, *query.keys()]
    query_parts.extend(value for values in query.values() for value in values)
    return parsed.path.rstrip("/") == "/video" and any(
        _PROGRAM_ID_RE.search(value) for value in query_parts
    )


def _program_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    for part in reversed([p for p in parsed.path.split("/") if p]):
        if part.isdigit() and len(part) >= 5:
            return part
        match = _PROGRAM_ID_RE.search(part)
        if match is not None:
            return match.group(1)
    for values in parse_qs(parsed.query).values():
        for value in values:
            match = _PROGRAM_ID_RE.search(value)
            if match is not None:
                return match.group(1)
    for value in (parsed.query, *parse_qs(parsed.query).keys()):
        match = _PROGRAM_ID_RE.search(value)
        if match is not None:
            return match.group(1)
    return ""


def _program_id_from_text(text: str) -> str:
    for pattern in (
        r"(?:program[_\s-]?id|progid|programid)\D{0,30}(\d{5,})",
        r"(?:clip[_\s-]?id|video[_\s-]?id)\D{0,30}(\d{5,})",
    ):
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match is not None:
            return match.group(1)
    return ""


def _parse_date(value: str | None) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    match = _DATE_ISO_RE.search(text)
    if match is not None:
        year = int(match.group("y"))
        month = int(match.group("m"))
        day = int(match.group("d"))
        try:
            return datetime(year, month, day, tzinfo=UTC).date().isoformat()
        except ValueError:
            return match.group(0)
    match = _DATE_MDY_SLASH_RE.search(text)
    if match is not None:
        year = int(match.group("y"))
        month = int(match.group("m"))
        day = int(match.group("d"))
        try:
            return datetime(year, month, day, tzinfo=UTC).date().isoformat()
        except ValueError:
            return match.group(0)
    match = _DATE_MDY_TEXT_RE.search(text)
    if match is not None:
        month_name = match.group("month").rstrip(".")
        try:
            month = datetime.strptime(month_name[:3], "%b").month
            day = int(match.group("d"))
            year = int(match.group("y"))
            return datetime(year, month, day, tzinfo=UTC).date().isoformat()
        except Exception:  # noqa: BLE001
            return match.group(0)
    return ""


def _datetime_from_date(value: str) -> datetime | None:
    date_text = _parse_date(value)
    if not date_text:
        return None
    try:
        parsed = datetime.fromisoformat(date_text)
    except ValueError:
        return None
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC)


def _duration_seconds(value: str | None) -> int | None:
    text = _clean_text(value)
    if not text:
        return None

    match = _ISO_DURATION_RE.match(text)
    if match is not None:
        hours = float(match.group("h") or 0)
        minutes = float(match.group("m") or 0)
        seconds = float(match.group("s") or 0)
        total = int(hours * 3600 + minutes * 60 + seconds)
        return total if total > 0 else None

    match = _TIME_RE.search(text)
    if match is not None:
        hours = int(match.group("h") or 0)
        minutes = int(match.group("m") or 0)
        seconds = int(match.group("s"))
        total = hours * 3600 + minutes * 60 + seconds
        return total if total > 0 else None

    lowered = text.casefold()
    if not any(unit in lowered for unit in ("hour", "hr", "minute", "min", "second", "sec")):
        return None
    hours_match = _HOURS_RE.search(text)
    minutes_match = _MINUTES_RE.search(text)
    seconds_match = _SECONDS_RE.search(text)
    hours = float(hours_match.group(1)) if hours_match is not None else 0
    minutes = float(minutes_match.group(1)) if minutes_match is not None else 0
    seconds = float(seconds_match.group(1)) if seconds_match is not None else 0
    total = int(hours * 3600 + minutes * 60 + seconds)
    return total if total > 0 else None


def _meta_values(root: HtmlElement, *names: str) -> list[str]:
    wanted = {_fold(name) for name in names}
    values: list[str] = []
    for meta in root.xpath("//meta[@name or @property or @itemprop]"):
        key = _fold(meta.get("name") or meta.get("property") or meta.get("itemprop"))
        if key not in wanted:
            continue
        value = _clean_text(meta.get("content"))
        if value:
            values.append(value)
    return _dedupe(values)


def _first_meta(root: HtmlElement, *names: str) -> str:
    values = _meta_values(root, *names)
    return values[0] if values else ""


def _first_xpath_text(node: HtmlElement, xpath: str) -> str:
    raw = node.xpath(xpath)
    if isinstance(raw, str):
        return _clean_text(raw)
    values = [_clean_text(str(value)) for value in raw if _clean_text(str(value))]
    return values[0] if values else ""


def _label_value(root: HtmlElement, *labels: str) -> str:
    wanted = {_fold(label).rstrip(":") for label in labels}
    for node in root.xpath(
        ".//*[self::dt or self::th or self::strong or self::b "
        "or contains(translate(@class, 'LABEL', 'label'), 'label')]"
    ):
        label_text = _fold(_node_text(node)).rstrip(":")
        if label_text not in wanted:
            continue
        if node.tag.casefold() == "dt":
            candidates = node.xpath("./following-sibling::dd[1]")
        elif node.tag.casefold() == "th":
            candidates = node.xpath("./following-sibling::td[1]")
        else:
            candidates = node.xpath("./following-sibling::*[1]")
        for candidate in candidates:
            value = _node_text(candidate)
            if value and _fold(value).rstrip(":") not in wanted:
                return value
        parent = node.getparent()
        if parent is not None:
            parent_text = _node_text(parent)
            raw_label = _node_text(node)
            if parent_text and raw_label and parent_text != raw_label:
                value = parent_text.replace(raw_label, "", 1).strip(" :-")
                if value:
                    return value

    lines = [
        _clean_text(str(value))
        for value in root.xpath(".//text()")
        if _clean_text(str(value))
    ]
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


def _result_container(anchor: HtmlElement) -> HtmlElement:
    best: HtmlElement = anchor
    for node in anchor.iterancestors():
        tag = str(node.tag).casefold()
        classes = _fold(f"{node.get('class') or ''} {node.get('id') or ''}")
        if tag in {"article", "li"} or any(
            token in classes
            for token in ("result", "program", "video", "card", "search-item", "listing")
        ):
            if len(_node_text(node)) >= 20:
                return node
            best = node
        if tag == "body":
            break
    return best


def _clean_title(value: str) -> str:
    title = _clean_text(value)
    title = re.sub(r"\s*\|\s*C-SPAN\.org\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+Video\s*\|\s*C-SPAN\.org\s*$", "", title, flags=re.IGNORECASE)
    return title.strip(" -")


def _card_title(card: HtmlElement, anchor: HtmlElement) -> str:
    for value in (
        _first_xpath_text(card, "string((.//*[self::h1 or self::h2 or self::h3])[1])"),
        _clean_text(anchor.get("aria-label")),
        _node_text(anchor),
    ):
        title = _clean_title(value)
        if title and _fold(title) not in _ACTION_TEXT and not title.isdigit():
            return title
    return ""


def _card_snippet(card: HtmlElement, title: str) -> str:
    text = _node_text(card)
    if title and text.startswith(title):
        text = text[len(title) :].strip(" :-")
    return text[:800]


def _card_metadata(card: HtmlElement, url: str) -> dict[str, Any]:
    text = _node_text(card)
    air_date = _parse_date(_label_value(card, "Date", "Aired", "Air Date") or text)
    duration = _duration_seconds(_label_value(card, "Duration", "Length") or text)
    program_id = _program_id_from_url(url) or _program_id_from_text(text)
    return {
        "program_id": program_id,
        "air_date": air_date,
        "duration_seconds": duration,
        "video_url": url,
    }


def _parse_search_html(
    raw_html: str,
    *,
    base_url: str,
    max_results: int,
) -> list[SearchResult]:
    root = _parse_html(raw_html)
    results: list[SearchResult] = []
    seen: set[str] = set()
    for anchor in root.xpath("//a[@href]"):
        href = anchor.get("href")
        url = _absolute_url(base_url, href)
        if not _is_video_url(url):
            continue
        if url in seen:
            continue
        card = _result_container(anchor)
        title = _card_title(card, anchor)
        if not title:
            continue
        seen.add(url)
        metadata = _card_metadata(card, url)
        results.append(
            SearchResult(
                url=url,
                title=title,
                snippet=_card_snippet(card, title),
                published_at=_datetime_from_date(str(metadata.get("air_date") or "")),
                source_kind=KIND,
                extras=metadata,
            )
        )
        if len(results) >= max_results:
            break
    return results


def _looks_like_empty_search(raw_html: str) -> bool:
    text = _fold(_node_text(_parse_html(raw_html)))
    return any(
        marker in text
        for marker in (
            "no results found",
            "no results",
            "0 results",
            "your search returned no",
            "could not find any videos",
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
            logger.debug("cspan diagnostic screenshot failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("cspan diagnostic dump failed: %s", exc)


async def _settle_page(page: Any) -> None:
    if not hasattr(page, "wait_for_load_state"):
        return
    try:
        await page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:  # noqa: BLE001
        pass


async def _reveal_transcript(page: Any) -> None:
    if not hasattr(page, "get_by_text"):
        return
    for label in (
        "Transcript",
        "Transcript type",
        "Show Transcript",
        "View Transcript",
        "Show Full Text",
    ):
        try:
            locator = page.get_by_text(label, exact=False)
            await locator.first.click(timeout=1_000)
            await _settle_page(page)
        except Exception:  # noqa: BLE001
            continue


async def search(
    query: str,
    *,
    max_results: int = 20,
    type: str | None = None,  # noqa: A002
) -> list[SearchResult]:
    """Search C-SPAN Video Library result cards through Playwright."""
    q = query.strip()
    if not q or max_results <= 0:
        return []
    search_url = build_search_url(q, type=type)

    try:
        async with browser.browser_session() as ctx:
            page = await ctx.new_page()
            try:
                await browser.navigate(page, search_url)
                await _settle_page(page)
                raw_html = await page.content()
                results = _parse_search_html(
                    raw_html,
                    base_url=search_url,
                    max_results=max_results,
                )
                if results or _looks_like_empty_search(raw_html):
                    return results
                logger.warning(
                    "cspan search selector drift: no video cards parsed from %s",
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
        logger.warning("cspan search playwright error: %s", exc)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("cspan search unexpected error: %s", exc)
        return []


def _json_values_from_scripts(root: HtmlElement) -> list[Any]:
    values: list[Any] = []
    for script in root.xpath("//script"):
        raw = _clean_text(script.text_content())
        if not raw:
            continue
        if not ("{" in raw or "[" in raw):
            continue
        candidates = [raw]
        for match in re.finditer(r"({.*?}|\[.*?\])", raw, flags=re.DOTALL):
            candidates.append(match.group(1))
        for candidate in candidates:
            try:
                values.append(json.loads(candidate))
                break
            except json.JSONDecodeError:
                continue
    return values


def _segment_from_mapping(
    value: dict[str, Any],
    *,
    in_transcript: bool,
) -> _TranscriptSegment | None:
    lowered = {str(k).casefold(): v for k, v in value.items()}
    text_value = (
        lowered.get("text")
        or lowered.get("caption")
        or lowered.get("transcript")
        or lowered.get("content")
    )
    if not isinstance(text_value, str):
        return None
    has_context = in_transcript or any(
        key in lowered
        for key in (
            "speaker",
            "speakername",
            "time",
            "timestart",
            "time_start",
            "start",
            "starttime",
            "timestamp",
        )
    )
    if not has_context:
        return None
    text = _clean_text(text_value)
    if not text or text.casefold() in {"transcript", "copy"}:
        return None
    speaker = _clean_text(
        str(
            lowered.get("speaker")
            or lowered.get("speakername")
            or lowered.get("person")
            or lowered.get("name")
            or ""
        )
    )
    raw_time = lowered.get("timestart") or lowered.get("time_start") or lowered.get("time")
    raw_time = (
        raw_time
        or lowered.get("starttime")
        or lowered.get("start")
        or lowered.get("timestamp")
    )
    time_start = _clean_text(str(raw_time or ""))
    return _TranscriptSegment(time_start=time_start, speaker=speaker, text=text)


def _segments_from_json(value: Any, *, in_transcript: bool = False) -> list[_TranscriptSegment]:
    segments: list[_TranscriptSegment] = []
    if isinstance(value, dict):
        segment = _segment_from_mapping(value, in_transcript=in_transcript)
        if segment is not None:
            segments.append(segment)
        for key, child in value.items():
            child_in_transcript = in_transcript or bool(_TRANSCRIPT_KEY_RE.search(str(key)))
            segments.extend(_segments_from_json(child, in_transcript=child_in_transcript))
    elif isinstance(value, list):
        for child in value:
            segments.extend(_segments_from_json(child, in_transcript=in_transcript))
    return segments


def _segment_from_text_node(node: HtmlElement) -> _TranscriptSegment | None:
    text = _node_text(node)
    if not text or _fold(text) in {"transcript", "copy"}:
        return None
    time_start = ""
    match = _TIME_RE.search(text)
    if match is not None:
        time_start = match.group(0)
    speaker = _first_xpath_text(
        node,
        "string((.//*[self::strong or self::b or contains(translate(@class, "
        "'SPEAKER', 'speaker'), 'speaker')])[1])",
    )
    body = text
    if time_start:
        body = body.replace(time_start, "", 1).strip(" :-")
    if speaker:
        body = body.replace(speaker, "", 1).strip(" :-")
    if not body:
        return None
    return _TranscriptSegment(time_start=time_start, speaker=speaker, text=body)


def _segments_from_transcript_dom(root: HtmlElement) -> list[_TranscriptSegment]:
    segments: list[_TranscriptSegment] = []
    for row in root.xpath(
        "//*[contains(translate(@id, 'TRANSCRIPT', 'transcript'), 'transcript') "
        "or contains(translate(@class, 'TRANSCRIPT', 'transcript'), 'transcript')]"
        "//tr"
    ):
        cells = row.xpath("./th|./td")
        if not cells:
            continue
        time_start = ""
        speaker = ""
        text_parts: list[str] = []
        for cell in cells:
            cell_text = _node_text(cell)
            if not time_start:
                match = _TIME_RE.search(cell_text)
                if match is not None:
                    time_start = match.group(0)
                    cell_text = cell_text.replace(time_start, "", 1).strip(" :-")
            if not speaker:
                speaker = _first_xpath_text(cell, "string((.//*[self::strong or self::b])[1])")
                if speaker:
                    cell_text = cell_text.replace(speaker, "", 1).strip(" :-")
            if cell_text:
                text_parts.append(cell_text)
        text = _clean_text(" ".join(text_parts))
        if text:
            segments.append(
                _TranscriptSegment(time_start=time_start, speaker=speaker, text=text)
            )

    for node in root.xpath(
        "//*[contains(translate(@id, 'TRANSCRIPT', 'transcript'), 'transcript') "
        "or contains(translate(@class, 'TRANSCRIPT', 'transcript'), 'transcript')]"
        "//*[self::p or self::li or self::div][not(.//tr) and not(ancestor::tr)]"
    ):
        segment = _segment_from_text_node(node)
        if segment is not None:
            segments.append(segment)
    return segments


def _text_lines(root: HtmlElement) -> list[str]:
    return [
        _clean_text(str(value))
        for value in root.xpath("//body//text()")
        if _clean_text(str(value))
    ]


def _is_time_line(line: str) -> bool:
    return _TIME_LINE_RE.match(line) is not None


def _is_transcript_ui_line(line: str) -> bool:
    folded = _fold(line)
    return (
        folded in _TRANSCRIPT_UI_TEXT
        or bool(_TRANSCRIPT_NOTE_RE.search(line))
        or bool(_TRANSCRIPT_BOUNDARY_RE.search(line))
    )


def _is_transcript_boundary_line(line: str) -> bool:
    return _TRANSCRIPT_BOUNDARY_RE.search(line) is not None


def _looks_like_speaker(line: str, upcoming: list[str]) -> bool:
    text = _clean_text(line)
    if not text or _is_time_line(text) or _is_transcript_ui_line(text):
        return False
    if text.startswith((">>", "*")):
        return False
    if len(text) > 100 or len(text.split()) > 12:
        return False
    next_text = next(
        (
            candidate
            for candidate in upcoming
            if candidate and not _is_transcript_ui_line(candidate)
        ),
        "",
    )
    return bool(next_text) and not _is_time_line(next_text)


def _split_inline_speaker_text(value: str) -> tuple[str, str]:
    text = _clean_text(value)
    if not text:
        return "", ""
    if ">>" in text:
        speaker, body = text.split(">>", 1)
        return _clean_text(speaker), f">> {_clean_text(body)}"
    return "", text


def _segments_from_timecoded_text(root: HtmlElement) -> list[_TranscriptSegment]:
    """Fallback for live C-SPAN pages whose transcript rows are not classed as transcript."""
    lines = _text_lines(root)
    segments: list[_TranscriptSegment] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _TIME_LINE_RE.match(line)
        if match is None:
            index += 1
            continue

        time_start = match.group("time")
        speaker = ""
        text_parts: list[str] = []
        inline_rest = _clean_text(match.group("rest"))
        if inline_rest and not _is_transcript_ui_line(inline_rest):
            inline_speaker, inline_text = _split_inline_speaker_text(inline_rest)
            speaker = inline_speaker
            if inline_text:
                text_parts.append(inline_text)

        cursor = index + 1
        while cursor < len(lines) and _is_transcript_ui_line(lines[cursor]):
            cursor += 1

        if not speaker and cursor < len(lines):
            upcoming = lines[cursor + 1 : cursor + 4]
            if _looks_like_speaker(lines[cursor], upcoming):
                speaker = lines[cursor]
                cursor += 1

        while cursor < len(lines):
            candidate = lines[cursor]
            if _is_time_line(candidate):
                break
            if _is_transcript_boundary_line(candidate):
                break
            if not _is_transcript_ui_line(candidate):
                if not speaker or _fold(candidate) != _fold(speaker):
                    text_parts.append(candidate)
            cursor += 1

        text = _clean_text(" ".join(text_parts))
        if text and _fold(text) not in _ACTION_TEXT:
            segments.append(
                _TranscriptSegment(
                    time_start=time_start,
                    speaker=speaker,
                    text=text,
                )
            )
        index = max(cursor, index + 1)

    return segments


def _dedupe_segments(segments: Iterable[_TranscriptSegment]) -> list[_TranscriptSegment]:
    out: list[_TranscriptSegment] = []
    seen: set[tuple[str, str, str]] = set()
    for segment in segments:
        text = _clean_text(segment.text)
        if not text:
            continue
        normalized = _TranscriptSegment(
            time_start=_clean_text(segment.time_start),
            speaker=_clean_text(segment.speaker),
            text=text,
        )
        key = (
            normalized.time_start.casefold(),
            normalized.speaker.casefold(),
            normalized.text.casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return out


def _extract_transcript_segments(root: HtmlElement) -> list[_TranscriptSegment]:
    segments: list[_TranscriptSegment] = []
    for value in _json_values_from_scripts(root):
        segments.extend(_segments_from_json(value))
    segments.extend(_segments_from_transcript_dom(root))
    segments.extend(_segments_from_timecoded_text(root))
    return _dedupe_segments(segments)


def _source_title(root: HtmlElement) -> str:
    for value in (
        _first_meta(root, "og:title", "twitter:title", "title"),
        _first_xpath_text(root, "string((//h1)[1])"),
        _first_xpath_text(root, "string((//h2)[1])"),
        _first_xpath_text(root, "string((//title)[1])"),
    ):
        title = _clean_title(value)
        if title and _fold(title) not in _ACTION_TEXT:
            return title
    return ""


def _canonical_url(root: HtmlElement, fallback: str) -> str:
    href = _first_xpath_text(root, "string((//link[@rel='canonical']/@href)[1])")
    if href:
        absolute = _absolute_url(fallback, href)
        if _is_cspan_url(absolute):
            return absolute
    return fallback


def _video_url(root: HtmlElement, canonical_url: str) -> str:
    for value in (
        _first_meta(root, "og:video", "og:video:url", "twitter:player"),
        _first_xpath_text(root, "string((//video//source/@src)[1])"),
        _first_xpath_text(root, "string((//video/@src)[1])"),
    ):
        url = _absolute_url(canonical_url, value)
        if url:
            return url
    return canonical_url


def _speakers_from_people_sections(root: HtmlElement) -> list[str]:
    names: list[str] = []
    heading_xpath = (
        "//*[self::h2 or self::h3 or self::h4 or self::h5]"
        "[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
        "'people in this video') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz'), 'speakers') or contains(translate(., "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'participants')]"
    )
    for heading in root.xpath(heading_xpath):
        for sibling in heading.itersiblings():
            tag = str(sibling.tag).casefold()
            if tag in {"h1", "h2", "h3", "h4", "h5"}:
                break
            for anchor in sibling.xpath(".//a"):
                href = _clean_text(anchor.get("href"))
                text = _node_text(anchor)
                if text and ("/person/" in href or len(text.split()) <= 5):
                    names.append(text)
            for item in sibling.xpath(".//li"):
                text = _node_text(item)
                if text:
                    names.append(text)
    if not names:
        for anchor in root.xpath("//a[contains(@href, '/person/')]"):
            text = _node_text(anchor)
            if text:
                names.append(text)
    return _dedupe(names)


def _description(root: HtmlElement) -> str:
    return _clean_text(
        _first_meta(root, "description", "og:description", "twitter:description")
        or _first_xpath_text(root, "string((//*[contains(@class, 'program_desc')])[1])")
    )


def _source_metadata(
    root: HtmlElement,
    *,
    canonical_url: str,
    raw_html: str,
    segments: list[_TranscriptSegment],
) -> dict[str, Any]:
    text = _node_text(root)
    program_id = (
        _program_id_from_url(canonical_url)
        or _program_id_from_text(raw_html)
        or _program_id_from_text(text)
    )
    air_date = _parse_date(
        _first_meta(root, "datePublished", "uploadDate", "article:published_time")
        or _label_value(root, "Aired", "Air Date", "Date")
        or text
    )
    duration = _duration_seconds(
        _first_meta(root, "duration")
        or _label_value(root, "Duration", "Length")
        or text
    )
    speakers = _speakers_from_people_sections(root)
    if not speakers:
        speakers = _dedupe(segment.speaker for segment in segments if segment.speaker)
    return {
        "program_id": program_id,
        "air_date": air_date,
        "duration_seconds": duration,
        "video_url": _video_url(root, canonical_url),
        "speakers": speakers,
    }


def _source_markdown(
    title: str,
    metadata: dict[str, Any],
    *,
    description: str,
    segments: list[_TranscriptSegment],
) -> str:
    lines = [f"# {title}", ""]
    for label, key in (
        ("Program ID", "program_id"),
        ("Air date", "air_date"),
        ("Duration seconds", "duration_seconds"),
        ("Video URL", "video_url"),
    ):
        value = metadata.get(key)
        if value not in (None, "", []):
            lines.append(f"- {label}: {value}")
    speakers = metadata.get("speakers")
    if isinstance(speakers, list) and speakers:
        lines.append(f"- Speakers: {'; '.join(str(speaker) for speaker in speakers)}")
    if description:
        lines.extend(["", "## Summary", "", description])
    lines.extend(["", "## Transcript", ""])
    if not segments:
        lines.append(
            "No transcript text was available on the C-SPAN page at fetch time."
        )
        return "\n".join(lines).strip()
    for segment in segments:
        prefix_parts: list[str] = []
        if segment.time_start:
            prefix_parts.append(f"[{segment.time_start}]")
        if segment.speaker:
            prefix_parts.append(f"{segment.speaker}:")
        prefix = " ".join(prefix_parts)
        if prefix:
            lines.append(f"{prefix} {segment.text}")
        else:
            lines.append(segment.text)
    return "\n".join(lines).strip()


def _parse_source_html(raw_html: str, *, url: str) -> Source | None:
    root = _parse_html(raw_html)
    title = _source_title(root)
    canonical_url = _canonical_url(root, url)
    segments = _extract_transcript_segments(root)
    metadata = _source_metadata(
        root,
        canonical_url=canonical_url,
        raw_html=raw_html,
        segments=segments,
    )
    cleaned_text = _source_markdown(
        title,
        metadata,
        description=_description(root),
        segments=segments,
    )
    if not title or not cleaned_text:
        return None
    return Source(
        url=canonical_url,
        title=title,
        cleaned_text=cleaned_text,
        raw_html=raw_html,
        fetched_at=datetime.now(UTC),
        source_kind=KIND,
        metadata=metadata,
    )


async def fetch(url: str) -> Source | None:
    """Fetch a C-SPAN program/clip page and return metadata plus transcript text."""
    if not url or not _is_video_url(url):
        return None

    try:
        async with browser.browser_session() as ctx:
            page = await ctx.new_page()
            try:
                await browser.navigate(page, url)
                await _settle_page(page)
                await _reveal_transcript(page)
                raw_html = await page.content()
                source = _parse_source_html(raw_html, url=url)
                if source is not None:
                    return source
                logger.warning("cspan fetch selector drift: no source parsed from %s", url)
                await _save_diagnostic_dump(page, "fetch-selector-drift", html=raw_html)
                return None
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
    except playwright.async_api.Error as exc:
        logger.warning("cspan fetch playwright error for %s: %s", url, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("cspan fetch unexpected error for %s: %s", url, exc)
        return None


def reset_for_tests() -> None:
    """Re-register host rates after browser.reset_for_tests(). Test-only."""
    _register_host_rates()


class _PayloadSchema(_BaseSearchPayload):
    max_results: int | None = None
    type: str | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=tuple(sorted(_ACCEPTED_HOSTS)),
    skill_name="cspan",
    description=(
        "C-SPAN Video Library US political broadcast video with transcripts "
        "(Playwright scrape, no auth)"
    ),
    optional_payload_knobs="`max_results`, `type=House\\|Senate`",
    example_query="Project 2025",
    module_name="cspan",
)


__all__ = [
    "KIND",
    "build_search_url",
    "fetch",
    "reset_for_tests",
    "search",
]
