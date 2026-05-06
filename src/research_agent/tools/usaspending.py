"""USAspending.gov connector (issue #104).

Public surface:

* ``async def search(query, *, award_type=None, max_results=20) -> list[SearchResult]``
  hits the awards search endpoint at ``api.usaspending.gov/api/v2/`` for
  contracts, grants, loans, and indefinite-delivery vehicles. ``award_type``
  filters by category — ``contracts`` (codes A/B/C/D), ``grants`` (02/03/04/05),
  ``loans`` (07/08), or one of the literal IDV codes (``IDV_A``..``IDV_E``).
* ``async def fetch(url) -> Source | None`` opens an award profile and returns
  base award amount, modifications history (count + total), recipient details,
  parent NAICS / PSC, and period of performance.

Auth: none required. Per AC, the per-host gate is 0.5s (2 RPS).

The search endpoint is POST-based — clients send a JSON ``filters`` body
rather than query-string params — so this module ships its own ``_post()``
helper rather than reusing the GET helper used by sister connectors.

**Critical rule:** file analysis on the **base award** (``base_and_all_options_value``),
not on modifications. Modifications dilute the count — a $10M base award
with 30 mods isn't the same as 30 distinct $10M procurements. The connector
surfaces ``base_award_amount`` and ``modifications_count`` separately so the
synthesizer can keep the distinction.
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

_BASE_URL = "https://api.usaspending.gov/api/v2/"
_SITE_BASE = "https://www.usaspending.gov"
_ACCEPTED_HOSTS = frozenset(
    {"api.usaspending.gov", "usaspending.gov", "www.usaspending.gov"}
)
# AC: per-host 2 RPS.
_RATE_LIMIT_INTERVAL = 0.5

# AC enumerates four buckets the agent typically reasons about. ``contracts``
# / ``grants`` / ``loans`` are convenience aliases that expand to the
# underlying USAspending award_type_code list. The literal IDV_* codes are
# also accepted directly so callers can drill into a specific IDV variant
# (per AC: ``IDV_A`` flagged for no-bid synthesis).
_AWARD_TYPE_CODES: dict[str, list[str]] = {
    "contracts": ["A", "B", "C", "D"],
    "grants": ["02", "03", "04", "05"],
    "loans": ["07", "08"],
    "IDV_A": ["IDV_A"],
    "IDV_B": ["IDV_B"],
    "IDV_C": ["IDV_C"],
    "IDV_D": ["IDV_D"],
    "IDV_E": ["IDV_E"],
}

# Default fields requested from spending_by_award. USAspending's API uses
# human-readable column names here; ``Award ID``, ``Recipient Name``, etc.
_SEARCH_FIELDS = [
    "Award ID",
    "Recipient Name",
    "Recipient UEI",
    "Award Amount",
    "Description",
    "Contract Award Type",
    "Award Type",
    "Awarding Agency",
    "Awarding Sub Agency",
    "Action Date",
    "Period of Performance Start Date",
    "Period of Performance Current End Date",
    "NAICS",
    "psc",
    "extent_competed",
    "generated_internal_id",
]

# Award detail permalinks: /award/<generated_internal_id>/ — the id is opaque
# (CONT_AWD_..., ASST_NON_..., IDV_..., etc.) and may contain underscores,
# colons, hyphens, and dots. Tolerate trailing slash.
_AWARD_URL_RE = re.compile(r"^/award/(?P<id>[^/]+)/?$")

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


# ---------------------------------------------------------------------------
# Config / rate-limit helpers
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": config.get("RESEARCH_USER_AGENT")
        or "research-agent/0.1 (+local; contact unset)",
    }


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


def _award_permalink(award_id: str) -> str:
    return f"{_SITE_BASE}/award/{award_id}/"


def _agency_name(value: Any) -> str:
    """Pull a human name from either a dict-of-strings or a bare string."""
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    if value is None:
        return ""
    return str(value).strip()


# ---------------------------------------------------------------------------
# POST helper
# ---------------------------------------------------------------------------


async def _post(
    path: str, body: dict[str, Any], timeout: float
) -> tuple[int | None, dict[str, Any] | None]:
    """POST a JSON body to ``urljoin(_BASE_URL, path)`` after the rate gate.

    Returns ``(status_code, payload)``. Either side may be ``None`` on
    transport / parse failure; HTTP error statuses come back with payload
    ``None`` so callers can branch on the code.
    """
    await _rate_limit_gate()
    url = urljoin(_BASE_URL, path)
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.post(url, json=body)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("usaspending POST failed for %s: %s", path, exc)
        return None, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        return response.status_code, response.json()
    except ValueError as exc:
        logger.warning("usaspending POST returned non-JSON for %s: %s", path, exc)
        return response.status_code, None


async def _get(path: str, timeout: float) -> tuple[int | None, dict[str, Any] | None]:
    """GET ``urljoin(_BASE_URL, path)`` after the rate gate."""
    await _rate_limit_gate()
    url = urljoin(_BASE_URL, path)
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("usaspending GET failed for %s: %s", path, exc)
        return None, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        return response.status_code, response.json()
    except ValueError as exc:
        logger.warning("usaspending GET returned non-JSON for %s: %s", path, exc)
        return response.status_code, None


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


def _build_search_result(
    hit: dict[str, Any], *, award_type_filter: str | None
) -> SearchResult | None:
    generated_id = (hit.get("generated_internal_id") or "").strip()
    if not generated_id:
        return None

    recipient_name = (hit.get("Recipient Name") or "").strip()
    recipient_uei = (hit.get("Recipient UEI") or "").strip()
    award_amount = hit.get("Award Amount")
    contract_award_type = (hit.get("Contract Award Type") or "").strip()
    award_type = (hit.get("Award Type") or "").strip()
    awarding_agency = (hit.get("Awarding Agency") or "").strip()
    awarding_sub = (hit.get("Awarding Sub Agency") or "").strip()
    action_date = hit.get("Action Date")
    naics_code = (hit.get("NAICS") or "").strip()
    psc_code = (hit.get("psc") or hit.get("PSC") or "").strip()
    description = (hit.get("Description") or "").strip()
    extent_competed = hit.get("extent_competed")
    award_id = (hit.get("Award ID") or "").strip()

    type_label = contract_award_type or award_type
    title_bits = [b for b in (recipient_name, type_label) if b]
    title = " — ".join(title_bits) if title_bits else f"USAspending award {generated_id}"

    snippet_bits: list[str] = []
    if award_amount is not None:
        snippet_bits.append(_fmt_money(award_amount))
    if awarding_agency:
        snippet_bits.append(awarding_agency)
    if action_date:
        snippet_bits.append(str(action_date))
    if description:
        clip = description if len(description) <= 120 else description[:120] + "…"
        snippet_bits.append(clip)
    snippet = " — ".join(snippet_bits)

    # AC: flag IDV_A awards lacking a competitive marker for synthesis.
    is_idv_a = (
        award_type_filter == "IDV_A"
        or contract_award_type.upper() == "IDV_A"
        or type_label.upper() == "IDV_A"
    )
    if isinstance(extent_competed, str):
        competitive_present = bool(extent_competed.strip())
    else:
        competitive_present = extent_competed not in (None, "", "null")
    no_bid_flag = bool(is_idv_a) and not competitive_present

    extras: dict[str, Any] = {
        "generated_internal_id": generated_id,
        "award_id": award_id,
        "recipient_name": recipient_name,
        "recipient_uei": recipient_uei,
        "award_amount": award_amount,
        "award_type": type_label,
        "awarding_agency": awarding_agency,
        "awarding_sub_agency": awarding_sub,
        "action_date": action_date,
        "naics_code": naics_code,
        "psc_code": psc_code,
        "extent_competed": extent_competed,
        "description": description,
        "no_bid_flag": no_bid_flag,
    }

    return SearchResult(
        url=_award_permalink(generated_id),
        title=title,
        snippet=snippet,
        published_at=_parse_iso_date(action_date),
        source_kind="usaspending",
        extras=extras,
    )


async def search(
    query: str,
    *,
    award_type: str | None = None,
    max_results: int = 20,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Run a USAspending awards search and return up to ``max_results`` hits.

    ``award_type`` narrows the search: ``contracts`` (codes A/B/C/D),
    ``grants`` (02/03/04/05), ``loans`` (07/08), or one of the literal
    ``IDV_A``..``IDV_E`` codes. When ``None``, no type filter is applied
    and all award categories come back.

    Returns ``[]`` on transport / HTTP error / non-JSON body or unknown
    ``award_type`` — connector failures must never crash the planner.
    """
    if award_type is not None and award_type not in _AWARD_TYPE_CODES:
        logger.warning(
            "usaspending.search: unknown award_type %r; expected one of %s",
            award_type,
            sorted(_AWARD_TYPE_CODES),
        )
        return []

    filters: dict[str, Any] = {"keywords": [query]}
    if award_type is not None:
        filters["award_type_codes"] = list(_AWARD_TYPE_CODES[award_type])

    body: dict[str, Any] = {
        "filters": filters,
        "fields": list(_SEARCH_FIELDS),
        "page": 1,
        "limit": max_results,
        "sort": "Action Date",
        "order": "desc",
    }

    status, payload = await _post("search/spending_by_award/", body, timeout)
    if status is None or status != 200 or not isinstance(payload, dict):
        if status is not None and status != 200:
            logger.warning(
                "usaspending search returned HTTP %s for %r (%s)",
                status,
                query,
                award_type,
            )
        return []

    raw_hits = payload.get("results") or []
    out: list[SearchResult] = []
    for hit in raw_hits[:max_results]:
        if not isinstance(hit, dict):
            continue
        result = _build_search_result(hit, award_type_filter=award_type)
        if result is not None:
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def _classify_url(url: str) -> str | None:
    """Return the generated_internal_id when ``url`` is a USAspending award page.

    Strict host match against ``_ACCEPTED_HOSTS`` so look-alikes like
    ``usaspending.gov.attacker.example`` are rejected.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    if host not in _ACCEPTED_HOSTS:
        return None
    m = _AWARD_URL_RE.match(parsed.path or "")
    if not m:
        return None
    return m.group("id")


def _base_award_amount(payload: dict[str, Any]) -> Any:
    """Pull the base award value, preferring ``base_and_all_options_value``.

    Per AC, file analysis runs on this number — *not* on ``total_obligation``,
    which folds in modifications and double-counts when a base award is
    repeatedly extended.
    """
    for key in ("base_and_all_options_value", "base_exercised_options_val"):
        v = payload.get(key)
        if v not in (None, "", "null"):
            return v
    return None


def _modifications_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``{count, total}`` for the modifications history.

    Uses fields available in the award detail payload itself — staying off
    the transactions endpoint per the plan ("keep to award detail payload
    fields"). ``count`` falls back to ``parent_award.modification_count``
    when present; ``total`` is ``total_obligation`` minus the base award
    when both are populated.
    """
    parent = payload.get("parent_award")
    count: int | None = None
    if isinstance(parent, dict):
        v = parent.get("modification_count")
        if isinstance(v, int):
            count = v
        elif isinstance(v, str) and v.isdigit():
            count = int(v)
    total_obligation = payload.get("total_obligation")
    base = _base_award_amount(payload)
    mod_total: float | None = None
    if total_obligation not in (None, "", "null") and base not in (None, "", "null"):
        try:
            mod_total = float(total_obligation) - float(base)
        except (TypeError, ValueError):
            mod_total = None
    return {"count": count, "total": mod_total}


def _recipient_block(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    recipient = payload.get("recipient")
    if not isinstance(recipient, dict):
        return "", {}
    name = (recipient.get("recipient_name") or "").strip()
    uei = (recipient.get("recipient_uei") or recipient.get("recipient_unique_id") or "").strip()
    parent_uei = (
        recipient.get("parent_recipient_uei")
        or recipient.get("parent_recipient_unique_id")
        or ""
    ).strip()
    parent_name = (recipient.get("parent_recipient_name") or "").strip()
    location = recipient.get("location")
    location_str = ""
    if isinstance(location, dict):
        bits = [
            (location.get("address_line1") or "").strip(),
            (location.get("city_name") or "").strip(),
            (location.get("state_code") or "").strip(),
            (location.get("zip5") or "").strip(),
        ]
        location_str = ", ".join(b for b in bits if b)

    if not name:
        return "", {}

    lines = ["## Recipient", ""]
    lines.append(f"- **Name:** {name}")
    if uei:
        lines.append(f"- **UEI:** {uei}")
    if parent_name or parent_uei:
        parent_label = parent_name or parent_uei
        if parent_name and parent_uei:
            parent_label = f"{parent_name} ({parent_uei})"
        lines.append(f"- **Parent recipient:** {parent_label}")
    if location_str:
        lines.append(f"- **Location:** {location_str}")
    extras = {
        "name": name,
        "uei": uei,
        "parent_uei": parent_uei,
        "parent_name": parent_name,
        "location": location_str,
    }
    return "\n".join(lines), extras


def _naics_psc_block(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    naics = payload.get("naics_hierarchy")
    psc = payload.get("psc_hierarchy")
    naics_top_code = ""
    naics_top_desc = ""
    psc_top_code = ""
    psc_top_desc = ""
    if isinstance(naics, dict):
        naics_top_code = (naics.get("toptier_code") or "").strip()
        naics_top_desc = (naics.get("toptier_description") or "").strip()
    if isinstance(psc, dict):
        psc_top_code = (psc.get("toptier_code") or "").strip()
        psc_top_desc = (psc.get("toptier_description") or "").strip()

    if not (naics_top_code or psc_top_code):
        return "", {}

    lines = ["## Classification", ""]
    if naics_top_code:
        label = (
            f"{naics_top_code} — {naics_top_desc}" if naics_top_desc else naics_top_code
        )
        lines.append(f"- **Parent NAICS:** {label}")
    if psc_top_code:
        label = (
            f"{psc_top_code} — {psc_top_desc}" if psc_top_desc else psc_top_code
        )
        lines.append(f"- **Parent PSC:** {label}")
    extras = {
        "parent_naics_code": naics_top_code,
        "parent_naics_description": naics_top_desc,
        "parent_psc_code": psc_top_code,
        "parent_psc_description": psc_top_desc,
    }
    return "\n".join(lines), extras


def _period_of_performance_block(
    payload: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    pop = payload.get("period_of_performance")
    if not isinstance(pop, dict):
        return "", {}
    start = (pop.get("start_date") or "").strip()
    end = (
        pop.get("end_date")
        or pop.get("last_modified_date")
        or ""
    )
    end = end.strip() if isinstance(end, str) else ""
    if not (start or end):
        return "", {}
    lines = ["## Period of performance", ""]
    if start:
        lines.append(f"- **Start:** {start}")
    if end:
        lines.append(f"- **End:** {end}")
    return "\n".join(lines), {"start": start, "end": end}


def _amount_block(
    payload: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    base = _base_award_amount(payload)
    total_obligation = payload.get("total_obligation")
    mods = _modifications_summary(payload)
    has_anything = base is not None or total_obligation is not None or mods["count"] is not None
    if not has_anything:
        return "", {}
    lines = ["## Amount", ""]
    lines.append(
        f"- **Base award:** {_fmt_money(base)} "
        "*(file analysis on this value, not modifications)*"
    )
    if total_obligation not in (None, "", "null"):
        lines.append(f"- **Total obligated:** {_fmt_money(total_obligation)}")
    count = mods["count"]
    if count is not None:
        lines.append(f"- **Modifications:** {count}")
    if mods["total"] is not None:
        lines.append(f"- **Modifications total:** {_fmt_money(mods['total'])}")
    extras = {
        "base_award_amount": base,
        "total_obligation": total_obligation,
        "modifications_count": count,
        "modifications_total": mods["total"],
    }
    return "\n".join(lines), extras


async def fetch(url: str, timeout: float = 30.0) -> Source | None:
    """Open an award profile and return a :class:`Source`.

    Returns ``None`` for anything outside ``_ACCEPTED_HOSTS`` or paths other
    than ``/award/<generated_internal_id>/``, and for any transport / HTTP /
    parse failure.

    Important: the metadata's ``base_award_amount`` is the value the
    synthesizer should use for file analysis. Modifications dilute the count
    and are surfaced separately under ``modifications_count`` /
    ``modifications_total``.
    """
    if not url:
        return None
    award_id = _classify_url(url)
    if not award_id:
        return None

    status, payload = await _get(f"awards/{award_id}/", timeout)
    if status is None or status != 200 or not isinstance(payload, dict):
        if status is not None and status != 200:
            logger.warning("usaspending award HTTP %s for %s", status, url)
        return None

    type_description = (payload.get("type_description") or "").strip()
    description = (payload.get("description") or "").strip()
    piid = (payload.get("piid") or "").strip()
    fain = (payload.get("fain") or "").strip()
    uri = (payload.get("uri") or "").strip()
    category = (payload.get("category") or "").strip()
    award_type_code = (payload.get("type") or "").strip()

    awarding_agency = _agency_name(
        (payload.get("awarding_agency") or {}).get("toptier_agency")
        if isinstance(payload.get("awarding_agency"), dict)
        else None
    )

    recipient_md, recipient_extras = _recipient_block(payload)
    classification_md, classification_extras = _naics_psc_block(payload)
    pop_md, pop_extras = _period_of_performance_block(payload)
    amount_md, amount_extras = _amount_block(payload)

    title_name = recipient_extras.get("name") or "USAspending award"
    title_bits = [title_name]
    if type_description:
        title_bits.append(type_description)
    if piid:
        title_bits.append(piid)
    elif fain:
        title_bits.append(fain)
    elif uri:
        title_bits.append(uri)
    title = " — ".join(b for b in title_bits if b)

    meta_bits = [b for b in (category, type_description, awarding_agency) if b]
    meta_line = "_" + " · ".join(meta_bits) + "_" if meta_bits else ""

    sections = [f"# {title}"]
    if meta_line:
        sections.append(meta_line)
    if amount_md:
        sections.append(amount_md)
    if recipient_md:
        sections.append(recipient_md)
    if classification_md:
        sections.append(classification_md)
    if pop_md:
        sections.append(pop_md)
    if description:
        sections.append("## Description\n\n" + description)

    cleaned_text = "\n\n".join(sections).strip()
    if not cleaned_text:
        return None

    metadata: dict[str, Any] = {
        "generated_internal_id": award_id,
        "piid": piid,
        "fain": fain,
        "uri": uri,
        "category": category,
        "award_type_code": award_type_code,
        "type_description": type_description,
        "awarding_agency": awarding_agency,
        "base_award_amount": amount_extras.get("base_award_amount"),
        "total_obligation": amount_extras.get("total_obligation"),
        "modifications_count": amount_extras.get("modifications_count"),
        "modifications_total": amount_extras.get("modifications_total"),
        "recipient": recipient_extras,
        "parent_naics": {
            "code": classification_extras.get("parent_naics_code", ""),
            "description": classification_extras.get("parent_naics_description", ""),
        },
        "parent_psc": {
            "code": classification_extras.get("parent_psc_code", ""),
            "description": classification_extras.get("parent_psc_description", ""),
        },
        "period_of_performance_start": pop_extras.get("start", ""),
        "period_of_performance_end": pop_extras.get("end", ""),
    }

    return Source(
        url=url,
        title=title,
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="usaspending",
        metadata=metadata,
    )


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


__all__ = ["fetch", "reset_for_tests", "search"]
