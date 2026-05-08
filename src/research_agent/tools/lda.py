"""Senate LDA (Lobbying Disclosure Act) connector (issue #103).

Public surface:

* ``async def search(query, *, kind="filings", max_results=20) -> list[SearchResult]``
  hits the LDA REST API (``lda.senate.gov/api/v1/``). ``kind`` selects among
  ``filings`` (LD-1 / LD-2 quarterly), ``registrants`` (lobbying firms /
  in-house registrants) or ``contributions`` (LD-203 semi-annual political
  contributions).
* ``async def fetch(url) -> Source | None`` opens an LD-2 filing detail page
  and returns markdown of the issues lobbied, the lobbyists named, and the
  income/expenses amount.

Auth: anonymous works for low-volume calls; setting ``LDA_API_KEY`` (free
registration at https://lda.senate.gov/api/register/) raises rate limits.
The token is sent via ``Authorization: Token <key>`` (REST framework
convention).

Per AC: 1 RPS per-host gate, mirroring ``tools/nonprofits.py``.

TODO(2026-06-30): the LDA team is migrating from ``lda.senate.gov`` to
``lda.gov``. Re-evaluate ``_BASE_URL`` and the accepted-host set near that
date — the old domain is expected to keep redirecting for a grace period
but new clients should target ``lda.gov`` once cutover finalises.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from research_agent import config
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_BASE_URL = "https://lda.senate.gov/api/v1/"
_SITE_BASE = "https://lda.senate.gov"
# Accept the new ``lda.gov`` cutover host once the migration lands; keeping
# both in the set means we don't have to flip a flag the moment it happens.
_ACCEPTED_HOSTS = frozenset({"lda.senate.gov", "lda.gov", "www.lda.gov"})
# AC: per-host rate of 1 RPS.
_RATE_LIMIT_INTERVAL = 1.0

_VALID_KINDS = {"filings", "registrants", "contributions"}

# LDA filing UUIDs are upper-case hex w/ dashes (RFC 4122). Tolerate either
# trailing slash and an optional ``/api/v1/filings/`` (REST endpoint) or
# ``/filings/`` (human-facing) prefix.
_FILING_URL_RE = re.compile(
    r"^/(?:api/v1/)?filings/(?P<uuid>[A-Fa-f0-9-]{36})/?$"
)

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


# ---------------------------------------------------------------------------
# Config / rate-limit helpers
# ---------------------------------------------------------------------------


def _resolve_api_key() -> str | None:
    """Return the configured LDA API key, or ``None`` for anonymous access."""
    raw = config.get("LDA_API_KEY") or ""
    key = raw.strip()
    return key or None


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": config.get("RESEARCH_USER_AGENT")
        or "research-agent/0.1 (+local; contact unset)",
    }
    key = _resolve_api_key()
    if key:
        headers["Authorization"] = f"Token {key}"
    return headers


async def _rate_limit_gate() -> None:
    """Block until at least ``_RATE_LIMIT_INTERVAL`` has passed since the last call."""
    global _last_call_monotonic
    async with _rate_lock:
        if _last_call_monotonic is not None:
            elapsed = time.monotonic() - _last_call_monotonic
            wait = _RATE_LIMIT_INTERVAL - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
        _last_call_monotonic = time.monotonic()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except ValueError:
            continue
    head = text.split("T", 1)[0]
    try:
        return datetime.strptime(head, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _fmt_money(value: Any) -> str:
    if value in (None, "", "null"):
        return "—"
    try:
        return f"${int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "—"


def _name_of(obj: Any) -> str:
    """Pull a human name from either a dict-of-strings or a bare string."""
    if isinstance(obj, dict):
        return str(obj.get("name") or "").strip()
    if obj is None:
        return ""
    return str(obj).strip()


def _filing_permalink(filing_uuid: str) -> str:
    return f"{_SITE_BASE}/filings/{filing_uuid}/"


def _filing_total(hit: dict[str, Any]) -> Any:
    """Return the dollar figure relevant to a filing (income xor expenses)."""
    income = hit.get("income")
    expenses = hit.get("expenses")
    # LD-2 reports either income (lobbying firm) or expenses (in-house),
    # never both. Prefer the populated one; fall back to whichever is present.
    if income not in (None, "", "null"):
        return income
    return expenses


# ---------------------------------------------------------------------------
# search() builders
# ---------------------------------------------------------------------------


def _build_filing_result(hit: dict[str, Any]) -> SearchResult | None:
    filing_uuid = (hit.get("filing_uuid") or "").strip()
    if not filing_uuid:
        return None

    filing_type_raw = hit.get("filing_type_display") or hit.get("filing_type") or ""
    filing_type = str(filing_type_raw).strip()
    filing_year = hit.get("filing_year")
    filing_period_raw = (
        hit.get("filing_period_display") or hit.get("filing_period") or ""
    )
    filing_period = str(filing_period_raw).strip()

    client_name = _name_of(hit.get("client"))
    registrant_name = _name_of(hit.get("registrant"))
    income = hit.get("income")
    expenses = hit.get("expenses")
    total = _filing_total(hit)
    dt_posted = hit.get("dt_posted")

    title_bits = [b for b in (client_name, filing_type, str(filing_year or ""), filing_period) if b]
    title = " – ".join(title_bits) if title_bits else f"LDA filing {filing_uuid}"

    snippet_bits: list[str] = []
    if registrant_name:
        snippet_bits.append(f"Registrant: {registrant_name}")
    if total not in (None, "", "null"):
        snippet_bits.append(f"Total: {_fmt_money(total)}")
    if filing_period:
        snippet_bits.append(filing_period)
    snippet = " — ".join(snippet_bits)

    document_url = (hit.get("filing_document_url") or "").strip()
    url = document_url or _filing_permalink(filing_uuid)

    extras: dict[str, Any] = {
        "filing_uuid": filing_uuid,
        "filing_type": filing_type,
        "filing_year": filing_year,
        "filing_period": filing_period,
        "client_name": client_name,
        "registrant_name": registrant_name,
        "income": income,
        "expenses": expenses,
        "url": url,
    }
    return SearchResult(
        url=url,
        title=title,
        snippet=snippet,
        published_at=_parse_iso_date(dt_posted),
        source_kind="lda",
        extras=extras,
    )


def _build_registrant_result(hit: dict[str, Any]) -> SearchResult | None:
    name = (hit.get("name") or "").strip()
    if not name:
        return None
    registrant_id = hit.get("id")

    addr_bits = [
        (hit.get("address_1") or "").strip(),
        (hit.get("address_2") or "").strip(),
        (hit.get("city") or "").strip(),
        (hit.get("state_display") or hit.get("state") or "").strip(),
        (hit.get("zip") or "").strip(),
    ]
    address = ", ".join(b for b in addr_bits if b)
    country = (hit.get("country_display") or hit.get("country") or "").strip()
    contact = (hit.get("contact_name") or "").strip()

    snippet_bits: list[str] = []
    if address:
        snippet_bits.append(address)
    if country:
        snippet_bits.append(country)
    if contact:
        snippet_bits.append(f"Contact: {contact}")
    snippet = " — ".join(snippet_bits)

    extras: dict[str, Any] = {
        "registrant_id": registrant_id,
        "name": name,
        "address": address,
        "country": country,
        "contact": contact,
    }
    # Registrants don't have a stable public detail page on lda.senate.gov;
    # link to the filings list filtered by registrant id so an operator can
    # eyeball recent activity.
    url = f"{_SITE_BASE}/system/public/filings/?registrant_id={registrant_id}" if registrant_id else _SITE_BASE
    return SearchResult(
        url=url,
        title=name,
        snippet=snippet,
        published_at=None,
        source_kind="lda",
        extras=extras,
    )


def _build_contribution_result(hit: dict[str, Any]) -> SearchResult | None:
    filing_uuid = (hit.get("filing_uuid") or "").strip()
    filer_name = _name_of(hit.get("filer")) or (hit.get("filer_name") or "").strip()
    if not filing_uuid and not filer_name:
        return None
    filer_type = (
        hit.get("filer_type_display") or hit.get("filer_type") or ""
    ).strip()
    contribution_total = hit.get("contributions_total") or hit.get("contribution_total")
    filing_year = hit.get("filing_year")
    filing_period_raw = (
        hit.get("filing_period_display") or hit.get("filing_period") or ""
    )
    filing_period = str(filing_period_raw).strip()
    dt_posted = hit.get("dt_posted")

    title_bits = [b for b in (filer_name, "LD-203", str(filing_year or ""), filing_period) if b]
    title = " – ".join(title_bits) if title_bits else (filer_name or "LD-203 contribution report")

    snippet_bits: list[str] = []
    if filer_type:
        snippet_bits.append(filer_type)
    if contribution_total not in (None, "", "null"):
        snippet_bits.append(f"Total contributions: {_fmt_money(contribution_total)}")
    snippet = " — ".join(snippet_bits)

    url = _filing_permalink(filing_uuid) if filing_uuid else _SITE_BASE

    extras: dict[str, Any] = {
        "filing_uuid": filing_uuid,
        "filer_type": filer_type,
        "filer_name": filer_name,
        "contribution_total": contribution_total,
        "filing_year": filing_year,
        "filing_period": filing_period,
    }
    return SearchResult(
        url=url,
        title=title,
        snippet=snippet,
        published_at=_parse_iso_date(dt_posted),
        source_kind="lda",
        extras=extras,
    )


_KIND_TO_ENDPOINT_AND_QPARAM: dict[str, tuple[str, str]] = {
    "filings": ("filings/", "registrant_name"),
    "registrants": ("registrants/", "name"),
    "contributions": ("contributions/", "filer_name"),
}

_KIND_TO_BUILDER = {
    "filings": _build_filing_result,
    "registrants": _build_registrant_result,
    "contributions": _build_contribution_result,
}


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def search(
    query: str,
    *,
    kind: str = "filings",
    max_results: int = 20,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Run an LDA search and return up to ``max_results`` hits.

    ``kind`` selects the index — ``filings`` (LD-1/LD-2 quarterly, query goes
    to ``registrant_name``), ``registrants`` (firm / in-house, query goes to
    ``name``) or ``contributions`` (LD-203, query goes to ``filer_name``).

    Returns ``[]`` on transport / HTTP error / non-JSON body or unknown
    ``kind`` — connector failures must never crash the planner.
    """
    if kind not in _VALID_KINDS:
        logger.warning(
            "lda.search: unknown kind %r; expected one of %s",
            kind,
            sorted(_VALID_KINDS),
        )
        return []

    endpoint, qparam = _KIND_TO_ENDPOINT_AND_QPARAM[kind]
    builder = _KIND_TO_BUILDER[kind]

    params: dict[str, Any] = {
        qparam: query,
        "page_size": max_results,
    }

    await _rate_limit_gate()

    url = urljoin(_BASE_URL, endpoint)
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("lda search failed for %r (%s): %s", query, kind, exc)
        return []

    if response.status_code != 200:
        logger.warning(
            "lda search returned HTTP %s for %r (%s)",
            response.status_code,
            query,
            kind,
        )
        return []

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("lda search returned non-JSON for %r: %s", query, exc)
        return []

    raw_hits = payload.get("results") or []
    out: list[SearchResult] = []
    for hit in raw_hits[:max_results]:
        if not isinstance(hit, dict):
            continue
        result = builder(hit)
        if result is not None:
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def _classify_url(url: str) -> str | None:
    """Return the filing UUID when ``url`` is an LDA filing detail page.

    Strict host match against ``_ACCEPTED_HOSTS`` so look-alikes like
    ``lda.senate.gov.attacker.example`` are rejected.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    if host not in _ACCEPTED_HOSTS:
        return None
    m = _FILING_URL_RE.match(parsed.path or "")
    if not m:
        return None
    return m.group("uuid")


async def _http_get_json(
    url: str, timeout: float
) -> tuple[int | None, dict[str, Any] | None]:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("lda fetch failed for %s: %s", url, exc)
        return None, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


def _activities_block(activities: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(activities, list) or not activities:
        return "", []
    rows: list[dict[str, Any]] = []
    for act in activities:
        if not isinstance(act, dict):
            continue
        code = (
            act.get("general_issue_code_display")
            or act.get("general_issue_code")
            or ""
        ).strip()
        description = (act.get("description") or "").strip()
        rows.append({"code": code, "description": description})
    if not rows:
        return "", rows
    lines = ["## Issues lobbied", ""]
    for r in rows:
        if r["code"] and r["description"]:
            lines.append(f"- **{r['code']}** — {r['description']}")
        elif r["code"]:
            lines.append(f"- **{r['code']}**")
        elif r["description"]:
            lines.append(f"- {r['description']}")
    return "\n".join(lines), rows


def _lobbyists_block(activities: Any) -> tuple[str, list[str]]:
    """Pull lobbyist names off the per-activity ``lobbyists[]`` lists.

    The LDA schema nests lobbyists under each activity (since covered
    positions and new-hire flags vary per issue), so we de-duplicate names
    across activities to give the operator a single roster line.
    """
    if not isinstance(activities, list):
        return "", []
    names: list[str] = []
    seen: set[str] = set()
    for act in activities:
        if not isinstance(act, dict):
            continue
        for entry in act.get("lobbyists") or []:
            if not isinstance(entry, dict):
                continue
            person = entry.get("lobbyist") or {}
            if not isinstance(person, dict):
                continue
            first = (person.get("first_name") or "").strip()
            last = (person.get("last_name") or "").strip()
            full = " ".join(p for p in (first, last) if p).strip()
            if full and full not in seen:
                seen.add(full)
                names.append(full)
    if not names:
        return "", names
    lines = ["## Lobbyists", ""]
    for name in names:
        lines.append(f"- {name}")
    return "\n".join(lines), names


def _amount_block(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    income = payload.get("income")
    expenses = payload.get("expenses")
    has_amount = income not in (None, "", "null") or expenses not in (None, "", "null")
    if not has_amount:
        return "", {"income": income, "expenses": expenses}
    lines = ["## Amount", ""]
    if income not in (None, "", "null"):
        lines.append(f"- **Income:** {_fmt_money(income)}")
    if expenses not in (None, "", "null"):
        lines.append(f"- **Expenses:** {_fmt_money(expenses)}")
    return "\n".join(lines), {"income": income, "expenses": expenses}


async def fetch(url: str, timeout: float = 30.0) -> Source | None:
    """Open an LD-2 filing detail page and return a :class:`Source`.

    Returns ``None`` for anything outside ``_ACCEPTED_HOSTS`` or paths other
    than ``/filings/<uuid>/`` (with or without the ``/api/v1/`` prefix), and
    for any transport / HTTP / parse failure.
    """
    if not url:
        return None
    filing_uuid = _classify_url(url)
    if not filing_uuid:
        return None

    api_url = urljoin(_BASE_URL, f"filings/{filing_uuid}/")
    await _rate_limit_gate()
    status, payload = await _http_get_json(api_url, timeout)
    if status is None or status >= 400 or not isinstance(payload, dict):
        if status is not None and status >= 400:
            logger.warning("lda filing HTTP %s for %s", status, api_url)
        return None

    client_name = _name_of(payload.get("client"))
    registrant_name = _name_of(payload.get("registrant"))
    filing_type = (
        payload.get("filing_type_display") or payload.get("filing_type") or ""
    ).strip()
    filing_year = payload.get("filing_year")
    filing_period = (
        payload.get("filing_period_display") or payload.get("filing_period") or ""
    ).strip()

    title_bits = [b for b in (client_name, filing_type, str(filing_year or ""), filing_period) if b]
    title = " – ".join(title_bits) if title_bits else f"LDA filing {filing_uuid}"

    activities = payload.get("lobbying_activities") or []
    issues_md, issues_rows = _activities_block(activities)
    lobbyists_md, lobbyist_names = _lobbyists_block(activities)
    amount_md, amount_extras = _amount_block(payload)

    meta_bits = [b for b in (registrant_name, filing_type, str(filing_year or ""), filing_period) if b]
    meta_line = "_" + " · ".join(meta_bits) + "_" if meta_bits else ""

    sections = [f"# {title}"]
    if meta_line:
        sections.append(meta_line)
    if issues_md:
        sections.append(issues_md)
    if lobbyists_md:
        sections.append(lobbyists_md)
    if amount_md:
        sections.append(amount_md)

    cleaned_text = "\n\n".join(sections).strip()
    if not cleaned_text:
        return None

    metadata: dict[str, Any] = {
        "filing_uuid": filing_uuid,
        "filing_type": filing_type,
        "filing_year": filing_year,
        "filing_period": filing_period,
        "client_name": client_name,
        "registrant_name": registrant_name,
        "income": amount_extras.get("income"),
        "expenses": amount_extras.get("expenses"),
        "issues": issues_rows,
        "lobbyists": lobbyist_names,
    }

    return Source(
        url=url,
        title=title,
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="lda",
        metadata=metadata,
    )


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


__all__ = ["fetch", "reset_for_tests", "search"]
