"""State licensing boards — Playwright-driven (issue #91).

Public surface:

* ``async def search(query, *, state="CA", max_results=25) -> list[SearchResult]``
  runs a state contractor / professional licensing board search by license
  number OR business name. Returns license number, status, classification,
  expiration date, and a profile permalink.
* ``async def fetch(url) -> Source | None`` opens the per-license profile and
  rolls the four CSLB tabs (Personnel, Workers' Compensation, Bonds,
  Disciplinary History) into a single markdown ``cleaned_text``.

v1 ships **California State License Board (CSLB)** as the worked example;
TX / FL / NY are config stubs that emit a WARN and return ``[]`` / ``None``.
The module is structured so adding a state is a recipe entry, not new code.

No APIs, no auth. Per-host rate gate at 0.5 RPS — CSLB has no public API
and Playwright traffic is conspicuous, so be polite.
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

_DIAGNOSTICS_DIR = Path("data/diagnostics/licensing")
_PER_HOST_RPS = 0.5

# CSLB license numbers are 6–8 digits. When the recipe defines a
# ``query_kind_selector`` the search radio is toggled accordingly so the
# board's form interprets the value as a license number rather than a name.
_LICENSE_NUMBER_RE = re.compile(r"^\d{6,8}$")


# ---------------------------------------------------------------------------
# Recipes — one per state.
# ---------------------------------------------------------------------------

_STATE_RECIPES: dict[str, dict[str, Any]] = {
    # California (CSLB) — fully wired against the live "Check License II"
    # form. The form is **tabbed**: license-number search, business-name
    # search, and personnel-name search each have their own input + submit
    # button, with only the active tab's inputs visible. A tab button must
    # be clicked first (the `tab_buttons_by_kind` mapping) to make the
    # corresponding input visible before fill().
    "CA": {
        "host": "www.cslb.ca.gov",
        "search_url": (
            "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/CheckLicense.aspx"
        ),
        # Tab buttons that switch which search input is visible.
        # `LicNoButton` selects the license-number tab; `BusNameButton`
        # selects the business-name tab. (HISNoButton / HISNameButton exist
        # for salesperson registration searches and are not wired here.)
        "tab_buttons_by_kind": {
            "number": "#LicNoButton",
            "name": "#BusNameButton",
        },
        # Per-mode input fields. ASP.NET WebForms naming — the visible IDs
        # are stable; the `name` attributes are also the form-post keys.
        "query_inputs_by_kind": {
            "number": "#MainContent_LicNo",
            "name": "#MainContent_NextName",
        },
        # Per-mode submit buttons — each tab has its own.
        "submit_buttons_by_kind": {
            "number": "#MainContent_Contractor_License_Number_Search",
            "name": "#MainContent_Contractor_Business_Name_Button",
        },
        # Result rows. CSLB's results page renders a single-license summary
        # for an exact match, or a table of links for multi-hit name searches.
        "row_selector": "table.searchresults tbody tr",
        "name_selector": "td.business-name",
        "link_selector": "td.business-name a",
        "license_number_selector": "td.license-number",
        "status_selector": "td.license-status",
        "classification_selector": "td.classification",
        "expiration_selector": "td.expiration",
        # Profile detail tabs. Each ``*_tab_button`` is clicked before the
        # corresponding ``*_section`` is scraped — eager-load every tab so the
        # rolled-up Source contains all four sections in one shot.
        "personnel_tab_button": "a#tabPersonnel, button#tabPersonnel",
        "personnel_section": "#sectionPersonnel",
        "workers_comp_tab_button": "a#tabWorkersComp, button#tabWorkersComp",
        "workers_comp_section": "#sectionWorkersComp",
        "bonds_tab_button": "a#tabBonds, button#tabBonds",
        "bonds_section": "#sectionBonds",
        "disciplinary_tab_button": "a#tabDisciplinary, button#tabDisciplinary",
        "disciplinary_section": "#sectionDisciplinary",
        # Header summary on the profile page (license number, status,
        # classification, expiration) — populated from data attributes when
        # present, otherwise scraped from the visible header.
        "profile_license_number_selector": "[data-field='license-number']",
        "profile_status_selector": "[data-field='license-status']",
        "profile_classification_selector": "[data-field='classification']",
        "profile_expiration_selector": "[data-field='expiration']",
    },
    # TX/FL/NY are stubs — selector recipes haven't been built yet. Emit a
    # WARN and return [] / None so the planner gracefully routes around the
    # gap rather than crashing.
    "TX": {"stub": True, "host": "www.tdlr.texas.gov"},
    "FL": {"stub": True, "host": "www.myfloridalicense.com"},
    "NY": {"stub": True, "host": "www.dos.ny.gov"},
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


def _looks_like_license_number(query: str) -> bool:
    """Heuristic: did the user paste a license number rather than a name?"""
    cleaned = (query or "").strip()
    if not cleaned:
        return False
    return bool(_LICENSE_NUMBER_RE.match(cleaned))


# Short per-call timeout for selector reads. Playwright's default 30s auto-wait
# is catastrophic on a page where most profile selectors may be absent — half
# a dozen missing selectors in ``fetch()`` would hang the connector for minutes.
_SELECTOR_READ_TIMEOUT_MS = 2_000


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
        logger.debug("licensing diagnostic screenshot failed: %s", exc)


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
    if not name:
        return None

    href = await _safe_attr(row.locator(recipe["link_selector"]).first, "href")
    license_number = await _safe_inner_text(
        row.locator(recipe.get("license_number_selector") or "").first
    ) if recipe.get("license_number_selector") else ""
    status = await _safe_inner_text(row.locator(recipe["status_selector"]).first)
    classification = await _safe_inner_text(
        row.locator(recipe.get("classification_selector") or "").first
    ) if recipe.get("classification_selector") else ""
    expiration = await _safe_inner_text(
        row.locator(recipe.get("expiration_selector") or "").first
    ) if recipe.get("expiration_selector") else ""

    state = next(
        (
            code
            for code, r in _STATE_RECIPES.items()
            if r.get("host") == recipe["host"]
        ),
        "",
    )

    if href:
        profile_url = _absolute_url(search_url, href)
    elif license_number:
        profile_url = f"{search_url}?LicNum={license_number}"
    else:
        profile_url = search_url

    snippet_bits = [b for b in (classification, status, expiration) if b]
    snippet = " — ".join(snippet_bits)

    extras: dict[str, Any] = {
        "license_number": license_number,
        "status": status,
        "classification": classification,
        "expiration": expiration,
        "state": state,
        "profile_url": profile_url,
    }
    return SearchResult(
        url=profile_url,
        title=name,
        snippet=snippet,
        source_kind="licensing",
        extras=extras,
    )


async def search(
    query: str,
    *,
    state: str = "CA",
    max_results: int = 25,
) -> list[SearchResult]:
    """Run a state licensing-board search; return up to ``max_results`` hits.

    ``state`` is the two-letter postal code (default ``"CA"`` for CSLB).
    Unknown or stub states return ``[]`` after a WARN so callers can route
    around the coverage gap rather than crashing the planner.

    Detects license-number vs name queries by regex; when the recipe exposes
    a ``query_kind_selector``, the matching radio is selected before
    submission.
    """
    code = (state or "").strip().upper()
    recipe = _STATE_RECIPES.get(code)
    if recipe is None:
        logger.warning("licensing: no recipe for state %r", state)
        return []
    if recipe.get("stub"):
        logger.warning(
            "licensing: state %s is a stub — coverage not yet wired (host=%s)",
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

                kind_key = (
                    "number" if _looks_like_license_number(query) else "name"
                )

                tab_buttons = recipe.get("tab_buttons_by_kind") or {}
                tab_selector = tab_buttons.get(kind_key)
                if tab_selector:
                    try:
                        await page.locator(tab_selector).first.click()
                    except Exception as exc:  # noqa: BLE001 — tab miss is non-fatal
                        logger.debug(
                            "licensing tab toggle failed (%s): %s", tab_selector, exc
                        )

                inputs = recipe.get("query_inputs_by_kind") or {}
                submits = recipe.get("submit_buttons_by_kind") or {}
                input_selector = inputs.get(kind_key) or recipe.get("query_input")
                submit_selector = submits.get(kind_key) or recipe.get("submit_button")
                if not input_selector or not submit_selector:
                    logger.warning(
                        "licensing recipe missing input/submit for state=%s kind=%s",
                        code,
                        kind_key,
                    )
                    return []

                try:
                    await page.locator(input_selector).first.fill(query)
                    await page.locator(submit_selector).first.click()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "licensing search submit failed for state=%s: %s", code, exc
                    )
                    await _save_diagnostic_screenshot(page, host)
                    return []

                try:
                    rows = await page.locator(recipe["row_selector"]).all()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "licensing search selector miss on %s: %s", search_url, exc
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
                        logger.warning("licensing row parse failed: %s", exc)
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
        logger.warning("licensing search playwright error for state=%s: %s", code, exc)
        return []
    except Exception as exc:  # noqa: BLE001 — never crash the planner
        logger.warning("licensing search unexpected error for state=%s: %s", code, exc)
        return []


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------

# Profile sections that fetch() rolls into the markdown body, in order.
# Each entry maps a (heading, tab-button-recipe-key, section-recipe-key,
# metadata-key) tuple — heading appears in cleaned_text, the tab button is
# clicked before the section is scraped, and the scraped text is mirrored
# under metadata[metadata_key] for structured callers.
_PROFILE_SECTIONS: tuple[tuple[str, str, str, str], ...] = (
    ("Personnel", "personnel_tab_button", "personnel_section", "personnel"),
    (
        "Workers' Compensation",
        "workers_comp_tab_button",
        "workers_comp_section",
        "workers_comp",
    ),
    ("Bonds", "bonds_tab_button", "bonds_section", "bonds"),
    (
        "Disciplinary History",
        "disciplinary_tab_button",
        "disciplinary_section",
        "disciplinary_history",
    ),
)


def _resolve_recipe_for_host(host: str) -> dict[str, Any] | None:
    for recipe in _STATE_RECIPES.values():
        if recipe.get("host") == host and not recipe.get("stub"):
            return recipe
    return None


async def _click_if_present(page: Any, selector: str | None) -> None:
    if not selector:
        return
    try:
        await page.locator(selector).first.click()
    except Exception as exc:  # noqa: BLE001 — missing tab is non-fatal
        logger.debug("licensing tab click miss (%s): %s", selector, exc)


async def _collect_simple_text(page: Any, selector: str | None) -> str:
    if not selector:
        return ""
    try:
        return await _safe_inner_text(page.locator(selector).first)
    except Exception as exc:  # noqa: BLE001
        logger.debug("licensing profile selector miss (%s): %s", selector, exc)
        return ""


async def fetch(url: str) -> Source | None:
    """Open a state licensing-board profile and return a :class:`Source`.

    Strict host gate: only URLs whose host matches a non-stub recipe are
    accepted; everything else returns ``None`` without a network call.
    Eagerly clicks every detail tab (Personnel, Workers' Comp, Bonds,
    Disciplinary History) before scraping so the rolled-up markdown contains
    all four sections — Disciplinary History is the primary signal for
    due-diligence runs.
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

                license_number = await _collect_simple_text(
                    page, recipe.get("profile_license_number_selector")
                )
                status = await _collect_simple_text(
                    page, recipe.get("profile_status_selector")
                )
                classification = await _collect_simple_text(
                    page, recipe.get("profile_classification_selector")
                )
                expiration = await _collect_simple_text(
                    page, recipe.get("profile_expiration_selector")
                )

                section_text: dict[str, str] = {}
                for heading, tab_key, section_key, meta_key in _PROFILE_SECTIONS:
                    await _click_if_present(page, recipe.get(tab_key))
                    text = await _collect_simple_text(page, recipe.get(section_key))
                    section_text[meta_key] = text
                    section_text[f"_heading_{meta_key}"] = heading

                meta_bits = [
                    b for b in (license_number, classification, status, expiration) if b
                ]
                meta_line = "_" + " · ".join(meta_bits) + "_" if meta_bits else ""

                sections: list[str] = [f"# {title}"]
                if meta_line:
                    sections.append(meta_line)

                for _, _, _, meta_key in _PROFILE_SECTIONS:
                    heading = section_text[f"_heading_{meta_key}"]
                    body = section_text.get(meta_key) or "(not available)"
                    sections.append(f"## {heading}\n\n{body}")

                cleaned_text = "\n\n".join(sections).strip()
                if not cleaned_text:
                    return None

                metadata: dict[str, Any] = {
                    "license_number": license_number,
                    "status": status,
                    "classification": classification,
                    "expiration": expiration,
                }
                for _, _, _, meta_key in _PROFILE_SECTIONS:
                    metadata[meta_key] = section_text.get(meta_key, "")

                return Source(
                    url=url,
                    title=title,
                    cleaned_text=cleaned_text,
                    raw_html=None,
                    fetched_at=datetime.now(UTC),
                    source_kind="licensing",
                    metadata=metadata,
                )
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
    except playwright.async_api.Error as exc:
        logger.warning("licensing fetch playwright error for %s: %s", url, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — never crash the planner
        logger.warning("licensing fetch unexpected error for %s: %s", url, exc)
        return None


def reset_for_tests() -> None:
    """Re-register host rates after browser.reset_for_tests() drops them. Test-only."""
    _register_host_rates()


__all__ = ["fetch", "reset_for_tests", "search"]
