"""Secretary of State business registries — Playwright-driven (issue #101).

Public surface:

* ``async def search(query, *, state="CA", max_results=25) -> list[SearchResult]``
  runs a state SoS business search by entity name OR entity number.
  Returns the business name, entity number, type, status, formed date, and
  profile URL.
* ``async def fetch(url) -> Source | None`` opens an entity profile page and
  returns markdown of: registered agent, principal address, officers (when
  listed), statement-of-information history, and filing history.

v1 ships **California** (``bizfileonline.sos.ca.gov``) — highest-volume
jurisdiction for the user's likely targets. Module is pluggable per state via
``_STATE_RECIPES``: DE / NV / WY / FL / NY each ship as config stubs that
return ``[]`` / ``None`` with a single WARN until selectors are wired.

No APIs, no auth. Per-host rate gate at 0.5 RPS — the SoS sites are public
infrastructure and Playwright traffic is conspicuous, so be polite.

Statements of Information for CA include the registered agent — this is the
direct unmask path before paying for OpenCorporates.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import playwright.async_api

from research_agent.tools import browser
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_DIAGNOSTICS_DIR = Path("data/diagnostics/sos")
_PER_HOST_RPS = 0.5

# CA bizfileonline entity numbers: usually a 7-digit corp number prefixed by a
# letter (e.g. ``C1234567``) for corporations, or a 12-digit numeric LLC id
# (e.g. ``201234567890``). Matching either flavour is sufficient to switch the
# search input from "name" to "entity number" mode where the recipe supports it.
_CA_ENTITY_NUMBER_RE = re.compile(r"^(?:[A-Z]\d{6,8}|\d{10,14})$")


# ---------------------------------------------------------------------------
# Recipes — one per state.
# ---------------------------------------------------------------------------

_STATE_RECIPES: dict[str, dict[str, Any]] = {
    # California — fully wired. bizfileonline.sos.ca.gov is a React SPA;
    # selectors target stable role/aria attributes where possible so a UI
    # tweak doesn't blank the connector.
    "CA": {
        "host": "bizfileonline.sos.ca.gov",
        "search_url": "https://bizfileonline.sos.ca.gov/search/business",
        "query_input": "input[placeholder*='Search' i]",
        "submit_button": "button[type='submit']",
        # Optional: if/when the UI exposes name-vs-number tabs, route here.
        "query_kind_selector": None,
        # Result rows.
        "row_selector": "table tbody tr",
        "name_selector": "td:nth-child(1)",
        "link_selector": "td:nth-child(1) a",
        "entity_number_selector": "td:nth-child(2)",
        "type_selector": "td:nth-child(3)",
        "status_selector": "td:nth-child(4)",
        "formed_date_selector": "td:nth-child(5)",
        # Entity profile.
        "agent_selector": "[data-field='registered-agent'], .registered-agent",
        "principal_address_selector": "[data-field='principal-address'], .principal-address",
        "officers_selector": "[data-field='officers'] li, .officers li",
        "soi_history_selector": "[data-field='soi-history'] tr, .soi-history tr",
        "filing_history_selector": "[data-field='filing-history'] tr, .filing-history tr",
    },
    # Delaware — STUB. The Division of Corporations charges $10 per name-search
    # certificate; the free public search at icis.corp.delaware.gov returns
    # only the entity name + file number, with no agent / officer / filings.
    # An operator should know coverage is shallow before relying on it.
    "DE": {"stub": True, "host": "icis.corp.delaware.gov"},
    "NV": {"stub": True, "host": "esos.nv.gov"},
    "WY": {"stub": True, "host": "wyobiz.wyo.gov"},
    "FL": {"stub": True, "host": "search.sunbiz.org"},
    "NY": {"stub": True, "host": "apps.dos.ny.gov"},
}


def _accepted_hosts() -> frozenset[str]:
    return frozenset(
        recipe["host"]
        for recipe in _STATE_RECIPES.values()
        if isinstance(recipe.get("host"), str)
    )


def _register_host_rates() -> None:
    """Wire per-host rates for every recipe with a configured host."""
    for recipe in _STATE_RECIPES.values():
        host = recipe.get("host")
        if isinstance(host, str) and host:
            browser.set_host_rate(host, _PER_HOST_RPS)


_register_host_rates()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _looks_like_entity_number(query: str) -> bool:
    """Heuristic: did the user paste an entity number rather than a name?"""
    cleaned = (query or "").strip().upper()
    if not cleaned:
        return False
    return bool(_CA_ENTITY_NUMBER_RE.match(cleaned))


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
        logger.debug("sos diagnostic screenshot failed: %s", exc)


def _absolute_url(base: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        parsed = urlparse(base)
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    return base.rstrip("/") + "/" + href


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def _extract_row(
    row: Any, recipe: dict[str, Any], *, search_url: str
) -> SearchResult | None:
    name = await _safe_inner_text(row.locator(recipe["name_selector"]).first)
    href = await _safe_attr(row.locator(recipe["link_selector"]).first, "href")
    if not name:
        return None

    entity_number = await _safe_inner_text(
        row.locator(recipe["entity_number_selector"]).first
    )
    entity_type = await _safe_inner_text(row.locator(recipe["type_selector"]).first)
    status = await _safe_inner_text(row.locator(recipe["status_selector"]).first)
    formed_date = await _safe_inner_text(
        row.locator(recipe["formed_date_selector"]).first
    )

    profile_url = _absolute_url(search_url, href) if href else search_url
    state = next(
        (
            code
            for code, r in _STATE_RECIPES.items()
            if r.get("host") == recipe["host"]
        ),
        "",
    )

    snippet_bits = [b for b in (entity_type, status, formed_date) if b]
    snippet = " — ".join(snippet_bits)

    extras: dict[str, Any] = {
        "entity_number": entity_number,
        "entity_type": entity_type,
        "status": status,
        "formed_date": formed_date,
        "state": state,
        "profile_url": profile_url,
    }
    return SearchResult(
        url=profile_url,
        title=name,
        snippet=snippet,
        source_kind="sos",
        extras=extras,
    )


async def search(
    query: str,
    *,
    state: str = "CA",
    max_results: int = 25,
) -> list[SearchResult]:
    """Run a Secretary of State business search; return up to ``max_results`` hits.

    ``state`` is the two-letter postal code (default ``"CA"``). Unknown or
    stub states return ``[]`` after a WARN so callers can route around the
    coverage gap rather than crashing the planner.

    Detects entity-number vs name queries by regex; when the recipe exposes
    a ``query_kind_selector``, the matching tab/radio is selected before
    submission.
    """
    code = (state or "").strip().upper()
    recipe = _STATE_RECIPES.get(code)
    if recipe is None:
        logger.warning("sos: no recipe for state %r", state)
        return []
    if recipe.get("stub"):
        logger.warning(
            "sos: state %s is a stub — coverage not yet wired (host=%s)",
            code,
            recipe.get("host"),
        )
        return []

    if not query or not query.strip():
        return []

    search_url = recipe["search_url"]
    host = recipe["host"]

    try:
        async with browser.browser_session() as ctx:
            page = await ctx.new_page()
            try:
                await browser.navigate(page, search_url)

                if recipe.get("query_kind_selector"):
                    kind = "number" if _looks_like_entity_number(query) else "name"
                    selector = recipe["query_kind_selector"].format(kind=kind)
                    try:
                        await page.locator(selector).first.click()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("sos query-kind toggle failed: %s", exc)

                try:
                    await page.locator(recipe["query_input"]).first.fill(query)
                    await page.locator(recipe["submit_button"]).first.click()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "sos search submit failed for state=%s: %s", code, exc
                    )
                    await _save_diagnostic_screenshot(page, host)
                    return []

                try:
                    rows = await page.locator(recipe["row_selector"]).all()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "sos search selector miss on %s: %s", search_url, exc
                    )
                    await _save_diagnostic_screenshot(page, host)
                    return []

                results: list[SearchResult] = []
                for row in rows[:max_results]:
                    try:
                        result = await _extract_row(
                            row, recipe, search_url=search_url
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("sos row parse failed: %s", exc)
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
        logger.warning("sos search playwright error for state=%s: %s", code, exc)
        return []
    except Exception as exc:  # noqa: BLE001 — never crash the planner
        logger.warning("sos search unexpected error for state=%s: %s", code, exc)
        return []


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def _resolve_recipe_for_host(host: str) -> dict[str, Any] | None:
    for recipe in _STATE_RECIPES.values():
        if recipe.get("host") == host and not recipe.get("stub"):
            return recipe
    return None


async def _collect_simple_text(page: Any, selector: str | None) -> str:
    if not selector:
        return ""
    try:
        return await _safe_inner_text(page.locator(selector).first)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sos profile selector miss (%s): %s", selector, exc)
        return ""


async def _collect_list(page: Any, selector: str | None) -> list[str]:
    if not selector:
        return []
    try:
        nodes = await page.locator(selector).all()
    except Exception as exc:  # noqa: BLE001
        logger.debug("sos profile list selector miss (%s): %s", selector, exc)
        return []
    out: list[str] = []
    for node in nodes:
        text = await _safe_inner_text(node)
        if text:
            out.append(text)
    return out


def _markdown_section(heading: str, lines: list[str]) -> str:
    if not lines:
        return ""
    body = "\n".join(f"- {line}" for line in lines)
    return f"## {heading}\n\n{body}"


async def fetch(url: str) -> Source | None:
    """Open a state SoS entity profile and return a :class:`Source`.

    Strict host gate: only URLs whose host matches a non-stub recipe are
    accepted; everything else returns ``None`` without a network call.
    """
    if not url:
        return None
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    if host not in _accepted_hosts():
        return None
    recipe = _resolve_recipe_for_host(host)
    if recipe is None:
        return None

    try:
        async with browser.browser_session() as ctx:
            page = await ctx.new_page()
            try:
                await browser.navigate(page, url)

                title = await _safe_inner_text(page.locator("h1").first)
                if not title:
                    title = host

                entity_number = await _collect_simple_text(
                    page, recipe.get("entity_number_selector")
                )
                entity_type = await _collect_simple_text(
                    page, recipe.get("type_selector")
                )
                status = await _collect_simple_text(page, recipe.get("status_selector"))
                formed_date = await _collect_simple_text(
                    page, recipe.get("formed_date_selector")
                )

                agent = await _collect_simple_text(page, recipe.get("agent_selector"))
                principal_address = await _collect_simple_text(
                    page, recipe.get("principal_address_selector")
                )
                officers = await _collect_list(page, recipe.get("officers_selector"))
                soi_rows = await _collect_list(page, recipe.get("soi_history_selector"))
                filing_rows = await _collect_list(
                    page, recipe.get("filing_history_selector")
                )

                meta_bits = [b for b in (entity_number, entity_type, status, formed_date) if b]
                meta_line = "_" + " · ".join(meta_bits) + "_" if meta_bits else ""

                sections: list[str] = [f"# {title}"]
                if meta_line:
                    sections.append(meta_line)

                if agent:
                    sections.append(f"## Registered agent\n\n{agent}")
                if principal_address:
                    sections.append(f"## Principal address\n\n{principal_address}")
                officers_md = _markdown_section("Officers", officers)
                if officers_md:
                    sections.append(officers_md)
                soi_md = _markdown_section("Statements of Information", soi_rows)
                if soi_md:
                    sections.append(soi_md)
                filings_md = _markdown_section("Filing history", filing_rows)
                if filings_md:
                    sections.append(filings_md)

                cleaned_text = "\n\n".join(sections).strip()
                if not cleaned_text:
                    return None

                metadata: dict[str, Any] = {
                    "entity_number": entity_number,
                    "entity_type": entity_type,
                    "status": status,
                    "formed_date": formed_date,
                    "registered_agent": agent,
                    "principal_address": principal_address,
                    "officers": officers,
                    "statements_of_information": soi_rows,
                    "filings": filing_rows,
                }

                return Source(
                    url=url,
                    title=title,
                    cleaned_text=cleaned_text,
                    raw_html=None,
                    fetched_at=datetime.now(UTC),
                    source_kind="sos",
                    metadata=metadata,
                )
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
    except playwright.async_api.Error as exc:
        logger.warning("sos fetch playwright error for %s: %s", url, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — never crash the planner
        logger.warning("sos fetch unexpected error for %s: %s", url, exc)
        return None


def reset_for_tests() -> None:
    """Re-register host rates after browser.reset_for_tests() drops them. Test-only."""
    _register_host_rates()


__all__ = ["fetch", "reset_for_tests", "search"]
