"""News connector (issue #20): RSS first, Playwright scrape fallback.

No paid news APIs. Public RSS feeds cover most outlets; for sites that don't
publish a feed (or paywall the feed), a per-source CSS recipe drives a
Playwright scrape via :mod:`research_agent.tools.browser`.

Public surface:

* ``async def search(query, since=None, bundle=None)`` — aggregate hits across
  every configured source in ``bundle`` (or all bundles when ``bundle`` is
  ``None``). Each :class:`SearchResult` carries ``extras['fetched_via']``
  (``"rss"`` or ``"scrape"``) and a stable ``extras['source_label']`` so the
  smoke wrapper can report which sources contributed.

Fail-soft per source: a single feed timing out, returning HTTP 5xx, or having
its scrape selector drift logs a warning and contributes ``[]`` — every other
source still runs.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
import playwright.async_api
import yaml  # type: ignore[import-untyped]

from research_agent.tools import browser
from research_agent.tools.models import SearchResult

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path("config/sources.yaml")
_DIAGNOSTICS_DIR = Path("data/diagnostics/news")
_PER_FEED_TIMEOUT = 10.0
_DEFAULT_SINCE_DAYS = 7

_DEFAULT_USER_AGENT = "research-agent/0.1"

_config_cache: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _load_config() -> dict[str, Any]:
    """Read ``config/sources.yaml`` once and cache the parsed dict."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if not _CONFIG_PATH.exists():
        _config_cache = {}
        return _config_cache
    raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    _config_cache = raw if isinstance(raw, dict) else {}
    return _config_cache


def _select_bundles(bundle: str | None) -> dict[str, dict[str, Any]]:
    cfg = _load_config()
    news_cfg = cfg.get("news") or {}
    if not isinstance(news_cfg, dict):
        return {}
    if bundle is None:
        return {k: v for k, v in news_cfg.items() if isinstance(v, dict)}
    selected = news_cfg.get(bundle)
    if not isinstance(selected, dict):
        return {}
    return {bundle: selected}


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------


def _entry_published(entry: Any) -> datetime | None:
    """Return a tz-aware UTC datetime for a feedparser entry, or None."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        struct = entry.get(key) if hasattr(entry, "get") else None
        if struct:
            try:
                year, month, day, hour, minute, second = struct[:6]
                return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
            except (TypeError, ValueError):
                continue
    return None


def _matches_query(query: str, title: str, snippet: str) -> bool:
    if not query or not query.strip():
        return True
    needle = query.lower()
    haystack = f"{title} {snippet}".lower()
    return needle in haystack


def _build_rss_result(entry: Any, *, feed_url: str, source_label: str) -> SearchResult | None:
    link = (entry.get("link") or "").strip() if hasattr(entry, "get") else ""
    title = (entry.get("title") or "").strip() if hasattr(entry, "get") else ""
    if not link or not title:
        return None
    snippet = (entry.get("summary") or "").strip() if hasattr(entry, "get") else ""
    return SearchResult(
        url=link,
        title=title,
        snippet=snippet,
        published_at=_entry_published(entry),
        source_kind="news",
        extras={
            "feed_url": feed_url,
            "fetched_via": "rss",
            "source_label": source_label,
        },
    )


async def _fetch_rss(feed_url: str, *, query: str, since: datetime) -> list[SearchResult]:
    headers = {"User-Agent": _DEFAULT_USER_AGENT}
    source_label = urlparse(feed_url).netloc or feed_url
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=_PER_FEED_TIMEOUT,
        headers=headers,
    ) as client:
        response = await client.get(feed_url)
    if response.status_code >= 400:
        logger.warning("news rss %s returned HTTP %s", feed_url, response.status_code)
        return []

    parsed = await asyncio.to_thread(feedparser.parse, response.content)
    out: list[SearchResult] = []
    for entry in parsed.entries or []:
        result = _build_rss_result(entry, feed_url=feed_url, source_label=source_label)
        if result is None:
            continue
        if result.published_at is not None and result.published_at < since:
            continue
        if not _matches_query(query, result.title, result.snippet):
            continue
        out.append(result)
    return out


# ---------------------------------------------------------------------------
# Scrape (Playwright)
# ---------------------------------------------------------------------------


async def _safe_inner_text(locator: Any) -> str:
    try:
        text = await locator.inner_text()
    except Exception:  # noqa: BLE001 — selector miss should not raise
        return ""
    return (text or "").strip()


async def _safe_attr(locator: Any, name: str) -> str:
    try:
        value = await locator.get_attribute(name)
    except Exception:  # noqa: BLE001
        return ""
    return (value or "").strip()


async def _save_diagnostic_screenshot(page: Any, host: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = _DIAGNOSTICS_DIR / f"{host}-{stamp}.png"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(target))
    except Exception as exc:  # noqa: BLE001
        logger.debug("news scrape diagnostic screenshot failed: %s", exc)


async def _fetch_scrape(
    recipe: dict[str, Any], *, query: str, since: datetime
) -> list[SearchResult]:
    index_url = recipe.get("index_url")
    item_selector = recipe.get("item_selector")
    title_selector = recipe.get("title_selector")
    link_selector = recipe.get("link_selector")
    summary_selector = recipe.get("summary_selector")
    if not (index_url and item_selector and title_selector and link_selector):
        logger.warning("news scrape recipe missing required keys: %s", recipe)
        return []

    source_label = recipe.get("name") or urlparse(index_url).netloc or index_url
    host = urlparse(index_url).netloc or "unknown"

    async with browser.browser_session() as ctx:
        page = await ctx.new_page()
        try:
            await browser.navigate(page, index_url)
            try:
                items = await page.locator(item_selector).all()
            except Exception as exc:
                logger.warning("news scrape selector miss on %s: %s", index_url, exc)
                await _save_diagnostic_screenshot(page, host)
                raise

            results: list[SearchResult] = []
            for item in items:
                title = await _safe_inner_text(item.locator(title_selector).first)
                href = await _safe_attr(item.locator(link_selector).first, "href")
                snippet = ""
                if summary_selector:
                    snippet = await _safe_inner_text(item.locator(summary_selector).first)
                if not title or not href:
                    continue
                url = urljoin(index_url, href)
                if not _matches_query(query, title, snippet):
                    continue
                results.append(
                    SearchResult(
                        url=url,
                        title=title,
                        snippet=snippet,
                        published_at=None,
                        source_kind="news",
                        extras={
                            "index_url": index_url,
                            "fetched_via": "scrape",
                            "source_label": source_label,
                        },
                    )
                )
            # Apply ``since`` filter only when a date is available; scrape recipes
            # without date selectors pass through (caller can rerank by recency
            # later).
            return results
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Source dispatch
# ---------------------------------------------------------------------------


async def _run_rss_source(feed_url: str, *, query: str, since: datetime) -> list[SearchResult]:
    try:
        return await asyncio.wait_for(
            _fetch_rss(feed_url, query=query, since=since),
            timeout=_PER_FEED_TIMEOUT,
        )
    except TimeoutError:
        logger.warning("news rss %s timed out after %ss", feed_url, _PER_FEED_TIMEOUT)
        return []
    except httpx.HTTPError as exc:
        logger.warning("news rss %s failed: %s", feed_url, exc)
        return []
    except Exception as exc:  # noqa: BLE001 — never let one feed kill the run
        logger.warning("news rss %s unexpected error: %s", feed_url, exc)
        return []


async def _run_scrape_source(
    recipe: dict[str, Any], *, query: str, since: datetime
) -> list[SearchResult]:
    label = recipe.get("name") or recipe.get("index_url") or "<unknown>"
    try:
        return await _fetch_scrape(recipe, query=query, since=since)
    except playwright.async_api.Error as exc:
        logger.warning("news scrape %s playwright error: %s", label, exc)
        return []
    except (TimeoutError, KeyError) as exc:
        logger.warning("news scrape %s failed: %s", label, exc)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("news scrape %s unexpected error: %s", label, exc)
        return []


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


async def search(
    query: str,
    since: datetime | None = None,
    bundle: str | None = None,
) -> list[SearchResult]:
    """Aggregate news hits from every configured source.

    ``since`` defaults to seven days ago (UTC). ``bundle`` selects a single
    bundle from ``config/sources.yaml`` under the ``news:`` key; ``None`` runs
    every bundle.
    """
    if since is None:
        since = datetime.now(UTC) - timedelta(days=_DEFAULT_SINCE_DAYS)
    elif since.tzinfo is None:
        since = since.replace(tzinfo=UTC)

    bundles = _select_bundles(bundle)
    if not bundles:
        return []

    tasks: list[asyncio.Task[list[SearchResult]]] = []
    for cfg in bundles.values():
        for feed_url in cfg.get("rss") or []:
            if not isinstance(feed_url, str):
                continue
            tasks.append(asyncio.create_task(_run_rss_source(feed_url, query=query, since=since)))
        for recipe in cfg.get("scrape") or []:
            if not isinstance(recipe, dict):
                continue
            tasks.append(asyncio.create_task(_run_scrape_source(recipe, query=query, since=since)))

    if not tasks:
        return []

    grouped = await asyncio.gather(*tasks)
    results: list[SearchResult] = []
    for batch in grouped:
        results.extend(batch)
    return results


def reset_for_tests() -> None:
    """Drop the cached config so tests can re-load after monkeypatching paths."""
    global _config_cache
    _config_cache = None


__all__ = ["reset_for_tests", "search"]
