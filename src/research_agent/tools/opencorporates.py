"""OpenCorporates connector — global company registry across 200+ jurisdictions (issue #92).

Public surface:

* ``async def search(query, *, jurisdiction=None, max_results=20)
  -> list[SearchResult]`` hits ``api.opencorporates.com/v0.4/companies/search``
  and returns hits with company number, name, jurisdiction, status, and
  registered agent (when available).
* ``async def fetch(url) -> Source | None`` opens a company page
  (``https://opencorporates.com/companies/<jurisdiction>/<id>``) and returns
  markdown of officers, filings history, registered agent + address, and any
  associated entities.

OpenCorporates is the canonical free-tier global registry. Critical for
**shell-company unmasking**: same registered agent across multiple LLCs at
one address is a classic red flag for political/financial investigations.

Auth: OpenCorporates removed anonymous v0.4 access — ``OPENCORPORATES_API_KEY``
is required for any live request. Without it, ``search()`` and ``fetch()``
return ``[]`` / ``None`` without making a network call (anonymous calls
return HTTP 401). The token rides as ``?api_token=<key>``. Free
public-benefit access requires emailing service desk; commercial
pricing £2,250–£12,000/yr.

Per-host rate gate: 0.5 RPS to stay polite on the authenticated tier.
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
from research_agent.tools._registry import (
    BaseSearchPayload as _BaseSearchPayload,
    register_kind as _register_kind,
)
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.opencorporates.com/v0.4/"
_SITE_BASE = "https://opencorporates.com"
_ACCEPTED_HOSTS = frozenset({"opencorporates.com", "www.opencorporates.com"})
# AC: per-host rate of 0.5 RPS — anonymous tier is fragile.
_RATE_LIMIT_INTERVAL = 2.0

# Company URLs look like /companies/<jurisdiction>/<id> with optional trailing
# slash. Jurisdiction is a lower-case alpha-numeric ISO-ish code (e.g. ``us_ca``,
# ``gb``, ``de``). Company id is mostly alpha-numeric but jurisdictions vary;
# allow a permissive set rather than locking ourselves out of edge codes.
_COMPANY_URL_RE = re.compile(
    r"^/companies/(?P<jurisdiction>[a-z0-9_]+)/(?P<id>[A-Za-z0-9._-]+)/?$"
)

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


# ---------------------------------------------------------------------------
# Config / rate-limit helpers
# ---------------------------------------------------------------------------


def _resolve_api_key() -> str | None:
    """Return the configured OpenCorporates API token, or ``None`` for anonymous."""
    raw = config.get("OPENCORPORATES_API_KEY") or ""
    key = raw.strip()
    return key or None


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


def _company_permalink(jurisdiction: str, company_number: str) -> str:
    return f"{_SITE_BASE}/companies/{jurisdiction}/{company_number}"


def _registered_agent(company: dict[str, Any]) -> tuple[str, str]:
    """Return ``(name, address)`` for the registered agent, if surfaced.

    OpenCorporates places the agent under ``registered_agent`` (a dict on
    detail pages) but on search rows it sometimes flattens to
    ``registered_agent_name``/``registered_agent_address``. Tolerate both.
    """
    agent = company.get("registered_agent")
    if isinstance(agent, dict):
        name = (agent.get("name") or "").strip()
        address = (
            agent.get("address")
            or agent.get("registered_address_in_full")
            or ""
        )
        return name, str(address).strip()
    name = (company.get("registered_agent_name") or "").strip()
    address = (company.get("registered_agent_address") or "").strip()
    return name, address


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


def _build_search_result(item: Any) -> SearchResult | None:
    """Extract a ``SearchResult`` from one envelope row.

    OpenCorporates wraps each hit as ``{"company": {...}}`` — unwrap it then
    pull the canonical fields.
    """
    if not isinstance(item, dict):
        return None
    company = item.get("company")
    if not isinstance(company, dict):
        return None

    name = (company.get("name") or "").strip()
    company_number = (company.get("company_number") or "").strip()
    jurisdiction_code = (company.get("jurisdiction_code") or "").strip()
    if not name or not company_number:
        return None

    current_status = (company.get("current_status") or "").strip()
    incorporation_date = company.get("incorporation_date")
    company_type = (company.get("company_type") or "").strip()
    registered_address = (
        company.get("registered_address_in_full")
        or company.get("registered_address")
        or ""
    )
    if isinstance(registered_address, dict):
        registered_address = registered_address.get("in_full") or ""
    registered_address = str(registered_address).strip()

    agent_name, agent_address = _registered_agent(company)

    permalink = company.get("opencorporates_url") or _company_permalink(
        jurisdiction_code, company_number
    )

    snippet_bits: list[str] = []
    if jurisdiction_code:
        snippet_bits.append(jurisdiction_code)
    if current_status:
        snippet_bits.append(current_status)
    if agent_name:
        snippet_bits.append(f"Agent: {agent_name}")
    elif registered_address:
        snippet_bits.append(registered_address)
    snippet = " — ".join(snippet_bits)

    extras: dict[str, Any] = {
        "company_number": company_number,
        "name": name,
        "jurisdiction_code": jurisdiction_code,
        "current_status": current_status,
        "company_type": company_type,
        "incorporation_date": incorporation_date,
        "registered_address_in_full": registered_address,
        "registered_agent_name": agent_name,
        "agent_address": agent_address,
    }
    return SearchResult(
        url=permalink,
        title=name,
        snippet=snippet,
        published_at=_parse_iso_date(incorporation_date),
        source_kind="opencorporates",
        extras=extras,
    )


async def _http_get_json(
    url: str, *, params: dict[str, Any] | None, timeout: float
) -> tuple[int | None, dict[str, Any] | None]:
    """GET ``url`` and return ``(status, json)``. ``status=None`` on transport error."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("opencorporates GET failed for %s: %s", url, exc)
        return None, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        return response.status_code, response.json()
    except ValueError as exc:
        logger.warning("opencorporates returned non-JSON for %s: %s", url, exc)
        return response.status_code, None


async def search(
    query: str,
    *,
    jurisdiction: str | None = None,
    max_results: int = 20,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Run an OpenCorporates company search and return up to ``max_results`` hits.

    ``jurisdiction`` is the OpenCorporates jurisdiction code (e.g. ``us_ca``
    for California, ``gb`` for the UK). ``OPENCORPORATES_API_KEY`` is
    required — anonymous v0.4 access is gated and returns HTTP 401, so
    without a key this returns ``[]`` without making a network call.

    Returns ``[]`` on transport / HTTP error or non-JSON body — connector
    failures must never crash the planner.
    """
    api_key = _resolve_api_key()
    if not api_key:
        logger.info(
            "opencorporates search skipped: OPENCORPORATES_API_KEY not set"
            " (anonymous v0.4 access is gated)"
        )
        return []

    params: dict[str, Any] = {
        "q": query,
        "per_page": min(max_results, 100),
        "api_token": api_key,
    }
    if jurisdiction:
        params["jurisdiction_code"] = jurisdiction

    await _rate_limit_gate()
    url = urljoin(_BASE_URL, "companies/search")
    status, payload = await _http_get_json(url, params=params, timeout=timeout)
    if status is None or status >= 400 or not isinstance(payload, dict):
        if status is not None and status >= 400:
            logger.warning("opencorporates search HTTP %s for %r", status, query)
        return []

    results_envelope = payload.get("results")
    if not isinstance(results_envelope, dict):
        return []
    raw_hits = results_envelope.get("companies") or []
    if not isinstance(raw_hits, list):
        return []

    out: list[SearchResult] = []
    for hit in raw_hits[:max_results]:
        result = _build_search_result(hit)
        if result is not None:
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def _classify_url(url: str) -> tuple[str, str] | None:
    """Return ``(jurisdiction, company_number)`` for OpenCorporates company URLs.

    Strict host match against ``_ACCEPTED_HOSTS`` so look-alikes like
    ``opencorporates.com.attacker.example`` are rejected.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    if host not in _ACCEPTED_HOSTS:
        return None
    m = _COMPANY_URL_RE.match(parsed.path or "")
    if not m:
        return None
    return m.group("jurisdiction"), m.group("id")


def _officers_block(officers: Any) -> tuple[str, list[dict[str, Any]]]:
    """Render the ``officers[]`` list as a markdown section."""
    if not isinstance(officers, list) or not officers:
        return "", []
    rows: list[dict[str, Any]] = []
    for entry in officers:
        if not isinstance(entry, dict):
            continue
        officer = entry.get("officer") if isinstance(entry.get("officer"), dict) else entry
        if not isinstance(officer, dict):
            continue
        name = (officer.get("name") or "").strip()
        position = (officer.get("position") or "").strip()
        start = (officer.get("start_date") or "").strip() if officer.get("start_date") else ""
        end = (officer.get("end_date") or "").strip() if officer.get("end_date") else ""
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "position": position,
                "start_date": start or None,
                "end_date": end or None,
            }
        )
    if not rows:
        return "", rows
    lines = ["## Officers", ""]
    for r in rows:
        bits: list[str] = [r["name"]]
        if r["position"]:
            bits.append(r["position"])
        date_bits = [d for d in (r["start_date"], r["end_date"]) if d]
        if date_bits:
            bits.append("–".join(date_bits))
        lines.append("- " + " — ".join(bits))
    return "\n".join(lines), rows


def _filings_block(filings: Any) -> tuple[str, list[dict[str, Any]]]:
    """Render the ``filings[]`` history as a markdown section."""
    if not isinstance(filings, list) or not filings:
        return "", []
    rows: list[dict[str, Any]] = []
    for entry in filings:
        if not isinstance(entry, dict):
            continue
        filing = entry.get("filing") if isinstance(entry.get("filing"), dict) else entry
        if not isinstance(filing, dict):
            continue
        title = (filing.get("title") or filing.get("description") or "").strip()
        filing_date = (filing.get("date") or "").strip() if filing.get("date") else ""
        filing_type = (
            filing.get("filing_type_name")
            or filing.get("filing_type") or ""
        ).strip()
        if not title and not filing_type:
            continue
        rows.append(
            {
                "title": title,
                "filing_type": filing_type,
                "date": filing_date or None,
            }
        )
    if not rows:
        return "", rows
    lines = ["## Filings", ""]
    for r in rows:
        bits: list[str] = []
        if r["date"]:
            bits.append(r["date"])
        if r["filing_type"]:
            bits.append(r["filing_type"])
        if r["title"]:
            bits.append(r["title"])
        lines.append("- " + " — ".join(bits))
    return "\n".join(lines), rows


def _registered_agent_block(agent_name: str, agent_address: str) -> str:
    if not agent_name and not agent_address:
        return ""
    lines = ["## Registered agent", ""]
    if agent_name:
        lines.append(f"- **Name:** {agent_name}")
    if agent_address:
        lines.append(f"- **Address:** {agent_address}")
    return "\n".join(lines)


def _associated_entities_block(
    company: dict[str, Any],
) -> tuple[str, list[str]]:
    """Roll up previous_names + alternative_names into one section."""
    names: list[str] = []
    seen: set[str] = set()
    for key in ("previous_names", "alternative_names"):
        raw = company.get(key) or []
        if not isinstance(raw, list):
            continue
        for entry in raw:
            if isinstance(entry, dict):
                value = (entry.get("company_name") or entry.get("name") or "").strip()
            else:
                value = str(entry or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            names.append(value)
    if not names:
        return "", names
    lines = ["## Associated entities", ""]
    for name in names:
        lines.append(f"- {name}")
    return "\n".join(lines), names


async def fetch(url: str, timeout: float = 30.0) -> Source | None:
    """Open an OpenCorporates company page and return a :class:`Source`.

    Returns ``None`` for anything outside ``_ACCEPTED_HOSTS`` or paths other
    than ``/companies/<jurisdiction>/<id>``, and for any transport / HTTP /
    parse failure.
    """
    if not url:
        return None
    parts = _classify_url(url)
    if parts is None:
        return None
    jurisdiction, company_number = parts

    api_key = _resolve_api_key()
    if not api_key:
        logger.info(
            "opencorporates fetch skipped: OPENCORPORATES_API_KEY not set"
            " (anonymous v0.4 access is gated)"
        )
        return None

    api_url = urljoin(_BASE_URL, f"companies/{jurisdiction}/{company_number}")
    params: dict[str, Any] = {"api_token": api_key}

    await _rate_limit_gate()
    status, payload = await _http_get_json(
        api_url, params=params, timeout=timeout
    )
    if status is None or status >= 400 or not isinstance(payload, dict):
        if status is not None and status >= 400:
            logger.warning(
                "opencorporates company HTTP %s for %s", status, api_url
            )
        return None

    results_envelope = payload.get("results")
    if not isinstance(results_envelope, dict):
        return None
    company = results_envelope.get("company")
    if not isinstance(company, dict):
        return None

    name = (company.get("name") or "").strip()
    if not name:
        return None
    current_status = (company.get("current_status") or "").strip()
    incorporation_date = (company.get("incorporation_date") or "").strip()
    company_type = (company.get("company_type") or "").strip()
    jurisdiction_code = (company.get("jurisdiction_code") or jurisdiction).strip()

    agent_name, agent_address = _registered_agent(company)
    officers_md, officers_rows = _officers_block(company.get("officers"))
    filings_md, filings_rows = _filings_block(company.get("filings"))
    associated_md, associated_names = _associated_entities_block(company)
    agent_md = _registered_agent_block(agent_name, agent_address)

    meta_bits = [
        b
        for b in (
            jurisdiction_code,
            current_status,
            incorporation_date,
            company_type,
        )
        if b
    ]
    meta_line = "_" + " · ".join(meta_bits) + "_" if meta_bits else ""

    sections = [f"# {name}"]
    if meta_line:
        sections.append(meta_line)
    if agent_md:
        sections.append(agent_md)
    if officers_md:
        sections.append(officers_md)
    if filings_md:
        sections.append(filings_md)
    if associated_md:
        sections.append(associated_md)

    cleaned_text = "\n\n".join(sections).strip()
    if not cleaned_text:
        return None

    metadata: dict[str, Any] = {
        "company_number": company_number,
        "jurisdiction_code": jurisdiction_code,
        "current_status": current_status,
        "incorporation_date": incorporation_date,
        "company_type": company_type,
        "registered_agent": {"name": agent_name, "address": agent_address},
        "registered_agent_name": agent_name,
        "registered_agent_address": agent_address,
        "officers": officers_rows,
        "filings": filings_rows,
        "associated_entities": associated_names,
    }

    return Source(
        url=url,
        title=name,
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="opencorporates",
        metadata=metadata,
    )


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


KIND = "opencorporates_search"


class _PayloadSchema(_BaseSearchPayload):
    jurisdiction: str | None = None
    max_results: int | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=("opencorporates.com", "www.opencorporates.com"),
    skill_name=None,
    description="Global company registry — requires `OPENCORPORATES_API_KEY`",
    optional_payload_knobs="`jurisdiction: us_ca\\|gb\\|...`",
    example_query="Acme Holdings",
    module_name="opencorporates",
)


__all__ = ["KIND", "fetch", "reset_for_tests", "search"]
