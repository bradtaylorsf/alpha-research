"""Cal-Access / Power Search connector — California campaign finance (issue #96).

Public surface:

* ``async def search(query, *, kind="contributions", max_results=25) -> list[SearchResult]``
  runs the MapLight-powered Power Search at ``powersearch.sos.ca.gov``.
  ``kind`` selects among ``contributions``, ``independent_expenditures``,
  ``lobbying``. Returns donor / payee / committee, amount, date, permalink.
* ``async def fetch(url) -> Source | None`` opens a Power Search detail page
  and returns markdown of the rolled-up record.

State-level analog to FEC. Covers 2001–present California state campaigns,
ballot measures, lobbying, and independent expenditures. No API, no auth —
Power Search is JS-heavy (Vue/React mounts), so Playwright with explicit
row waits is the only viable scrape path.

Per-host rate gate at 0.5 RPS — the SoS site is public infrastructure and
Playwright traffic is conspicuous.

A future bulk-CSV loader (against ``cal-access.sos.ca.gov/.../downloads/``)
would be more efficient for full-cycle analysis; track separately.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import playwright.async_api

from research_agent.tools import browser
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_DIAGNOSTICS_DIR = Path("data/diagnostics/calaccess")
_PER_HOST_RPS = 0.5
_HOST = "powersearch.sos.ca.gov"

# Short per-call timeout for selector reads. Playwright's default 30s auto-wait
# would hang the connector when a row column is missing; 2s is plenty for any
# element that has actually rendered after the row wait_for has fired.
_SELECTOR_READ_TIMEOUT_MS = 2_000


# ---------------------------------------------------------------------------
# Recipes — one per Power Search "kind".
# ---------------------------------------------------------------------------

_KIND_RECIPES: dict[str, dict[str, Any]] = {
    "contributions": {
        "search_url": "https://powersearch.sos.ca.gov/",
        # Power Search exposes per-kind tabs along the top of the page; the
        # contributions tab is the default view but we click it explicitly so
        # state from a prior render can't leak in.
        "kind_tab_selector": "a[href*='contributions' i], button:has-text('Contributions')",
        "query_input": "input[type='search'], input[placeholder*='Search' i]",
        "submit_button": "button[type='submit'], button:has-text('Search')",
        "row_selector": "table tbody tr",
        # Column indices align with the rendered table; donor / contributor /
        # employer is cell-1, recipient committee is cell-2, amount is cell-3,
        # date is cell-4. Per-kind selectors below override only the labels.
        "donor_selector": "td:nth-child(1)",
        "committee_selector": "td:nth-child(2)",
        "amount_selector": "td:nth-child(3)",
        "date_selector": "td:nth-child(4)",
        "permalink_selector": "td:nth-child(1) a, a.detail-link",
        "primary_label": "donor",
    },
    "independent_expenditures": {
        "search_url": "https://powersearch.sos.ca.gov/independent-expenditures/",
        "kind_tab_selector": (
            "a[href*='independent' i], button:has-text('Independent Expenditures')"
        ),
        "query_input": "input[type='search'], input[placeholder*='Search' i]",
        "submit_button": "button[type='submit'], button:has-text('Search')",
        "row_selector": "table tbody tr",
        "payee_selector": "td:nth-child(1)",
        "committee_selector": "td:nth-child(2)",
        "amount_selector": "td:nth-child(3)",
        "date_selector": "td:nth-child(4)",
        "permalink_selector": "td:nth-child(1) a, a.detail-link",
        "primary_label": "payee",
    },
    "lobbying": {
        "search_url": "https://powersearch.sos.ca.gov/lobbying/",
        "kind_tab_selector": "a[href*='lobbying' i], button:has-text('Lobbying')",
        "query_input": "input[type='search'], input[placeholder*='Search' i]",
        "submit_button": "button[type='submit'], button:has-text('Search')",
        "row_selector": "table tbody tr",
        # Lobbying rows: lobbyist/firm cell-1, client cell-2, amount cell-3,
        # period cell-4. We map "lobbyist" into the donor slot for the unified
        # ``primary`` field at extraction time.
        "lobbyist_selector": "td:nth-child(1)",
        "committee_selector": "td:nth-child(2)",
        "amount_selector": "td:nth-child(3)",
        "date_selector": "td:nth-child(4)",
        "permalink_selector": "td:nth-child(1) a, a.detail-link",
        "primary_label": "lobbyist",
    },
}


def _register_host_rate() -> None:
    browser.set_host_rate(_HOST, _PER_HOST_RPS)


_register_host_rate()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _safe_inner_text(locator: Any) -> str:
    try:
        text = await locator.inner_text(timeout=_SELECTOR_READ_TIMEOUT_MS)
    except TypeError:
        # Test fakes don't accept the timeout kwarg — fall back gracefully.
        try:
            text = await locator.inner_text()
        except Exception:  # noqa: BLE001
            return ""
    except Exception:  # noqa: BLE001 — selector miss must not raise
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


async def _save_diagnostic_screenshot(page: Any, label: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = _DIAGNOSTICS_DIR / f"{label}-{stamp}.png"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(target))
    except Exception as exc:  # noqa: BLE001
        logger.debug("calaccess diagnostic screenshot failed: %s", exc)


def _absolute_url(base: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        parsed = urlparse(base)
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    return base.rstrip("/") + "/" + href.lstrip("./")


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def _extract_row(
    row: Any,
    recipe: dict[str, Any],
    *,
    kind: str,
    search_url: str,
) -> SearchResult | None:
    primary_label = recipe["primary_label"]
    primary_selector_key = f"{primary_label}_selector"
    primary_selector = recipe.get(primary_selector_key)
    if not primary_selector:
        return None

    primary = await _safe_inner_text(row.locator(primary_selector).first)
    committee = await _safe_inner_text(row.locator(recipe["committee_selector"]).first)
    amount = await _safe_inner_text(row.locator(recipe["amount_selector"]).first)
    date = await _safe_inner_text(row.locator(recipe["date_selector"]).first)
    href = await _safe_attr(row.locator(recipe["permalink_selector"]).first, "href")

    if not primary and not committee:
        return None

    permalink = _absolute_url(search_url, href) if href else search_url
    title = primary or committee
    snippet_bits = [b for b in (amount, date, committee) if b]
    snippet = " — ".join(snippet_bits)

    extras: dict[str, Any] = {
        "kind": kind,
        "donor": primary if primary_label == "donor" else "",
        "payee": primary if primary_label == "payee" else "",
        "lobbyist": primary if primary_label == "lobbyist" else "",
        "committee": committee,
        "amount": amount,
        "date": date,
        "permalink": permalink,
    }
    return SearchResult(
        url=permalink,
        title=title,
        snippet=snippet,
        source_kind="calaccess",
        extras=extras,
    )


async def search(
    query: str,
    *,
    kind: str = "contributions",
    max_results: int = 25,
) -> list[SearchResult]:
    """Run a Power Search query; return up to ``max_results`` hits.

    ``kind`` is one of ``contributions`` (default), ``independent_expenditures``,
    or ``lobbying``. Unknown kinds return ``[]`` after a WARN so callers can
    route around the gap rather than crashing the planner.
    """
    recipe = _KIND_RECIPES.get(kind)
    if recipe is None:
        logger.warning("calaccess: unknown kind %r", kind)
        return []
    if not query or not query.strip():
        return []

    search_url = recipe["search_url"]

    try:
        async with browser.browser_session() as ctx:
            page = await ctx.new_page()
            try:
                await browser.navigate(page, search_url)

                # Optional: click the kind tab. Power Search renders its tabs
                # inside the same SPA shell, so a tab click is a synchronous
                # re-render rather than a navigation.
                tab_selector = recipe.get("kind_tab_selector")
                if tab_selector:
                    try:
                        await page.locator(tab_selector).first.click()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("calaccess kind tab toggle failed: %s", exc)

                try:
                    await page.locator(recipe["query_input"]).first.fill(query)
                    await page.locator(recipe["submit_button"]).first.click()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "calaccess search submit failed for kind=%s: %s", kind, exc
                    )
                    await _save_diagnostic_screenshot(page, kind)
                    return []

                # Power Search is a Vue/React app — rows render after an XHR
                # round-trip. Wait for the first row before reading.
                try:
                    await page.locator(recipe["row_selector"]).first.wait_for(
                        timeout=15_000
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "calaccess search rows did not render on %s: %s",
                        search_url,
                        exc,
                    )
                    await _save_diagnostic_screenshot(page, kind)
                    return []

                try:
                    rows = await page.locator(recipe["row_selector"]).all()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "calaccess search selector miss on %s: %s", search_url, exc
                    )
                    await _save_diagnostic_screenshot(page, kind)
                    return []

                results: list[SearchResult] = []
                for row in rows[:max_results]:
                    try:
                        result = await _extract_row(
                            row, recipe, kind=kind, search_url=search_url
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("calaccess row parse failed: %s", exc)
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
        logger.warning("calaccess search playwright error for kind=%s: %s", kind, exc)
        return []
    except Exception as exc:  # noqa: BLE001 — never crash the planner
        logger.warning("calaccess search unexpected error for kind=%s: %s", kind, exc)
        return []


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def _collect_simple_text(page: Any, selector: str | None) -> str:
    if not selector:
        return ""
    try:
        return await _safe_inner_text(page.locator(selector).first)
    except Exception as exc:  # noqa: BLE001
        logger.debug("calaccess profile selector miss (%s): %s", selector, exc)
        return ""


async def fetch(url: str) -> Source | None:
    """Open a Power Search detail page and return a :class:`Source`.

    Strict host gate — only ``powersearch.sos.ca.gov`` is accepted; everything
    else returns ``None`` without a network call.
    """
    if not url:
        return None
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    if host != _HOST:
        return None

    try:
        async with browser.browser_session() as ctx:
            page = await ctx.new_page()
            try:
                await browser.navigate(page, url)

                title = await _safe_inner_text(page.locator("h1").first)
                if not title:
                    title = _HOST

                # Detail pages roll the record up under a single key/value
                # block. We don't know the exact selector layout in advance,
                # so we read a small set of known shapes and let missing
                # selectors fall through to empty strings.
                record = await _collect_simple_text(page, ".record, [data-section='record']")
                parties = await _collect_simple_text(
                    page, ".parties, [data-section='parties']"
                )
                amount = await _collect_simple_text(
                    page, ".amount, [data-section='amount']"
                )
                date = await _collect_simple_text(page, ".date, [data-section='date']")
                filing_ref = await _collect_simple_text(
                    page, ".filing, [data-section='filing']"
                )

                sections: list[str] = [f"# {title}"]
                if record:
                    sections.append(f"## Record\n\n{record}")
                if parties:
                    sections.append(f"## Parties\n\n{parties}")
                if amount:
                    sections.append(f"## Amount\n\n{amount}")
                if date:
                    sections.append(f"## Date\n\n{date}")
                if filing_ref:
                    sections.append(f"## Filing reference\n\n{filing_ref}")

                cleaned_text = "\n\n".join(sections).strip()
                if not cleaned_text:
                    return None

                metadata: dict[str, Any] = {
                    "record": record,
                    "parties": parties,
                    "amount": amount,
                    "date": date,
                    "filing_reference": filing_ref,
                }

                return Source(
                    url=url,
                    title=title,
                    cleaned_text=cleaned_text,
                    raw_html=None,
                    fetched_at=datetime.now(UTC),
                    source_kind="calaccess",
                    metadata=metadata,
                )
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
    except playwright.async_api.Error as exc:
        logger.warning("calaccess fetch playwright error for %s: %s", url, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — never crash the planner
        logger.warning("calaccess fetch unexpected error for %s: %s", url, exc)
        return None


def reset_for_tests() -> None:
    """Re-register host rates after browser.reset_for_tests() drops them. Test-only."""
    _register_host_rate()


__all__ = ["fetch", "reset_for_tests", "search"]
