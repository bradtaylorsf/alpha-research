"""Web search via headless DuckDuckGo / Google SERPs (no paid APIs).

Issue #14 — first browser-driven connector. DDG's ``html.duckduckgo.com``
SERP is the default because it doesn't aggressively bot-block; Google is
the fallback when DDG misses. Both go through the shared
:mod:`research_agent.tools.browser` so per-host rate limits stick.

Selectors live in inline constants near the parsers — when the SERP HTML
inevitably drifts, the screenshot fail-soft path drops a PNG under
``data/diagnostics/web_search/`` and logs a single WARN; the operator
fixes the constant.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, quote_plus

from playwright.async_api import Error as PlaywrightError

from research_agent.tools import browser
from research_agent.tools.models import SearchResult

logger = logging.getLogger(__name__)

Engine = Literal["ddg", "google"]

DDG_HOST = "html.duckduckgo.com"
GOOGLE_HOST = "www.google.com"
_SERP_RPS = 0.5  # 1 query per 2 seconds — public SERPs notice bursts.

DIAGNOSTICS_DIR = Path("data/diagnostics/web_search")


def _ensure_serp_rates() -> None:
    """Register conservative per-host rates for both SERPs.

    Idempotent — safe to call on every ``search()`` invocation. We re-apply
    on each call (rather than at module import) so a test that resets the
    shared bucket dict still gets the limits back.
    """
    browser.set_host_rate(DDG_HOST, _SERP_RPS)
    browser.set_host_rate(GOOGLE_HOST, _SERP_RPS)


_ensure_serp_rates()


# ---------------------------------------------------------------------------
# Parsers — pure functions over HTML so they can be unit-tested without
# Playwright. Keep selectors documented inline so future drift is repairable.
# ---------------------------------------------------------------------------


class _DDGParser(HTMLParser):
    """Extract result rows from html.duckduckgo.com.

    DDG HTML SERP shape (last verified 2026-05):
      <div class="result__body">
        <a class="result__a" href="/l/?uddg=<encoded url>">Title</a>
        <a class="result__snippet">Snippet text</a>
      </div>
    DDG wraps the destination URL in a redirect; we unwrap the ``uddg`` param.
    """

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._depth_in_body = 0
        self._current: dict[str, str] | None = None
        self._capture: str | None = None  # "title" | "snippet" | None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrd = {k: (v or "") for k, v in attrs}
        cls = attrd.get("class", "")
        if tag == "div" and "result__body" in cls.split():
            self._current = {"url": "", "title": "", "snippet": ""}
            self._depth_in_body = 1
            return
        if self._depth_in_body > 0 and tag == "div":
            self._depth_in_body += 1
        if self._current is None:
            return
        if tag == "a" and "result__a" in cls.split():
            href = attrd.get("href", "")
            self._current["url"] = _unwrap_ddg_href(href)
            self._capture = "title"
            self._buf = []
        elif tag == "a" and "result__snippet" in cls.split():
            self._capture = "snippet"
            self._buf = []
        elif self._capture == "snippet" and tag == "div" and "result__snippet" in cls.split():
            # Snippet is occasionally a <div>, not <a>.
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if self._capture == "title" and tag == "a":
            assert self._current is not None
            self._current["title"] = "".join(self._buf).strip()
            self._capture = None
            self._buf = []
            return
        if self._capture == "snippet" and tag in ("a", "div"):
            assert self._current is not None
            self._current["snippet"] = "".join(self._buf).strip()
            self._capture = None
            self._buf = []
            return
        if self._depth_in_body > 0 and tag == "div":
            self._depth_in_body -= 1
            if self._depth_in_body == 0 and self._current is not None:
                if self._current.get("url"):
                    self.results.append(self._current)
                self._current = None

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buf.append(data)


def _unwrap_ddg_href(href: str) -> str:
    """DDG wraps result links as ``/l/?uddg=<urlencoded>``; pull the inner URL out."""
    if not href:
        return ""
    if href.startswith("/l/") or href.startswith("//duckduckgo.com/l/"):
        # parse_qs handles the ``uddg`` param regardless of leading scheme.
        qs = href.split("?", 1)[1] if "?" in href else ""
        params = parse_qs(qs)
        uddg = params.get("uddg")
        if uddg:
            return uddg[0]
    return href


def _parse_ddg(html: str) -> list[dict[str, str]]:
    parser = _DDGParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — malformed HTML must never crash search
        return parser.results
    return [r for r in parser.results if r.get("url") and r.get("title")]


# Google SERP shape (last verified 2026-05): organic results contain a
# top-level anchor with an <h3> inside, e.g.:
#   <div class="g">
#     <a href="https://example.com/x"><h3>Title</h3></a>
#     <div class="VwiC3b">snippet</div>
#   </div>
# Skip:
#   - ad blocks (ancestor div with ``data-text-ad`` or
#     ``commercial-unit-desktop-top`` class),
#   - knowledge panels (``.ULSxyf`` / ``.kp-blk``),
#   - "people also ask" (``.related-question-pair``),
#   - ad redirector URLs (``/aclk?`` or ``/url?...adurl=``).
_GOOGLE_AD_CLASSES = frozenset(
    {
        "commercial-unit-desktop-top",
        "ULSxyf",
        "kp-blk",
        "related-question-pair",
    }
)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", html)).strip()


class _GoogleParser(HTMLParser):
    """Walk the DOM and collect organic anchor → h3 → snippet triples."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        # Ad-container nesting depth counter. Increment when entering a div
        # marked as ad/panel; decrement on its close. While >0, skip results.
        self._ad_stack: list[int] = []  # depth of div nesting at each "in-ad" entry
        self._div_depth = 0

        self._current_url: str | None = None
        self._capturing_title = False
        self._title_buf: list[str] = []
        # Snippet capture (.VwiC3b div); enabled after we successfully record
        # an organic result, captures next snippet.
        self._snippet_pending: bool = False
        self._capturing_snippet = False
        self._snippet_buf: list[str] = []
        self._snippet_div_depth = 0

    @staticmethod
    def _is_ad_container(attrd: dict[str, str]) -> bool:
        if "data-text-ad" in attrd:
            return True
        cls = set(attrd.get("class", "").split())
        return bool(cls & _GOOGLE_AD_CLASSES)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrd = {k: (v or "") for k, v in attrs}

        if tag == "div":
            self._div_depth += 1
            if self._is_ad_container(attrd):
                self._ad_stack.append(self._div_depth)
            if self._snippet_pending and "VwiC3b" in attrd.get("class", "").split():
                self._capturing_snippet = True
                self._snippet_div_depth = self._div_depth
                self._snippet_buf = []
                self._snippet_pending = False
            return

        if self._ad_stack:
            return  # Skip everything inside an ad/panel container.

        if tag == "a" and self._current_url is None:
            href = attrd.get("href", "")
            if not href.startswith(("http://", "https://")):
                return
            if "/aclk?" in href:
                return
            if "/url?" in href and "adurl=" in href:
                return
            if href.startswith(("https://www.google.com/", "https://accounts.google.com/")):
                return
            self._current_url = href
            return

        if tag == "h3" and self._current_url is not None:
            self._capturing_title = True
            self._title_buf = []

    def handle_endtag(self, tag: str) -> None:
        if self._capturing_snippet and tag == "div":
            # We may close inner divs; only stop when the snippet div itself closes.
            if self._div_depth == self._snippet_div_depth:
                snippet = _strip_tags("".join(self._snippet_buf))
                if self.results:
                    self.results[-1]["snippet"] = snippet
                self._capturing_snippet = False
                self._snippet_buf = []
            self._div_depth -= 1
            if self._ad_stack and self._ad_stack[-1] > self._div_depth:
                self._ad_stack.pop()
            return

        if tag == "div":
            if self._ad_stack and self._ad_stack[-1] == self._div_depth:
                self._ad_stack.pop()
            self._div_depth -= 1
            return

        if self._capturing_title and tag == "h3":
            title = _strip_tags("".join(self._title_buf))
            self._capturing_title = False
            self._title_buf = []
            if self._current_url and title:
                self.results.append({"url": self._current_url, "title": title, "snippet": ""})
                self._snippet_pending = True
            return

        if tag == "a":
            self._current_url = None
            self._capturing_title = False
            self._title_buf = []

    def handle_data(self, data: str) -> None:
        if self._capturing_title:
            self._title_buf.append(data)
        elif self._capturing_snippet:
            self._snippet_buf.append(data)


def _parse_google(html: str) -> list[dict[str, str]]:
    """Pull organic results from a Google SERP, skipping ads/knowledge panels."""
    parser = _GoogleParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — malformed HTML must never crash search
        return parser.results
    # Dedupe by URL while preserving order.
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for row in parser.results:
        if row["url"] in seen:
            continue
        seen.add(row["url"])
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def _capture_diagnostic(page, engine: str) -> Path:
    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = DIAGNOSTICS_DIR / f"{ts}-{engine}.png"
    try:
        await page.screenshot(path=str(path))
    except PlaywrightError:
        # If we can't even screenshot, write an empty marker so the path is
        # still surfaced in the WARN — the operator can grep for the engine.
        path.write_bytes(b"")
    return path


async def search(
    query: str,
    max_results: int = 10,
    engine: Engine = "ddg",
) -> list[SearchResult]:
    """Search ``query`` against ``engine`` and return up to ``max_results`` hits.

    ``score`` and ``published_at`` are intentionally left unset — public SERPs
    don't expose either consistently, so any value would be made up. Callers
    that need ranking can re-rank these later.
    """
    if not query.strip():
        return []

    _ensure_serp_rates()

    if engine == "ddg":
        url = f"https://{DDG_HOST}/html/?q={quote_plus(query)}"
        parser = _parse_ddg
    elif engine == "google":
        url = f"https://{GOOGLE_HOST}/search?q={quote_plus(query)}&hl=en"
        parser = _parse_google
    else:  # pragma: no cover — Literal type covers this
        raise ValueError(f"unknown engine: {engine!r}")

    async with browser.browser_session() as ctx:
        page = await ctx.new_page()
        try:
            try:
                await browser.navigate(page, url)
                html = await page.content()
            except PlaywrightError as exc:
                logger.warning("web_search %s navigation failed: %s", engine, exc)
                return []

            parsed = parser(html)

            if not parsed:
                screenshot = await _capture_diagnostic(page, engine)
                logger.warning(
                    "web_search %s returned 0 results for %r — selector drift? screenshot=%s",
                    engine,
                    query,
                    screenshot,
                )
                return []
        finally:
            await page.close()

    results: list[SearchResult] = []
    for row in parsed[:max_results]:
        results.append(
            SearchResult(
                url=row["url"],
                title=row["title"],
                snippet=row.get("snippet", ""),
                source_kind="web",
                extras={"source_engine": engine},
            )
        )
    return results


__all__ = ["DIAGNOSTICS_DIR", "search"]
