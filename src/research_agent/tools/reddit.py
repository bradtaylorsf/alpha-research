"""Reddit connector (issue #21): Playwright on old.reddit.com (no PRAW).

The legacy ``old.reddit.com`` UI is server-rendered with stable selectors,
so a polite Playwright scrape is materially friendlier than the modern SPA
and avoids the OAuth dance entirely. We deliberately read no Reddit-API
env vars (no client id, no client secret, no API user-agent) — there is
no API surface here.

Public surface:

* ``async def search(query, subreddit=None, sort='relevance', limit=25)`` —
  hits a search-results listing and returns up to ``limit`` :class:`SearchResult`.
* ``async def fetch(url)`` — navigates a post permalink and returns a
  :class:`Source` whose ``cleaned_text`` is the post body plus its depth-1
  comments (full trees are out of scope for v1).

Selector drift is the most likely failure mode — when ``query`` is
non-empty and we still come up with zero hits, we save a diagnostic
screenshot under ``data/diagnostics/reddit/`` and emit a single WARN so
operators notice. Any unexpected Playwright failure is also caught so
this connector cannot crash the daemon.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

from research_agent.tools import browser
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_DIAGNOSTICS_DIR = Path("data/diagnostics/reddit")
_HOST = "old.reddit.com"
_HOST_RPS = 0.5  # 1 nav per 2 seconds — politeness budget for old.reddit.com

# Register the per-host rate limit at import time so concurrent connectors
# share the same bucket through ``tools/browser.py``.
browser.set_host_rate(_HOST, _HOST_RPS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _safe_inner_text(locator) -> str:
    try:
        text = await locator.inner_text()
    except Exception:  # noqa: BLE001 — selector miss must not raise
        return ""
    return (text or "").strip()


async def _safe_attr(locator, name: str) -> str:
    try:
        value = await locator.get_attribute(name)
    except Exception:  # noqa: BLE001
        return ""
    return (value or "").strip()


async def _save_diagnostic_screenshot(page) -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = _DIAGNOSTICS_DIR / f"{stamp}.png"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(target))
    except Exception as exc:  # noqa: BLE001
        logger.debug("reddit diagnostic screenshot failed: %s", exc)


def _build_search_url(query: str, subreddit: str | None, sort: str) -> str:
    q = quote_plus(query or "")
    sort_q = quote_plus(sort or "relevance")
    if subreddit:
        sub = subreddit.lstrip("/").removeprefix("r/")
        return f"https://old.reddit.com/r/{sub}/search?q={q}&restrict_sr=on&sort={sort_q}"
    return f"https://old.reddit.com/search?q={q}&sort={sort_q}"


def _normalize_to_old(url: str) -> str:
    """Rewrite ``www.reddit.com`` / ``reddit.com`` permalinks to ``old.reddit.com``."""
    parsed = urlparse(url)
    if parsed.netloc in {"www.reddit.com", "reddit.com", "new.reddit.com"}:
        return parsed._replace(netloc="old.reddit.com").geturl()
    return url


def _parse_int(text: str) -> int | None:
    """Pull the leading integer out of strings like ``"42 points"`` / ``"3 comments"``."""
    cleaned = (text or "").strip().lower().replace(",", "")
    if not cleaned:
        return None
    head = cleaned.split()[0]
    try:
        return int(head)
    except ValueError:
        return None


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    # Reddit emits ISO-8601 like "2026-04-30T12:34:56+00:00"; ``fromisoformat``
    # handles that on 3.11+.
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


# Per-result selectors. Documented inline so future drift is a 5-minute fix:
# - ``div.search-result-link`` is the canonical container on old.reddit.com's
#   ``/search`` and ``/r/<sub>/search`` results.
# - ``div.thing.link`` is the listing variant used on plain subreddit listings
#   (e.g. ``/r/<sub>``); we treat it as a fallback so the same parser works
#   when a caller hands us a listing-style URL.
_RESULT_SELECTORS = ("div.search-result-link", "div.thing.link")


async def _extract_search_item(item, *, sort: str) -> SearchResult | None:
    title = await _safe_inner_text(item.locator("a.search-title").first)
    if not title:
        title = await _safe_inner_text(item.locator("a.title").first)
    href = await _safe_attr(item.locator("a.search-title").first, "href")
    if not href:
        href = await _safe_attr(item.locator("a.title").first, "href")
    if not title or not href:
        return None
    url = urljoin("https://old.reddit.com", href)

    snippet = await _safe_inner_text(item.locator(".search-result-body").first)
    if not snippet:
        snippet = await _safe_inner_text(item.locator(".md").first)

    subreddit = await _safe_inner_text(item.locator("a.search-subreddit-link").first)
    if not subreddit:
        subreddit = await _safe_attr(item, "data-subreddit")

    score_text = await _safe_inner_text(item.locator(".search-score").first)
    if not score_text:
        score_text = await _safe_inner_text(item.locator(".score.unvoted").first)
    score = _parse_int(score_text)

    comments_text = await _safe_inner_text(item.locator(".search-comments").first)
    if not comments_text:
        comments_text = await _safe_inner_text(item.locator("a.comments").first)
    num_comments = _parse_int(comments_text)

    posted_raw = await _safe_attr(item.locator("time").first, "datetime")
    posted_at = _parse_datetime(posted_raw)

    return SearchResult(
        url=url,
        title=title,
        snippet=snippet,
        published_at=posted_at,
        source_kind="reddit",
        extras={
            "subreddit": subreddit,
            "score": score,
            "num_comments": num_comments,
            "sort": sort,
            "fetched_via": "old.reddit.com",
        },
    )


async def search(
    query: str,
    subreddit: str | None = None,
    sort: str = "relevance",
    limit: int = 25,
) -> list[SearchResult]:
    """Search ``old.reddit.com`` and return up to ``limit`` :class:`SearchResult`.

    Builds a ``/search`` (or ``/r/<sub>/search``) URL, lets the shared
    Playwright session render it, and parses the result containers. On
    selector drift / unexpected failure we log a single WARN, save a
    diagnostic screenshot, and return ``[]`` — the daemon does not crash.
    """
    url = _build_search_url(query, subreddit, sort)
    try:
        async with browser.browser_session() as ctx:
            page = await ctx.new_page()
            try:
                await browser.navigate(page, url)

                items: list = []
                for selector in _RESULT_SELECTORS:
                    try:
                        found = await page.locator(selector).all()
                    except Exception:  # noqa: BLE001
                        found = []
                    if found:
                        items = found
                        break

                results: list[SearchResult] = []
                for item in items[:limit]:
                    try:
                        hit = await _extract_search_item(item, sort=sort)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("reddit search item parse failed: %s", exc)
                        continue
                    if hit is not None:
                        results.append(hit)

                if not results and (query or "").strip():
                    logger.warning(
                        "reddit search returned 0 results for %r — likely selector drift",
                        query,
                    )
                    await _save_diagnostic_screenshot(page)
                return results
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001 — never let the connector kill the daemon
        logger.warning("reddit search failed for %r: %s", query, exc)
        return []


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


async def _extract_post_body(page) -> str:
    text = await _safe_inner_text(page.locator(".expando .md").first)
    if text:
        return text
    return await _safe_inner_text(page.locator("div.usertext-body .md").first)


async def _extract_top_level_comments(page) -> tuple[list[str], int]:
    """Return ``(joined_comment_strings, count)`` for depth-1 comments only."""
    try:
        comments = await page.locator("div.commentarea > div.sitetable > div.thing.comment").all()
    except Exception:  # noqa: BLE001
        return [], 0

    joined: list[str] = []
    for comment in comments:
        # ``.morechildren`` is a placeholder Reddit injects for "load more";
        # it's a sibling thing without a real body — skip it.
        classes = await _safe_attr(comment, "class")
        if "morechildren" in classes:
            continue
        author = await _safe_inner_text(comment.locator("a.author").first)
        body = await _safe_inner_text(comment.locator(".usertext-body .md").first)
        if not body:
            continue
        joined.append(f"{author or 'Anonymous'}: {body}")
    return joined, len(joined)


async def fetch(url: str) -> Source | None:
    """Fetch a Reddit post permalink and return a :class:`Source`.

    The post body and depth-1 comments are concatenated into
    ``cleaned_text``; full comment trees are explicitly out of scope for v1.
    On any Playwright error we save a diagnostic screenshot and return None.
    """
    if not url:
        return None
    target = _normalize_to_old(url)
    try:
        async with browser.browser_session() as ctx:
            page = await ctx.new_page()
            try:
                await browser.navigate(page, target)

                title = await _safe_inner_text(page.locator("a.title").first)
                body = await _extract_post_body(page)
                comments, comment_count = await _extract_top_level_comments(page)

                subreddit = await _safe_inner_text(page.locator("a.subreddit").first)
                score_text = await _safe_inner_text(page.locator(".sitetable .score.unvoted").first)
                score = _parse_int(score_text)
                num_comments_text = await _safe_inner_text(
                    page.locator(".sitetable a.comments").first
                )
                num_comments = _parse_int(num_comments_text)

                cleaned_text = body
                if comments:
                    joined_comments = "\n\n".join(comments)
                    if cleaned_text:
                        cleaned_text = f"{cleaned_text}\n\n---\n{joined_comments}"
                    else:
                        cleaned_text = joined_comments

                if not title and not cleaned_text:
                    await _save_diagnostic_screenshot(page)
                    return None

                return Source(
                    url=target,
                    title=title or target,
                    cleaned_text=cleaned_text,
                    raw_html=None,
                    fetched_at=datetime.now(UTC),
                    source_kind="reddit",
                    metadata={
                        "subreddit": subreddit,
                        "score": score,
                        "num_comments": num_comments,
                        "comment_count": comment_count,
                    },
                )
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("reddit fetch failed for %s: %s", target, exc)
        return None


__all__ = ["fetch", "search"]
