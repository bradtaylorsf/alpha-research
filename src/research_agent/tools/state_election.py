"""Official state-election candidate roster connector."""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml  # type: ignore[import-untyped]

from research_agent.tools import browser
from research_agent.tools._registry import (
    BaseSearchPayload as _BaseSearchPayload,
)
from research_agent.tools._registry import (
    register_kind as _register_kind,
)
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path("config/state_election_recipes.yaml")
_DIAGNOSTICS_DIR = Path("data/diagnostics/state_election")
_DEFAULT_TIMEOUT = 20.0

_NAME_KEYS = (
    "candidate_name",
    "candidate name",
    "candidate",
    "name",
    "candidate full name",
    "full name",
)
_PARTY_KEYS = ("party", "political party", "party name")
_OFFICE_KEYS = ("office", "contest", "contest name", "race", "office sought")
_DISTRICT_KEYS = ("district_or_seat", "district", "seat", "district/seat")
_STATUS_KEYS = ("status", "candidate_status", "ballot status", "filing status")
_SOURCE_URL_KEYS = ("source_url", "source url", "url", "link")


def _load_recipes() -> dict[str, dict[str, Any]]:
    try:
        raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        logger.warning("state_election: recipe config missing at %s", _CONFIG_PATH)
        return {}
    if not isinstance(raw, dict):
        logger.warning("state_election: recipe config root must be a mapping")
        return {}
    recipes: dict[str, dict[str, Any]] = {}
    for state, recipe in raw.items():
        if isinstance(recipe, dict):
            recipes[str(state).upper()] = recipe
    return recipes


_RECIPES = _load_recipes()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _clean(value: Any) -> str:
    text = "" if value is None else str(value)
    return re.sub(r"\s+", " ", text).strip()


def _lookup(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    normalized = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        value = normalized.get(key)
        if value not in (None, ""):
            return _clean(value)
    return ""


def _chamber_from_office(office: str) -> str:
    lower = office.lower()
    if "senate" in lower:
        return "Senate"
    if "house" in lower or "representative" in lower or "congressional" in lower:
        return "House"
    return office


def _district_from_office(office: str) -> str:
    patterns = (
        r"\bdistrict\s+([A-Za-z0-9-]+)",
        r"\bdist\.?\s*([A-Za-z0-9-]+)",
        r"\bcd\s*([A-Za-z0-9-]+)",
        r"\bseat\s+([A-Za-z0-9-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, office, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _row_to_result(
    row: dict[str, Any],
    *,
    state: str,
    recipe: dict[str, Any],
    query: str,
) -> SearchResult | None:
    candidate = _lookup(row, _NAME_KEYS)
    if not candidate:
        return None
    office = _lookup(row, _OFFICE_KEYS)
    party = _lookup(row, _PARTY_KEYS)
    status = _lookup(row, _STATUS_KEYS) or "candidate-listed"
    district = _lookup(row, _DISTRICT_KEYS) or _district_from_office(office)
    chamber = _chamber_from_office(office)
    source_url = _lookup(row, _SOURCE_URL_KEYS) or str(recipe.get("source_url") or "")
    retrieved_at = _now_iso()
    confidence = 0.9 if str(recipe.get("retrieval_method")) == "static_fetch" else 0.82
    if query and query.lower() not in candidate.lower() and query.lower() not in office.lower():
        confidence -= 0.05

    extras = {
        "state": state,
        "chamber": chamber,
        "district_or_seat": district,
        "candidate_name": candidate,
        "party": party,
        "status": status,
        "candidate_status": status,
        "source_url": source_url,
        "source_type": str(recipe.get("source_type") or ""),
        "source_kind": "state_election",
        "retrieval_timestamp": retrieved_at,
        "confidence": round(confidence, 2),
    }
    bits = [candidate, party, chamber, district, status]
    return SearchResult(
        url=source_url,
        title=candidate,
        snippet=" — ".join(bit for bit in bits if bit),
        published_at=None,
        source_kind="state_election",
        score=round(confidence, 2),
        extras=extras,
    )


def _filter_rows(
    rows: list[SearchResult],
    *,
    query: str,
    office: str | None,
    max_results: int,
) -> list[SearchResult]:
    query_l = query.strip().lower()
    office_l = (office or "").strip().lower()
    out: list[SearchResult] = []
    for row in rows:
        haystack = " ".join(
            str(row.extras.get(k) or "")
            for k in ("candidate_name", "chamber", "district_or_seat", "party", "status")
        ).lower()
        if query_l and query_l not in haystack:
            continue
        if office_l and office_l not in haystack:
            continue
        out.append(row)
        if len(out) >= max_results:
            break
    return out


def _parse_csv(text: str, *, state: str, recipe: dict[str, Any], query: str) -> list[SearchResult]:
    reader = csv.DictReader(io.StringIO(text))
    rows: list[SearchResult] = []
    for raw in reader:
        result = _row_to_result(raw, state=state, recipe=recipe, query=query)
        if result is not None:
            rows.append(result)
    return rows


def _parse_html(text: str, *, state: str, recipe: dict[str, Any], query: str) -> list[SearchResult]:
    from bs4 import BeautifulSoup  # type: ignore[import-untyped]

    soup = BeautifulSoup(text or "", "html.parser")
    rows: list[SearchResult] = []
    for table in soup.find_all("table"):
        headers = [_clean(th.get_text(" ")) for th in table.find_all("th")]
        if not headers:
            continue
        for tr in table.find_all("tr"):
            cells = [_clean(td.get_text(" ")) for td in tr.find_all("td")]
            if not cells:
                continue
            raw = dict(zip(headers, cells, strict=False))
            result = _row_to_result(raw, state=state, recipe=recipe, query=query)
            if result is not None:
                rows.append(result)
    return rows


async def _static_search(
    query: str,
    *,
    state: str,
    recipe: dict[str, Any],
    office: str | None,
    max_results: int,
    timeout: float,
) -> list[SearchResult]:
    url = str(recipe.get("source_url") or "")
    source_type = str(recipe.get("source_type") or "").lower()
    if source_type not in {"csv", "html"}:
        logger.warning(
            "state_election %s recipe source_type=%s is registry-only; %s",
            state,
            source_type,
            recipe.get("known_unblocker") or "no parser configured",
        )
        return []
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            response = await client.get(url)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("state_election %s static fetch failed: %s", state, exc)
        return []
    if response.status_code != 200:
        logger.warning("state_election %s static fetch HTTP %s", state, response.status_code)
        return []
    if source_type == "csv":
        rows = _parse_csv(response.text, state=state, recipe=recipe, query=query)
    else:
        rows = _parse_html(response.text, state=state, recipe=recipe, query=query)
    if not rows:
        logger.warning(
            "state_election %s static parser returned 0 candidate rows; %s",
            state,
            recipe.get("known_unblocker") or "check recipe/source format",
        )
    return _filter_rows(rows, query=query, office=office, max_results=max_results)


async def _write_diagnostics(page: Any, *, state: str, label: str) -> None:
    target = _DIAGNOSTICS_DIR / state
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    screenshot = target / f"{stamp}-{label}.png"
    dom_path = target / f"{stamp}-{label}.html"
    try:
        await page.screenshot(path=str(screenshot))
    except Exception:  # noqa: BLE001
        pass
    try:
        html = await page.content()
    except Exception:  # noqa: BLE001
        html = ""
    if html:
        dom_path.write_text(html, encoding="utf-8")


async def _safe_text(locator: Any) -> str:
    try:
        return _clean(await locator.inner_text())
    except Exception:  # noqa: BLE001
        return ""


async def _row_selector_text(row: Any, selector: Any) -> str:
    if not isinstance(selector, str) or not selector:
        return ""
    try:
        locator = row.locator(selector)
    except Exception:  # noqa: BLE001
        return ""
    return await _safe_text(locator)


async def _portal_search(
    query: str,
    *,
    state: str,
    recipe: dict[str, Any],
    office: str | None,
    max_results: int,
) -> list[SearchResult]:
    selectors = recipe.get("selectors")
    if not isinstance(selectors, dict):
        logger.warning(
            "state_election %s portal recipe missing selectors; %s",
            state,
            recipe.get("known_unblocker") or "selectors required",
        )
        return []
    async with browser.browser_session(block_media=True) as ctx:
        page = await ctx.new_page()
        try:
            await browser.navigate(page, str(recipe.get("source_url") or ""))
            query_input = selectors.get("query_input")
            if query and query_input:
                await page.locator(query_input).fill(query)
            submit = selectors.get("submit_button")
            if submit:
                await page.locator(submit).click()
            row_locator = page.locator(str(selectors.get("row_selector") or "table tbody tr"))
            try:
                await row_locator.first.wait_for(timeout=10_000)
            except Exception:  # noqa: BLE001
                await _write_diagnostics(page, state=state, label="no-rows")
                return []
            rows = await row_locator.all()
            results: list[SearchResult] = []
            for row in rows:
                raw = {
                    "candidate_name": await _row_selector_text(
                        row, selectors.get("name_selector")
                    ),
                    "party": await _row_selector_text(row, selectors.get("party_selector")),
                    "office": await _row_selector_text(row, selectors.get("office_selector")),
                    "status": await _row_selector_text(row, selectors.get("status_selector")),
                    "district_or_seat": await _row_selector_text(
                        row, selectors.get("district_selector")
                    ),
                }
                result = _row_to_result(raw, state=state, recipe=recipe, query=query)
                if result is not None:
                    results.append(result)
            if not results:
                await _write_diagnostics(page, state=state, label="empty-parse")
                logger.warning(
                    "state_election %s portal returned no candidate rows; %s",
                    state,
                    recipe.get("known_unblocker") or "check portal selectors",
                )
            return _filter_rows(results, query=query, office=office, max_results=max_results)
        except Exception as exc:  # noqa: BLE001
            await _write_diagnostics(page, state=state, label="error")
            logger.warning("state_election %s portal search failed: %s", state, exc)
            return []
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass


async def search(
    query: str,
    *,
    state: str,
    office: str | None = None,
    cycle: int | None = None,
    max_results: int = 50,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[SearchResult]:
    """Search official state-election candidate roster sources."""
    state_norm = state.strip().upper()
    recipe = _RECIPES.get(state_norm)
    if recipe is None:
        logger.warning("state_election: no recipe for state=%s", state_norm)
        return []
    cycles = recipe.get("cycle_coverage")
    if cycle is not None and isinstance(cycles, list) and cycle not in cycles:
        logger.warning(
            "state_election %s recipe does not declare cycle %s coverage; %s",
            state_norm,
            cycle,
            recipe.get("known_unblocker") or "check election-specific source",
        )
    method = str(recipe.get("retrieval_method") or "static_fetch")
    if method == "static_fetch":
        return await _static_search(
            query,
            state=state_norm,
            recipe=recipe,
            office=office,
            max_results=max_results,
            timeout=timeout,
        )
    if method == "playwright_form":
        return await _portal_search(
            query,
            state=state_norm,
            recipe=recipe,
            office=office,
            max_results=max_results,
        )
    logger.warning(
        "state_election %s retrieval_method=%s is not automated yet; %s",
        state_norm,
        method,
        recipe.get("known_unblocker") or "manual portal use required",
    )
    return []


async def fetch(url: str, timeout: float = _DEFAULT_TIMEOUT) -> Source | None:
    """Fetch a state-election roster URL as a source."""
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            response = await client.get(url)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("state_election fetch failed for %s: %s", url, exc)
        return None
    if response.status_code != 200:
        return None
    title = parsed.netloc
    text = response.text
    return Source(
        url=url,
        title=title,
        cleaned_text=text,
        raw_html=text if "<html" in text.lower() else None,
        fetched_at=datetime.now(UTC),
        source_kind="state_election",
        metadata={"source_kind": "state_election"},
    )


KIND = "state_election_search"


class _PayloadSchema(_BaseSearchPayload):
    state: str
    office: str | None = None
    cycle: int | None = None
    max_results: int | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=(
        "elections.cdn.sos.ca.gov",
        "elections.myflorida.com",
        "elections.maryland.gov",
        "ncsbe.gov",
        "sos.texas.gov",
        "sos.ga.gov",
        "tracer.sos.colorado.gov",
        "vrems.scvotes.sc.gov",
        "www.elections.il.gov",
        "oklahoma.gov",
    ),
    skill_name="state_election",
    description="Official state election candidate roster sources and portals",
    optional_payload_knobs="`state`, `office`, `cycle`, `max_results`",
    example_query="2026 House candidates",
    module_name="state_election",
)


__all__ = ["KIND", "fetch", "search"]
