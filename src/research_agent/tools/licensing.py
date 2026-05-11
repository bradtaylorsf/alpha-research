"""State licensing boards — Playwright-driven (issue #91).

Public surface:

* ``async def search(query, *, state="CA", max_results=25) -> list[SearchResult]``
  runs a state contractor / professional licensing board search by license
  number OR business name. Returns license number, status, and a profile
  permalink. Pass ``return_diagnostic=True`` to get a status string back
  alongside the rows so callers (e.g. the smoke wrapper) can distinguish
  "board returned 0 hits" from "parser missed every row".
* ``async def fetch(url) -> Source | None`` opens the per-license profile and
  rolls the Business Information / Status / Classifications / Bonding /
  Workers' Compensation / Other sections (plus a Disciplinary History note
  derived from the PublicComplaintDisclosure link) into a single markdown
  ``cleaned_text``.

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
from typing import Any, Literal
from urllib.parse import urlparse

import playwright.async_api

from research_agent.tools import browser
from research_agent.tools._registry import (
    BaseSearchPayload as _BaseSearchPayload,
    register_kind as _register_kind,
)
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

# Diagnostics ship under data/diagnostics/cslb/ per issue #155 acceptance
# criteria — both a screenshot AND a page.content() HTML dump so future
# selector drift is visible without re-running.
_DIAGNOSTICS_DIR = Path("data/diagnostics/cslb")
_PER_HOST_RPS = 0.5

# CSLB license numbers are 6–8 digits. When the recipe defines a
# ``query_kind_selector`` the search radio is toggled accordingly so the
# board's form interprets the value as a license number rather than a name.
_LICENSE_NUMBER_RE = re.compile(r"^\d{6,8}$")

# CSLB's results page uses ASP.NET WebForms IDs of the form
# ``MainContent_dlMain_<field>_<index>`` where <index> is 0..N for each row.
_RESULT_ID_RE = re.compile(r"_(\d+)$")


# ---------------------------------------------------------------------------
# Recipes — one per state.
# ---------------------------------------------------------------------------

_STATE_RECIPES: dict[str, dict[str, Any]] = {
    # California (CSLB) — verified against the live "Check License II"
    # search page on 2026-05-06. The form is **tabbed**: license-number
    # search, business-name search, and personnel-name search each have
    # their own input + submit button, with only the active tab's inputs
    # visible. A tab button must be clicked first (the
    # ``tab_buttons_by_kind`` mapping) to make the corresponding input
    # visible before fill().
    #
    # The submit posts back to ``NameSearch.aspx`` (or
    # ``LicenseDetail.aspx`` for license-number search) and the results
    # render as a single ``MainContent_dlMain`` table whose rows carry
    # numbered ``MainContent_dlMain_<field>_<N>`` spans/links — there is
    # no class-based row layout. The detail page (``LicenseDetail.aspx``)
    # is a single stacked table; CSLB does NOT use tabbed sections for the
    # detail view, despite what the prior recipe assumed.
    "CA": {
        "host": "www.cslb.ca.gov",
        "search_url": (
            "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/CheckLicense.aspx"
        ),
        "tab_buttons_by_kind": {
            "number": "#LicNoButton",
            "name": "#BusNameButton",
        },
        "query_inputs_by_kind": {
            "number": "#MainContent_LicNo",
            "name": "#MainContent_NextName",
        },
        "submit_buttons_by_kind": {
            "number": "#MainContent_Contractor_License_Number_Search",
            "name": "#MainContent_Contractor_Business_Name_Button",
        },
        # The outer results container — used to distinguish 'CSLB
        # rendered the results table but had no rows' from 'page error /
        # the user got bounced back to the search form'.
        "results_table_id": "MainContent_dlMain",
        # Profile detail (LicenseDetail.aspx) selectors — single stacked
        # table, no tabs.
        "profile_license_number_id": "MainContent_Header2Detail",
        "profile_status_id": "MainContent_Status",
        "profile_classifications_id": "MainContent_ClassCellTable",
        "profile_expiration_id": "MainContent_ExpDt",
        "profile_issue_date_id": "MainContent_IssDt",
        "profile_entity_id": "MainContent_Entity",
        "profile_business_info_id": "MainContent_BusInfo",
        "profile_bonding_id": "MainContent_BondingCellTable",
        "profile_workers_comp_id": "MainContent_WCStatus",
        "profile_other_id": "MainContent_MultiLicDisplay",
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


SearchStatus = Literal[
    "ok",
    "no-hits",
    "parser-miss",
    "submit-failed",
    "page-error",
]


def _looks_like_license_number(query: str) -> bool:
    """Heuristic: did the user paste a license number rather than a name?"""
    cleaned = (query or "").strip()
    if not cleaned:
        return False
    return bool(_LICENSE_NUMBER_RE.match(cleaned))


def _absolute_url(base: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        parsed = urlparse(base)
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    return base.rstrip("/") + "/" + href


async def _save_diagnostic_dump(page: Any, host: str, *, label: str = "") -> None:
    """Persist a screenshot AND the rendered HTML for operator inspection.

    The previous implementation wrote only a PNG; future selector drift then
    required a re-run with attach-to-running-browser. Dumping the actual HTML
    the parser saw closes that loop — operators can diff against the in-tree
    fixture (`tests/fixtures/cslb/sbi_builders_results.html`) to spot the
    change immediately.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"-{label}" if label else ""
    base = _DIAGNOSTICS_DIR / f"{host}-{stamp}{suffix}"
    try:
        base.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("licensing diagnostic mkdir failed: %s", exc)
        return
    try:
        await page.screenshot(path=str(base.with_suffix(".png")))
    except Exception as exc:  # noqa: BLE001
        logger.debug("licensing diagnostic screenshot failed: %s", exc)
    try:
        html = await page.content()
        base.with_suffix(".html").write_text(html or "", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.debug("licensing diagnostic html dump failed: %s", exc)


# ---------------------------------------------------------------------------
# Pure parsers — testable without a live browser.
# ---------------------------------------------------------------------------


def _parse_search_results(
    html: str,
    *,
    recipe: dict[str, Any],
    search_url: str,
    state: str,
    max_results: int = 25,
) -> tuple[list[SearchResult], SearchStatus]:
    """Parse a CSLB Name/License Search results page into ``SearchResult`` rows.

    Returns ``(results, status)``. ``status`` distinguishes 'CSLB returned 0
    hits' from 'parser missed all rows' from 'CSLB never rendered the
    results table' — the smoke wrapper uses this to print a truthful
    one-liner without re-running the search.
    """
    from bs4 import BeautifulSoup  # lazy

    table_id = recipe.get("results_table_id") or ""
    soup = BeautifulSoup(html or "", "html.parser")
    table = soup.find(id=table_id) if table_id else None
    if table is None:
        return [], "page-error"

    name_spans = soup.select(f"span[id^='{table_id}_lblName_']")
    if not name_spans:
        return [], "no-hits"

    results: list[SearchResult] = []
    for span in name_spans:
        if len(results) >= max_results:
            break
        idx_match = _RESULT_ID_RE.search(span.get("id", ""))
        if idx_match is None:
            continue
        idx = idx_match.group(1)
        name = span.get_text(strip=True)
        if not name:
            continue
        license_link = soup.find(id=f"{table_id}_hlLicense_{idx}")
        href = license_link.get("href", "") if license_link else ""
        license_number = (
            license_link.get_text(strip=True) if license_link else ""
        )
        status_span = soup.find(id=f"{table_id}_lblLicenseStatus_{idx}")
        status = status_span.get_text(strip=True) if status_span else ""
        city_span = soup.find(id=f"{table_id}_lblCity_{idx}")
        city = city_span.get_text(strip=True) if city_span else ""
        type_span = soup.find(id=f"{table_id}_lblType_{idx}")
        name_type = type_span.get_text(strip=True) if type_span else ""

        if href:
            profile_url = _absolute_url(search_url, str(href))
        elif license_number:
            profile_url = f"{search_url}?LicNum={license_number}"
        else:
            profile_url = search_url

        snippet_bits = [b for b in (name_type, city, status) if b]
        snippet = " — ".join(snippet_bits)

        extras: dict[str, Any] = {
            "license_number": license_number,
            "status": status,
            # Classification + expiration aren't on the search-results
            # page — they live on the per-license detail page. fetch()
            # populates them. Keeping the keys present preserves the
            # downstream contract (extras is a stable dict shape).
            "classification": "",
            "expiration": "",
            "city": city,
            "name_type": name_type,
            "state": state,
            "profile_url": profile_url,
        }
        results.append(
            SearchResult(
                url=profile_url,
                title=name,
                snippet=snippet,
                source_kind="licensing",
                extras=extras,
            )
        )

    if not results:
        return [], "parser-miss"
    return results, "ok"


# Profile sections in render order. Each tuple: (heading, recipe key,
# metadata key). The detail page stacks these in a single table, so we
# scrape by ID rather than navigating tabs.
_PROFILE_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("Business Information", "profile_business_info_id", "business_info"),
    ("License Status", "profile_status_id", "status_section"),
    ("Classifications", "profile_classifications_id", "classifications"),
    ("Bonding Information", "profile_bonding_id", "bonding"),
    ("Workers' Compensation", "profile_workers_comp_id", "workers_comp"),
    ("Other", "profile_other_id", "other"),
)


def _parse_profile(html: str, url: str, *, recipe: dict[str, Any]) -> Source | None:
    """Parse a CSLB LicenseDetail.aspx page into a :class:`Source`.

    Pulls the stacked Business Information / Status / Classifications /
    Bonding / Workers' Comp / Other sections by their stable
    ``MainContent_*`` IDs. Disciplinary history isn't a stacked section on
    CSLB — it's gated behind a ``PublicComplaintDisclosure.aspx`` link
    that only renders when the contractor has disclosable actions. The
    parser surfaces that link (or a 'no actions' note) under a
    ``Disciplinary History`` heading so the metadata key remains stable
    for the smoke wrapper.
    """
    from bs4 import BeautifulSoup  # lazy

    soup = BeautifulSoup(html or "", "html.parser")

    license_number_id = recipe.get("profile_license_number_id")
    license_number = ""
    if license_number_id:
        node = soup.find(id=license_number_id)
        if node:
            license_number = node.get_text(strip=True)

    expire_node = soup.find(id=recipe.get("profile_expiration_id"))
    expiration = expire_node.get_text(strip=True) if expire_node else ""

    classification_node = soup.find(id=recipe.get("profile_classifications_id"))
    classification = (
        classification_node.get_text(" ", strip=True) if classification_node else ""
    )

    status_node = soup.find(id=recipe.get("profile_status_id"))
    status_text = status_node.get_text(" ", strip=True) if status_node else ""
    # Take the leading sentence as the compact status — the cell often
    # bundles a follow-up reminder like "All information below should be
    # reviewed." that's not useful in the metadata key.
    status = ""
    if status_text:
        first, sep, _ = status_text.partition(".")
        status = (first.strip() + (sep or "")).strip()

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    if not title and license_number:
        title = f"License #{license_number}"
    if not title:
        parsed = urlparse(url)
        title = parsed.netloc or "License Detail"

    # The legal-disclaimer block always carries a "click here for a
    # definition of disclosable actions" link; it's NOT an actual
    # disclosure. The real disclosure link only renders when the
    # contractor has disclosable actions, and it lives outside the
    # `#disclaimer` ul. Filter the disclaimer's link out so we don't
    # falsely flag every active license as having a complaint history.
    real_disclosure_link = None
    for link in soup.select("a[href*='PublicComplaintDisclosure']"):
        if link.find_parent(id="disclaimer") is not None:
            continue
        real_disclosure_link = link
        break
    if real_disclosure_link is not None:
        link_text = (
            real_disclosure_link.get_text(strip=True) or "Public complaint disclosure"
        )
        href = real_disclosure_link.get("href", "")
        disciplinary_history = (
            f"{link_text} — see {href}" if href else link_text
        )
    else:
        disciplinary_history = "No disclosable actions reported on this profile."

    sections: list[str] = [f"# {title}"]
    meta_bits = [b for b in (license_number, classification, status, expiration) if b]
    if meta_bits:
        sections.append("_" + " · ".join(meta_bits) + "_")

    section_text: dict[str, str] = {}
    for heading, recipe_key, meta_key in _PROFILE_SECTIONS:
        sel_id = recipe.get(recipe_key)
        body = ""
        if sel_id:
            node = soup.find(id=sel_id)
            if node:
                body = node.get_text("\n", strip=True)
        section_text[meta_key] = body
        sections.append(f"## {heading}\n\n{body or '(not available)'}")

    sections.append(f"## Disciplinary History\n\n{disciplinary_history}")

    cleaned_text = "\n\n".join(sections).strip()
    # Empty profile = nothing besides the title heading, which means CSLB
    # served us something other than a license detail page.
    has_any_section = any(section_text.values())
    has_signal = (
        bool(meta_bits) or has_any_section or real_disclosure_link is not None
    )
    if not has_signal:
        return None

    metadata: dict[str, Any] = {
        "license_number": license_number,
        "status": status,
        "classification": classification,
        "expiration": expiration,
        "disciplinary_history": disciplinary_history,
    }
    for _, _, meta_key in _PROFILE_SECTIONS:
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


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def search(
    query: str,
    *,
    state: str = "CA",
    max_results: int = 25,
    return_diagnostic: bool = False,
) -> list[SearchResult] | tuple[list[SearchResult], SearchStatus]:
    """Run a state licensing-board search; return up to ``max_results`` hits.

    ``state`` is the two-letter postal code (default ``"CA"`` for CSLB).
    Unknown or stub states return ``[]`` after a WARN so callers can route
    around the coverage gap rather than crashing the planner.

    Detects license-number vs name queries by regex; the matching tab/input
    pair is selected before submission.

    When ``return_diagnostic=True`` returns ``(results, status)`` so callers
    can distinguish 'CSLB returned 0 hits' from 'parser missed every row'
    from 'submit failed'. Default ``False`` keeps the historical
    list-only return shape.
    """
    code = (state or "").strip().upper()
    recipe = _STATE_RECIPES.get(code)
    if recipe is None:
        logger.warning("licensing: no recipe for state %r", state)
        return ([], "page-error") if return_diagnostic else []
    if recipe.get("stub"):
        logger.warning(
            "licensing: state %s is a stub — coverage not yet wired (host=%s)",
            code,
            recipe.get("host"),
        )
        return ([], "page-error") if return_diagnostic else []

    if not query or not query.strip():
        return ([], "no-hits") if return_diagnostic else []

    search_url = recipe["search_url"]
    host = recipe["host"]

    status: SearchStatus = "page-error"
    results: list[SearchResult] = []

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
                    return ([], "page-error") if return_diagnostic else []

                try:
                    await page.locator(input_selector).first.fill(query)
                    await page.locator(submit_selector).first.click()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "licensing search submit failed for state=%s: %s", code, exc
                    )
                    await _save_diagnostic_dump(page, host, label="submit-failed")
                    return ([], "submit-failed") if return_diagnostic else []

                # ASP.NET WebForms postback can still be in flight when we
                # read content — wait for the network to settle. If the
                # board is slow we fall back on a best-effort read; the
                # parser will surface a 'page-error' status and dump
                # diagnostics for the operator.
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=15_000
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "licensing wait_for_load_state failed: %s", exc
                    )

                try:
                    html = await page.content()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "licensing search page.content() failed for %s: %s",
                        search_url,
                        exc,
                    )
                    await _save_diagnostic_dump(page, host, label="content-failed")
                    return ([], "page-error") if return_diagnostic else []

                results, status = _parse_search_results(
                    html,
                    recipe=recipe,
                    search_url=search_url,
                    state=code,
                    max_results=max_results,
                )
                if status in ("parser-miss", "page-error"):
                    logger.warning(
                        "licensing search %s on %s for query=%r",
                        status,
                        search_url,
                        query,
                    )
                    await _save_diagnostic_dump(page, host, label=status)
                elif status == "no-hits":
                    logger.info(
                        "licensing search returned 0 hits on %s for query=%r",
                        search_url,
                        query,
                    )
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
    except playwright.async_api.Error as exc:
        logger.warning(
            "licensing search playwright error for state=%s: %s", code, exc
        )
        return ([], "page-error") if return_diagnostic else []
    except Exception as exc:  # noqa: BLE001 — never crash the planner
        logger.warning(
            "licensing search unexpected error for state=%s: %s", code, exc
        )
        return ([], "page-error") if return_diagnostic else []
    return (results, status) if return_diagnostic else results


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def _resolve_recipe_for_host(host: str) -> dict[str, Any] | None:
    for recipe in _STATE_RECIPES.values():
        if recipe.get("host") == host and not recipe.get("stub"):
            return recipe
    return None


async def fetch(url: str) -> Source | None:
    """Open a state licensing-board profile and return a :class:`Source`.

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
                # CSLB's LicenseDetail.aspx redirects back to the search
                # form when the request lacks a same-origin referer — so
                # set one before navigating, mimicking the click-from-
                # results-page flow a real user would take. Without this,
                # fetch() silently receives the search form HTML and
                # ``_parse_profile`` returns None.
                referer = recipe.get("search_url")
                if referer:
                    try:
                        await page.set_extra_http_headers({"Referer": referer})
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "licensing fetch set_extra_http_headers failed: %s",
                            exc,
                        )
                await browser.navigate(page, url)
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=15_000
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "licensing fetch wait_for_load_state failed: %s", exc
                    )
                try:
                    html = await page.content()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "licensing fetch page.content() failed for %s: %s",
                        url,
                        exc,
                    )
                    return None
                return _parse_profile(html, url, recipe=recipe)
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


KIND = "licensing_search"


class _PayloadSchema(_BaseSearchPayload):
    state: str | None = None
    max_results: int | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=(
        "www.cslb.ca.gov",
        "www.tdlr.texas.gov",
        "www.myfloridalicense.com",
        "www.dos.ny.gov",
    ),
    skill_name=None,
    description=(
        "State contractor / licensing-board lookups (Playwright; CA wired,"
        " others stubs)"
    ),
    optional_payload_knobs="`state: CA\\|TX\\|FL\\|NY`",
    example_query="SBI Builders",
    module_name="licensing",
)


__all__ = ["KIND", "SearchStatus", "fetch", "reset_for_tests", "search"]
