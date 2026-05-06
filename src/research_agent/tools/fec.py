"""FEC OpenFEC connector (issue #94).

Public surface:

* ``async def search(query, *, kind="candidates", max_results=20) -> list[SearchResult]``
  hits ``api.open.fec.gov/v1/`` for candidates, committees, individual contributions
  (``schedules/schedule_a``) or independent expenditures (``schedules/schedule_e``).
* ``async def fetch(url) -> Source | None`` opens a candidate or committee detail
  page on ``www.fec.gov/data/`` and returns rolled-up cycle totals, top donors,
  and top expenditures.

Auth: api.data.gov key in ``DATA_GOV_API_KEY`` (free signup at
https://api.data.gov/signup/). Authenticated tier is 1,000 req/hr; falling back
to ``DEMO_KEY`` works for smoke but is throttled to ~40 req/hr per IP and emits
a warning. Key is passed via the ``api_key`` query parameter.

Per AC, response JSON is cached at ``corpus/.cache/fec/<kind>-<id>.json`` with a
1-hour TTL (mtime check) — bulk files refresh daily upstream and the ProPublica
real-time mirror is 15-min stale, so an hour is a safe ceiling.

For bulk historical analysis (donors-by-state, FECfile-grade dumps) point
operators at https://www.fec.gov/data/browse-data/ — the connector itself
stays REST-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from research_agent import config
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.open.fec.gov/v1/"
_SITE_BASE = "https://www.fec.gov/data/"
# 1000 req/hr authenticated => ~3.6s; round to 4s for headroom. Anonymous
# DEMO_KEY only practical for smoke (~40 req/hr per IP).
_RATE_LIMIT_INTERVAL = 4.0
_CACHE_DIR = Path("corpus/.cache/fec")
# Per AC: 1-hour TTL — bulk files refresh daily; ProPublica real-time mirror
# is 15-min stale; an hour is a safe ceiling.
_CACHE_TTL = 3600.0

_VALID_KINDS = {
    "candidates",
    "committees",
    "schedules/schedule_a",
    "schedules/schedule_e",
}

# Candidate IDs: letter prefix (H/S/P) + 8 digits typically; allow alphanumeric.
# Committee IDs: C + 8 digits typically; allow alphanumeric to be tolerant.
_CANDIDATE_URL_RE = re.compile(
    r"^/data/candidate/(?P<id>[A-Za-z0-9]+)/?$"
)
_COMMITTEE_URL_RE = re.compile(
    r"^/data/committee/(?P<id>[A-Za-z0-9]+)/?$"
)

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


# ---------------------------------------------------------------------------
# Config / rate-limit helpers
# ---------------------------------------------------------------------------


def _resolve_api_key() -> str:
    """Return the configured api.data.gov key, falling back to DEMO_KEY.

    DEMO_KEY is throttled to ~40 req/hr per IP — fine for `_smoke-tool` runs
    but it will fall over under any real workload, so emit a warning so
    operators see why the connector mysteriously starts returning [].
    """
    raw = config.get("DATA_GOV_API_KEY") or ""
    key = raw.strip()
    if not key:
        logger.warning(
            "DATA_GOV_API_KEY not set — falling back to DEMO_KEY (40 req/hr "
            "per IP). Sign up at https://api.data.gov/signup/ for the 1000 "
            "req/hr authenticated tier."
        )
        return "DEMO_KEY"
    return key


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
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
    try:
        # Allow floats in totals (api returns floats); render as integer dollars.
        return f"${int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "—"


def _candidate_url(candidate_id: str) -> str:
    return f"{_SITE_BASE}candidate/{candidate_id}/"


def _committee_url(committee_id: str) -> str:
    return f"{_SITE_BASE}committee/{committee_id}/"


# ---------------------------------------------------------------------------
# search() result builders
# ---------------------------------------------------------------------------


def _build_candidate_result(hit: dict[str, Any]) -> SearchResult | None:
    cand_id = (hit.get("candidate_id") or "").strip()
    name = (hit.get("name") or "").strip()
    if not cand_id or not name:
        return None
    party = (hit.get("party") or "").strip()
    state = (hit.get("state") or "").strip()
    office = (hit.get("office_full") or hit.get("office") or "").strip()
    incumbent = (hit.get("incumbent_challenge_full") or "").strip()
    election_years = hit.get("election_years") or []
    if not isinstance(election_years, list):
        election_years = []

    bits = [name]
    if party:
        bits.append(party)
    if state:
        bits.append(state)
    if office:
        bits.append(office)
    if incumbent:
        bits.append(incumbent)
    snippet = " — ".join(bits)

    extras: dict[str, Any] = {
        "candidate_id": cand_id,
        "party": party,
        "state": state,
        "office": office,
        "office_full": office,
        "incumbent_challenge_full": incumbent,
        "election_years": election_years,
    }
    return SearchResult(
        url=_candidate_url(cand_id),
        title=name,
        snippet=snippet,
        published_at=None,
        source_kind="fec",
        extras=extras,
    )


def _build_committee_result(hit: dict[str, Any]) -> SearchResult | None:
    com_id = (hit.get("committee_id") or "").strip()
    name = (hit.get("name") or "").strip()
    if not com_id or not name:
        return None
    committee_type = (hit.get("committee_type_full") or "").strip()
    designation = (hit.get("designation_full") or "").strip()
    org_type = (hit.get("organization_type_full") or hit.get("organization_type") or "").strip()
    party = (hit.get("party_full") or hit.get("party") or "").strip()
    state = (hit.get("state") or "").strip()

    bits = [name]
    if committee_type:
        bits.append(committee_type)
    if designation:
        bits.append(designation)
    if state:
        bits.append(state)
    snippet = " — ".join(bits)

    extras: dict[str, Any] = {
        "committee_id": com_id,
        "committee_type_full": committee_type,
        "designation_full": designation,
        "organization_type": org_type,
        "party": party,
        "state": state,
    }
    return SearchResult(
        url=_committee_url(com_id),
        title=name,
        snippet=snippet,
        published_at=None,
        source_kind="fec",
        extras=extras,
    )


def _build_schedule_a_result(hit: dict[str, Any]) -> SearchResult | None:
    contributor = (hit.get("contributor_name") or "").strip()
    amount = hit.get("contribution_receipt_amount")
    committee_field = hit.get("committee")
    if isinstance(committee_field, dict):
        recipient = (committee_field.get("name") or "").strip()
    else:
        recipient = ""
    if not recipient:
        recipient = (hit.get("recipient_name") or "").strip()
    receipt_date = hit.get("contribution_receipt_date")
    employer = (hit.get("contributor_employer") or "").strip()
    occupation = (hit.get("contributor_occupation") or "").strip()
    com_id = (hit.get("committee_id") or "").strip()

    if not contributor and not amount:
        return None

    title = contributor or recipient or "FEC contribution"
    snippet_bits = []
    if contributor:
        snippet_bits.append(contributor)
    if amount is not None:
        snippet_bits.append(_fmt_money(amount))
    if recipient:
        snippet_bits.append(f"→ {recipient}")
    if receipt_date:
        snippet_bits.append(str(receipt_date))
    snippet = " — ".join(snippet_bits)

    # Schedule A query echoes the contributor name so an operator clicking the
    # url lands on a filtered FEC site search rather than a raw committee page.
    base = f"{_SITE_BASE}receipts/"
    qs = f"?contributor_name={contributor.replace(' ', '+')}" if contributor else ""
    url = base + qs

    extras: dict[str, Any] = {
        "amount": amount,
        "contributor_name": contributor,
        "contributor_employer": employer,
        "contributor_occupation": occupation,
        "recipient_name": recipient,
        "committee_id": com_id,
        "contribution_receipt_date": receipt_date,
    }
    return SearchResult(
        url=url,
        title=str(title),
        snippet=snippet,
        published_at=_parse_iso_date(receipt_date),
        source_kind="fec",
        extras=extras,
    )


def _build_schedule_e_result(hit: dict[str, Any]) -> SearchResult | None:
    payee = (hit.get("payee_name") or "").strip()
    amount = hit.get("expenditure_amount")
    candidate = (hit.get("candidate_name") or "").strip()
    expenditure_date = hit.get("expenditure_date")
    indicator = (hit.get("support_oppose_indicator") or "").strip()
    com_id = (hit.get("committee_id") or "").strip()

    if not payee and not amount:
        return None

    title = payee or "FEC independent expenditure"
    indicator_label = (
        "Support"
        if indicator.upper() == "S"
        else "Oppose"
        if indicator.upper() == "O"
        else (indicator or "?")
    )
    snippet_bits: list[str] = []
    if payee:
        snippet_bits.append(payee)
    if amount is not None:
        snippet_bits.append(_fmt_money(amount))
    if candidate:
        snippet_bits.append(f"{indicator_label} {candidate}")
    if expenditure_date:
        snippet_bits.append(str(expenditure_date))
    snippet = " — ".join(snippet_bits)

    url = _committee_url(com_id) if com_id else f"{_SITE_BASE}independent-expenditures/"

    extras: dict[str, Any] = {
        "expenditure_amount": amount,
        "payee_name": payee,
        "candidate_name": candidate,
        "support_oppose_indicator": indicator,
        "expenditure_date": expenditure_date,
        "committee_id": com_id,
    }
    return SearchResult(
        url=url,
        title=str(title),
        snippet=snippet,
        published_at=_parse_iso_date(expenditure_date),
        source_kind="fec",
        extras=extras,
    )


_KIND_TO_ENDPOINT_AND_QPARAM: dict[str, tuple[str, str]] = {
    "candidates": ("candidates/search/", "q"),
    "committees": ("committees/", "q"),
    "schedules/schedule_a": ("schedules/schedule_a/", "contributor_name"),
    "schedules/schedule_e": ("schedules/schedule_e/", "payee_name"),
}

_KIND_TO_BUILDER = {
    "candidates": _build_candidate_result,
    "committees": _build_committee_result,
    "schedules/schedule_a": _build_schedule_a_result,
    "schedules/schedule_e": _build_schedule_e_result,
}


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def search(
    query: str,
    *,
    kind: str = "candidates",
    max_results: int = 20,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Run an OpenFEC search and return up to ``max_results`` hits.

    ``kind`` selects the index — ``candidates``, ``committees``,
    ``schedules/schedule_a`` (individual contributions, query goes to
    ``contributor_name``) or ``schedules/schedule_e`` (independent
    expenditures, query goes to ``payee_name``).

    Returns ``[]`` on transport / HTTP error / non-JSON body or unknown
    ``kind`` — connector failures must never crash the planner.
    """
    if kind not in _VALID_KINDS:
        logger.warning(
            "fec.search: unknown kind %r; expected one of %s",
            kind,
            sorted(_VALID_KINDS),
        )
        return []

    endpoint, qparam = _KIND_TO_ENDPOINT_AND_QPARAM[kind]
    builder = _KIND_TO_BUILDER[kind]

    params: dict[str, Any] = {
        "api_key": _resolve_api_key(),
        "per_page": max_results,
        qparam: query,
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
        logger.warning("fec search failed for %r (%s): %s", query, kind, exc)
        return []

    if response.status_code != 200:
        logger.warning(
            "fec search returned HTTP %s for %r (%s)",
            response.status_code,
            query,
            kind,
        )
        return []

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("fec search returned non-JSON for %r: %s", query, exc)
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


def _classify_url(url: str) -> tuple[str | None, str | None]:
    """Return ``(resource, id)`` where resource ∈ {"candidate","committee"}.

    Anything outside ``www.fec.gov`` (strict host match — look-alikes like
    ``www.fec.gov.attacker.example`` must not pass) returns ``(None, None)``.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    if host != "www.fec.gov":
        return None, None
    path = parsed.path or ""
    m = _CANDIDATE_URL_RE.match(path)
    if m:
        return "candidate", m.group("id")
    m = _COMMITTEE_URL_RE.match(path)
    if m:
        return "committee", m.group("id")
    return None, None


def _cache_path(prefix: str, resource_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9]", "_", resource_id)
    return _CACHE_DIR / f"{prefix}-{safe}.json"


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def _load_cache(path: Path) -> dict[str, Any] | None:
    """Return cached payload if present and within TTL; else delete + None."""
    if not path.exists():
        return None
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age > _CACHE_TTL:
        # Stale — drop it so the next call re-fetches.
        try:
            path.unlink()
        except OSError:
            pass
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _http_get_json(
    url: str, timeout: float, *, params: dict[str, Any] | None = None
) -> tuple[int | None, dict[str, Any] | None]:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("fec fetch failed for %s: %s", url, exc)
        return None, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


def _latest_cycle(values: Any) -> int | None:
    if not isinstance(values, list):
        return None
    cycles: list[int] = []
    for v in values:
        try:
            cycles.append(int(v))
        except (TypeError, ValueError):
            continue
    return max(cycles) if cycles else None


def _cycle_totals_block(
    totals: dict[str, Any] | None, *, cycle: int | None
) -> tuple[str, dict[str, Any]]:
    if not totals:
        return "", {}
    receipts = totals.get("receipts")
    disb = totals.get("disbursements")
    coh = (
        totals.get("cash_on_hand_end_period")
        or totals.get("last_cash_on_hand_end_period")
    )
    rows = [
        ("Receipts", _fmt_money(receipts)),
        ("Disbursements", _fmt_money(disb)),
        ("Cash on hand (end)", _fmt_money(coh)),
    ]
    header = f"## Cycle totals ({cycle})" if cycle else "## Cycle totals"
    lines = [header, ""]
    for label, value in rows:
        lines.append(f"- **{label}:** {value}")
    md = "\n".join(lines)
    extras = {
        "cycle": cycle,
        "receipts": receipts,
        "disbursements": disb,
        "cash_on_hand_end_period": coh,
    }
    return md, extras


def _top_donors_block(
    payload: dict[str, Any] | None,
) -> tuple[str, list[dict[str, Any]]]:
    if not payload:
        return "", []
    raw = payload.get("results") or []
    rows: list[dict[str, Any]] = []
    for r in raw[:10]:
        if not isinstance(r, dict):
            continue
        label = (
            r.get("employer")
            or r.get("contributor_employer")
            or r.get("size")
            or "?"
        )
        total = r.get("total")
        rows.append({"label": str(label), "total": total})
    if not rows:
        return "", rows
    lines = ["## Top donors", ""]
    for r in rows:
        lines.append(f"- {r['label']} — {_fmt_money(r['total'])}")
    return "\n".join(lines), rows


def _top_expenditures_block(
    payload: dict[str, Any] | None,
) -> tuple[str, list[dict[str, Any]]]:
    if not payload:
        return "", []
    raw = payload.get("results") or []
    rows: list[dict[str, Any]] = []
    for r in raw[:10]:
        if not isinstance(r, dict):
            continue
        label = (
            r.get("purpose")
            or r.get("category_full")
            or r.get("recipient_name")
            or r.get("disbursement_purpose_category")
            or "?"
        )
        total = r.get("total")
        rows.append({"label": str(label), "total": total})
    if not rows:
        return "", rows
    lines = ["## Top expenditures", ""]
    for r in rows:
        lines.append(f"- {r['label']} — {_fmt_money(r['total'])}")
    return "\n".join(lines), rows


async def _fetch_json_cached(
    cache_key_prefix: str,
    cache_id: str,
    api_url: str,
    timeout: float,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Fetch JSON, caching at ``corpus/.cache/fec/<prefix>-<id>.json`` for 1h."""
    cache = _cache_path(cache_key_prefix, cache_id)
    payload = _load_cache(cache)
    if payload is not None:
        return payload
    await _rate_limit_gate()
    status, payload = await _http_get_json(api_url, timeout, params=params)
    if status is None or status >= 400 or not isinstance(payload, dict):
        if status is not None and status >= 400:
            logger.warning("fec %s HTTP %s for %s", cache_key_prefix, status, api_url)
        return None
    _write_cache(cache, payload)
    return payload


def _first_result(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return None
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    return first if isinstance(first, dict) else None


async def _fetch_candidate(
    candidate_id: str, source_url: str, timeout: float
) -> Source | None:
    common_params = {"api_key": _resolve_api_key()}

    header_payload = await _fetch_json_cached(
        "candidate",
        candidate_id,
        urljoin(_BASE_URL, f"candidate/{candidate_id}/"),
        timeout,
        params=common_params,
    )
    header = _first_result(header_payload)
    if not header:
        return None

    name = (header.get("name") or "").strip()
    if not name:
        return None
    cycle = _latest_cycle(header.get("election_years"))

    totals_params = dict(common_params)
    if cycle is not None:
        totals_params["cycle"] = cycle
    totals_payload = await _fetch_json_cached(
        "candidate-totals",
        f"{candidate_id}-{cycle}" if cycle else candidate_id,
        urljoin(_BASE_URL, f"candidate/{candidate_id}/totals/"),
        timeout,
        params=totals_params,
    )
    totals = _first_result(totals_payload)

    donors_params = dict(common_params)
    donors_params.update(
        {"candidate_id": candidate_id, "per_page": 10, "sort": "-total"}
    )
    donors_payload = await _fetch_json_cached(
        "candidate-donors",
        f"{candidate_id}-{cycle}" if cycle else candidate_id,
        urljoin(_BASE_URL, "schedules/schedule_a/by_employer/"),
        timeout,
        params=donors_params,
    )

    purposes_params = dict(common_params)
    purposes_params.update(
        {"candidate_id": candidate_id, "per_page": 10, "sort": "-total"}
    )
    purposes_payload = await _fetch_json_cached(
        "candidate-purposes",
        f"{candidate_id}-{cycle}" if cycle else candidate_id,
        urljoin(_BASE_URL, "schedules/schedule_b/by_purpose/"),
        timeout,
        params=purposes_params,
    )

    totals_md, totals_extras = _cycle_totals_block(totals, cycle=cycle)
    donors_md, donors_rows = _top_donors_block(donors_payload)
    spend_md, spend_rows = _top_expenditures_block(purposes_payload)

    party = (header.get("party_full") or header.get("party") or "").strip()
    state = (header.get("state") or "").strip()
    office = (header.get("office_full") or header.get("office") or "").strip()
    incumbent = (header.get("incumbent_challenge_full") or "").strip()
    meta_bits = [b for b in (party, state, office, incumbent) if b]
    meta_line = "_" + " · ".join(meta_bits) + "_" if meta_bits else ""

    sections = [f"# {name}"]
    if meta_line:
        sections.append(meta_line)
    if totals_md:
        sections.append(totals_md)
    if donors_md:
        sections.append(donors_md)
    if spend_md:
        sections.append(spend_md)

    cleaned_text = "\n\n".join(sections).strip()
    if not cleaned_text:
        return None

    metadata: dict[str, Any] = {
        "candidate_id": candidate_id,
        "party": party,
        "state": state,
        "office": office,
        "incumbent_challenge_full": incumbent,
        "cycle_totals": totals_extras,
        "top_donors": donors_rows,
        "top_expenditures": spend_rows,
    }

    return Source(
        url=source_url,
        title=name,
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="fec",
        metadata=metadata,
    )


async def _fetch_committee(
    committee_id: str, source_url: str, timeout: float
) -> Source | None:
    common_params = {"api_key": _resolve_api_key()}

    header_payload = await _fetch_json_cached(
        "committee",
        committee_id,
        urljoin(_BASE_URL, f"committee/{committee_id}/"),
        timeout,
        params=common_params,
    )
    header = _first_result(header_payload)
    if not header:
        return None

    name = (header.get("name") or "").strip()
    if not name:
        return None
    cycles = header.get("cycles") or []
    cycle = _latest_cycle(cycles)

    totals_params = dict(common_params)
    if cycle is not None:
        totals_params["cycle"] = cycle
    totals_payload = await _fetch_json_cached(
        "committee-totals",
        f"{committee_id}-{cycle}" if cycle else committee_id,
        urljoin(_BASE_URL, f"committee/{committee_id}/totals/"),
        timeout,
        params=totals_params,
    )
    totals = _first_result(totals_payload)

    donors_params = dict(common_params)
    donors_params.update(
        {"committee_id": committee_id, "per_page": 10, "sort": "-total"}
    )
    donors_payload = await _fetch_json_cached(
        "committee-donors",
        f"{committee_id}-{cycle}" if cycle else committee_id,
        urljoin(_BASE_URL, "schedules/schedule_a/by_employer/"),
        timeout,
        params=donors_params,
    )

    purposes_params = dict(common_params)
    purposes_params.update(
        {"committee_id": committee_id, "per_page": 10, "sort": "-total"}
    )
    purposes_payload = await _fetch_json_cached(
        "committee-purposes",
        f"{committee_id}-{cycle}" if cycle else committee_id,
        urljoin(_BASE_URL, "schedules/schedule_b/by_purpose/"),
        timeout,
        params=purposes_params,
    )

    totals_md, totals_extras = _cycle_totals_block(totals, cycle=cycle)
    donors_md, donors_rows = _top_donors_block(donors_payload)
    spend_md, spend_rows = _top_expenditures_block(purposes_payload)

    committee_type = (header.get("committee_type_full") or "").strip()
    designation = (header.get("designation_full") or "").strip()
    party = (header.get("party_full") or header.get("party") or "").strip()
    state = (header.get("state") or "").strip()
    org_type = (header.get("organization_type_full") or "").strip()
    meta_bits = [
        b for b in (committee_type, designation, party, state, org_type) if b
    ]
    meta_line = "_" + " · ".join(meta_bits) + "_" if meta_bits else ""

    sections = [f"# {name}"]
    if meta_line:
        sections.append(meta_line)
    if totals_md:
        sections.append(totals_md)
    if donors_md:
        sections.append(donors_md)
    if spend_md:
        sections.append(spend_md)

    cleaned_text = "\n\n".join(sections).strip()
    if not cleaned_text:
        return None

    metadata: dict[str, Any] = {
        "committee_id": committee_id,
        "committee_type_full": committee_type,
        "designation_full": designation,
        "party": party,
        "state": state,
        "organization_type": org_type,
        "cycle_totals": totals_extras,
        "top_donors": donors_rows,
        "top_expenditures": spend_rows,
    }

    return Source(
        url=source_url,
        title=name,
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="fec",
        metadata=metadata,
    )


async def fetch(url: str, timeout: float = 30.0) -> Source | None:
    """Open a candidate or committee page on www.fec.gov/data/ and return a Source.

    Returns ``None`` for unrecognised URLs (anything outside ``www.fec.gov``,
    or paths other than ``/data/candidate/<id>/`` and ``/data/committee/<id>/``)
    and for any transport / HTTP / parse failure.
    """
    if not url:
        return None
    resource, resource_id = _classify_url(url)
    if not resource or not resource_id:
        return None

    if resource == "candidate":
        return await _fetch_candidate(resource_id, url, timeout)
    if resource == "committee":
        return await _fetch_committee(resource_id, url, timeout)
    return None


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


__all__ = ["fetch", "reset_for_tests", "search"]
