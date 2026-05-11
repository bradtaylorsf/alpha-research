"""Better Business Bureau profile lookup — Playwright-driven (issue #95).

Public surface:

* ``async def search(query, *, max_results=25) -> list[SearchResult]`` runs
  BBB's national search at ``bbb.org/search?find_country=USA&find_text=<name>``.
  Returns business name, rating (e.g. ``A+``, ``B-``, ``NR``), city/state
  location, and a profile permalink.
* ``async def fetch(url) -> Source | None`` opens a BBB business profile and
  returns markdown of: rating, accreditation status, complaint counts in the
  last 12 months / 3 years, complaint summary categories, and government
  actions.

BBB has no public API — the connector is Playwright-only.

Two regional caveats worth knowing before relying on results:

* BBB profiles are operated by regional bureaus, so a search by name without
  a city/state qualifier returns one row per state branch (e.g. "ACME, Inc"
  in CA and TX may both appear). Let the planner narrow with location terms.
* Complaint *bodies* on profile pages are gated behind a "Show more" / "Show
  full complaint" reveal — fetch() best-effort clicks every visible reveal
  control before scraping so the rolled-up markdown is not truncated.

No auth. Per-host rate gate at 0.5 RPS — BBB is public infrastructure and
Playwright traffic is conspicuous, so be polite.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import playwright.async_api

from research_agent.tools import browser
from research_agent.tools._registry import (
    BaseSearchPayload as _BaseSearchPayload,
    register_kind as _register_kind,
)
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_DIAGNOSTICS_DIR = Path("data/diagnostics/bbb")
_PER_HOST_RPS = 0.5
_HOST = "www.bbb.org"
# Accept both ``www.bbb.org`` and the bare ``bbb.org`` host so URLs that
# arrive from search results without the ``www.`` prefix still navigate;
# bbb.org 301s to www.bbb.org so Playwright will follow the redirect.
_ACCEPTED_HOSTS = frozenset({_HOST, "bbb.org"})
_SEARCH_URL = "https://www.bbb.org/search"

# Short per-call timeout for selector reads. Playwright's default 30s
# auto-wait is catastrophic on a profile page where most selectors may be
# absent — half a dozen missing selectors in fetch() would hang the
# connector for minutes.
_SELECTOR_READ_TIMEOUT_MS = 2_000

# Search page selectors — BBB's React app uses semantic data-testid
# attributes plus stable CSS classes. Card root, link, name, rating, and
# location elements are scraped per row.
_RESULT_CARD_SELECTOR = (
    "div[data-testid='search-result-card'], div.result-card, article.result-card"
)
_RESULT_NAME_SELECTOR = (
    "[data-testid='result-business-name'], h3.result-business-name, h3 a"
)
_RESULT_LINK_SELECTOR = (
    "a[data-testid='result-business-link'], a.result-business-link, h3 a"
)
_RESULT_RATING_SELECTOR = (
    "[data-testid='result-rating'], .result-rating, .bbb-rating"
)
_RESULT_LOCATION_SELECTOR = (
    "[data-testid='result-location'], .result-location, .business-address"
)

# Profile page selectors — header summary plus the four content blocks the
# AC enumerates. BBB's profile DOM is less stable than its search markup;
# we list a couple of fallbacks per field so a class rename does not blank
# the whole connector.
_PROFILE_RATING_SELECTOR = (
    "[data-testid='profile-rating'], .bbb-rating-letter, span.rating-letter"
)
_PROFILE_ACCREDITATION_SELECTOR = (
    "[data-testid='accreditation-status'], .accreditation-status, .bbb-accreditation"
)
_PROFILE_COMPLAINTS_12MO_SELECTOR = (
    "[data-testid='complaints-12mo'], .complaints-12-months"
)
_PROFILE_COMPLAINTS_3YR_SELECTOR = (
    "[data-testid='complaints-3yr'], .complaints-3-years"
)
_PROFILE_COMPLAINT_CATEGORIES_SELECTOR = (
    "[data-testid='complaint-category'], .complaint-summary-category li,"
    " .complaint-categories li"
)
_PROFILE_GOVERNMENT_ACTIONS_SELECTOR = (
    "[data-testid='government-actions'], .government-actions"
)

# "Show more" / "Show full complaint" reveal controls that gate complaint
# bodies. Click every visible one before scraping; misses are non-fatal.
_REVEAL_BUTTON_SELECTORS: tuple[str, ...] = (
    "button:has-text('Show more')",
    "button:has-text('Show full complaint')",
    "button:has-text('Read more')",
)


def _register_host_rates() -> None:
    browser.set_host_rate(_HOST, _PER_HOST_RPS)


_register_host_rates()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _safe_inner_text(locator: Any) -> str:
    try:
        text = await locator.inner_text(timeout=_SELECTOR_READ_TIMEOUT_MS)
    except TypeError:
        try:
            text = await locator.inner_text()
        except Exception:  # noqa: BLE001
            return ""
    except Exception:  # noqa: BLE001 — selector miss should not raise
        return ""
    return (text or "").strip()


async def _safe_attr(locator: Any, name: str) -> str:
    try:
        value = await locator.get_attribute(name, timeout=_SELECTOR_READ_TIMEOUT_MS)
    except TypeError:
        try:
            value = await locator.get_attribute(name)
        except Exception:  # noqa: BLE001
            return ""
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
        logger.debug("bbb diagnostic screenshot failed: %s", exc)


def _absolute_url(base: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        parsed = urlparse(base)
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    return base.rstrip("/") + "/" + href


async def _collect_simple_text(page: Any, selector: str) -> str:
    try:
        return await _safe_inner_text(page.locator(selector).first)
    except Exception as exc:  # noqa: BLE001
        logger.debug("bbb profile selector miss (%s): %s", selector, exc)
        return ""


async def _collect_list(page: Any, selector: str) -> list[str]:
    try:
        nodes = await page.locator(selector).all()
    except Exception as exc:  # noqa: BLE001
        logger.debug("bbb profile list selector miss (%s): %s", selector, exc)
        return []
    out: list[str] = []
    for node in nodes:
        text = await _safe_inner_text(node)
        if text:
            out.append(text)
    return out


async def _click_all_reveals(page: Any) -> int:
    """Best-effort: click every visible "Show more" / "Show full complaint"
    button so complaint bodies are expanded before scraping. Returns the
    number of successful clicks for diagnostics; misses are swallowed so a
    selector drift never blocks fetch().
    """
    clicks = 0
    for selector in _REVEAL_BUTTON_SELECTORS:
        try:
            buttons = await page.locator(selector).all()
        except Exception as exc:  # noqa: BLE001
            logger.debug("bbb reveal lookup miss (%s): %s", selector, exc)
            continue
        for button in buttons:
            try:
                await button.click()
                clicks += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("bbb reveal click miss (%s): %s", selector, exc)
    return clicks


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def _extract_card(card: Any) -> SearchResult | None:
    name = await _safe_inner_text(card.locator(_RESULT_NAME_SELECTOR).first)
    if not name:
        return None

    href = await _safe_attr(card.locator(_RESULT_LINK_SELECTOR).first, "href")
    rating = await _safe_inner_text(card.locator(_RESULT_RATING_SELECTOR).first)
    location = await _safe_inner_text(card.locator(_RESULT_LOCATION_SELECTOR).first)

    profile_url = _absolute_url(_SEARCH_URL, href) if href else _SEARCH_URL

    snippet_bits = [b for b in (rating, location) if b]
    snippet = " — ".join(snippet_bits)

    extras: dict[str, Any] = {
        "rating": rating,
        "location": location,
        "profile_url": profile_url,
    }
    return SearchResult(
        url=profile_url,
        title=name,
        snippet=snippet,
        source_kind="bbb",
        extras=extras,
    )


async def search(query: str, *, max_results: int = 25) -> list[SearchResult]:
    """Run a BBB national search for ``query``; return up to ``max_results`` hits.

    BBB is regional — a name without a city/state qualifier typically returns
    one row per bureau branch (different cities/states for the same business).
    Let the planner narrow the query with location terms when disambiguation
    matters.
    """
    if not query or not query.strip():
        return []

    # The national search endpoint accepts ``find_country`` + ``find_text``;
    # all other filters (city, category) are optional and we leave them off
    # so the planner can narrow at the query layer rather than via this URL.
    search_url = (
        f"{_SEARCH_URL}?find_country=USA&find_text={query.strip().replace(' ', '+')}"
    )

    try:
        async with browser.browser_session() as ctx:
            page = await ctx.new_page()
            try:
                await browser.navigate(page, search_url)

                try:
                    await page.locator(_RESULT_CARD_SELECTOR).first.wait_for(
                        timeout=15_000
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "bbb search cards did not render on %s: %s",
                        search_url,
                        exc,
                    )
                    await _save_diagnostic_screenshot(page, _HOST)
                    return []

                try:
                    cards = await page.locator(_RESULT_CARD_SELECTOR).all()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "bbb search selector miss on %s: %s", search_url, exc
                    )
                    await _save_diagnostic_screenshot(page, _HOST)
                    return []

                results: list[SearchResult] = []
                for card in cards[:max_results]:
                    try:
                        result = await _extract_card(card)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("bbb card parse failed: %s", exc)
                        continue
                    if result is not None:
                        results.append(result)
                return results
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
    except playwright.async_api.Error as exc:
        logger.warning("bbb search playwright error: %s", exc)
        return []
    except Exception as exc:  # noqa: BLE001 — never crash the planner
        logger.warning("bbb search unexpected error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def fetch(url: str) -> Source | None:
    """Open a BBB business profile and return a :class:`Source`.

    Strict host gate: only ``www.bbb.org`` URLs are accepted; everything else
    returns ``None`` without a network call. Eagerly clicks every visible
    "Show more" / "Show full complaint" reveal before scraping so the
    rolled-up markdown contains the full complaint summary text rather than
    the gated preview.
    """
    if not url:
        return None
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    if host not in _ACCEPTED_HOSTS:
        return None

    try:
        async with browser.browser_session() as ctx:
            page = await ctx.new_page()
            try:
                await browser.navigate(page, url)

                # Expand any reveals before scraping. Best-effort — selector
                # drift here should never block the rest of the fetch.
                await _click_all_reveals(page)

                title = await _safe_inner_text(page.locator("h1").first)
                if not title:
                    title = _HOST

                rating = await _collect_simple_text(page, _PROFILE_RATING_SELECTOR)
                accreditation = await _collect_simple_text(
                    page, _PROFILE_ACCREDITATION_SELECTOR
                )
                complaints_12mo = await _collect_simple_text(
                    page, _PROFILE_COMPLAINTS_12MO_SELECTOR
                )
                complaints_3yr = await _collect_simple_text(
                    page, _PROFILE_COMPLAINTS_3YR_SELECTOR
                )
                complaint_categories = await _collect_list(
                    page, _PROFILE_COMPLAINT_CATEGORIES_SELECTOR
                )
                government_actions = await _collect_simple_text(
                    page, _PROFILE_GOVERNMENT_ACTIONS_SELECTOR
                )

                sections: list[str] = [f"# {title}"]
                if rating:
                    sections.append(f"## Rating\n\n{rating}")
                if accreditation:
                    sections.append(f"## Accreditation\n\n{accreditation}")
                if complaints_12mo or complaints_3yr:
                    body_lines = []
                    if complaints_12mo:
                        body_lines.append(f"- Last 12 months: {complaints_12mo}")
                    if complaints_3yr:
                        body_lines.append(f"- Last 3 years: {complaints_3yr}")
                    sections.append(
                        "## Complaints (12mo / 3yr)\n\n" + "\n".join(body_lines)
                    )
                if complaint_categories:
                    body = "\n".join(f"- {c}" for c in complaint_categories)
                    sections.append(f"## Complaint summary categories\n\n{body}")
                if government_actions:
                    sections.append(f"## Government actions\n\n{government_actions}")

                cleaned_text = "\n\n".join(sections).strip()
                if not cleaned_text:
                    return None

                metadata: dict[str, Any] = {
                    "rating": rating,
                    "accreditation": accreditation,
                    "complaints_12mo": complaints_12mo,
                    "complaints_3yr": complaints_3yr,
                    "complaint_categories": complaint_categories,
                    "government_actions": government_actions,
                }

                return Source(
                    url=url,
                    title=title,
                    cleaned_text=cleaned_text,
                    raw_html=None,
                    fetched_at=datetime.now(UTC),
                    source_kind="bbb",
                    metadata=metadata,
                )
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
    except playwright.async_api.Error as exc:
        logger.warning("bbb fetch playwright error for %s: %s", url, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — never crash the planner
        logger.warning("bbb fetch unexpected error for %s: %s", url, exc)
        return None


def reset_for_tests() -> None:
    """Re-register host rate after browser.reset_for_tests() drops it. Test-only."""
    _register_host_rates()


KIND = "bbb_search"


class _PayloadSchema(_BaseSearchPayload):
    max_results: int | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=("www.bbb.org", "bbb.org"),
    skill_name=None,
    description=(
        "Better Business Bureau profiles + ratings (Playwright, no auth)"
    ),
    optional_payload_knobs="—",
    example_query="SBI Builders",
    module_name="bbb",
)


__all__ = ["KIND", "fetch", "reset_for_tests", "search"]
