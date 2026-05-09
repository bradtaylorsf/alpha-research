"""ProPublica Nonprofit Explorer connector (issue #100).

Public surface:

* ``async def search(query, *, max_results=20) -> list[SearchResult]`` hits
  ``projects.propublica.org/nonprofits/api/v2/search.json`` by org name or
  EIN. Returns name, EIN, NTEE category, city/state, permalink.
* ``async def fetch(url) -> Source | None`` opens an org detail page and
  returns rolled-up filings: latest 990 form-line summary plus a filing
  history list.

No auth required (public API). Polite per-host rate of 1 RPS.

Org detail JSON is cached at ``corpus/.cache/nonprofits/org-<EIN>.json``.
Per AC, IRS PDFs are NOT auto-fetched — their URLs are stored in
``Source.metadata["filings"]`` for downstream extract_findings.
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
from research_agent.tools._registry import (
    BaseSearchPayload as _BaseSearchPayload,
    register_kind as _register_kind,
)
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_BASE_URL = "https://projects.propublica.org/nonprofits/api/v2/"
_SITE_BASE = "https://projects.propublica.org/nonprofits"
# AC: polite per-host rate of 1 RPS.
_RATE_LIMIT_INTERVAL = 1.0
_CACHE_DIR = Path("corpus/.cache/nonprofits")

# Permalinks look like /nonprofits/organizations/<EIN> with EIN as digits only.
_ORG_URL_RE = re.compile(r"^/nonprofits/organizations/(?P<ein>\d+)/?$")

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _format_ein(raw: Any) -> str:
    """Normalize EIN to ``NN-NNNNNNN``. Accepts ints or strings."""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) == 9:
        return f"{digits[:2]}-{digits[2:]}"
    return text


def _subsection_label(code: Any) -> str:
    """Render IRC §501(c)(N) when ``code`` looks like a subsection int."""
    try:
        n = int(code)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return f"501(c)({n})"


def _permalink(ein_digits: str) -> str:
    return f"{_SITE_BASE}/organizations/{ein_digits}"


def _build_search_result(hit: dict[str, Any]) -> SearchResult | None:
    ein_raw = hit.get("ein")
    if ein_raw is None:
        return None
    ein_digits = re.sub(r"\D", "", str(ein_raw))
    if not ein_digits:
        return None
    name = (hit.get("name") or "").strip()
    if not name:
        return None

    url = _permalink(ein_digits)
    ein_pretty = hit.get("strein") or _format_ein(ein_raw)
    city = (hit.get("city") or "").strip()
    state = (hit.get("state") or "").strip()
    ntee = (hit.get("ntee_code") or hit.get("raw_ntee_code") or "").strip()
    subsection = _subsection_label(hit.get("subseccd"))

    parts = [name]
    location = ", ".join(p for p in (city, state) if p)
    if location:
        parts.append(location)
    if subsection:
        parts.append(subsection)
    if ntee:
        parts.append(f"NTEE {ntee}")
    snippet = " — ".join(parts) if parts else name

    extras: dict[str, Any] = {
        "ein": ein_pretty,
        "ein_digits": ein_digits,
        "ntee_code": ntee,
        "city": city,
        "state": state,
        "subsection_code": hit.get("subseccd"),
        "subsection": subsection,
        "sub_name": hit.get("sub_name") or "",
        "score": hit.get("score"),
    }

    return SearchResult(
        url=url,
        title=name,
        snippet=snippet,
        published_at=None,
        source_kind="nonprofits",
        extras=extras,
    )


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def search(
    query: str,
    *,
    max_results: int = 20,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Run a Nonprofit Explorer search and return up to ``max_results`` hits.

    Hits ``v2/search.json?q=<query>`` and parses the ``organizations[]``
    array. Each :class:`SearchResult` carries the EIN, NTEE code, and
    city/state in ``extras`` so downstream re-rankers can disambiguate.

    Returns ``[]`` on transport / HTTP error / non-JSON body — connector
    failures must never crash the planner.
    """
    await _rate_limit_gate()

    url = urljoin(_BASE_URL, "search.json")
    params = {"q": query}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=_headers(),
        ) as client:
            response = await client.get(url, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("nonprofits search failed for %r: %s", query, exc)
        return []

    if response.status_code != 200:
        logger.warning(
            "nonprofits search returned HTTP %s for %r",
            response.status_code,
            query,
        )
        return []

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning(
            "nonprofits search returned non-JSON for %r: %s", query, exc
        )
        return []

    raw_hits = payload.get("organizations") or []
    out: list[SearchResult] = []
    for hit in raw_hits[:max_results]:
        if not isinstance(hit, dict):
            continue
        result = _build_search_result(hit)
        if result is not None:
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def _classify_url(url: str) -> str | None:
    """Return the EIN (digits-only) when ``url`` is a Nonprofit Explorer org page."""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    # Strict host match so look-alikes like ``projects.propublica.org.attacker.example``
    # don't pass — the Source.url would otherwise leak the attacker domain
    # downstream even though the body came from the official API.
    if host != "projects.propublica.org":
        return None
    m = _ORG_URL_RE.match(parsed.path or "")
    if not m:
        return None
    return m.group("ein")


def _cache_path(ein: str) -> Path:
    safe = re.sub(r"\D", "", ein)
    return _CACHE_DIR / f"org-{safe}.json"


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def _load_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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
        logger.warning("nonprofits fetch failed for %s: %s", url, exc)
        return None, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


def _fmt_money(value: Any) -> str:
    try:
        return f"${int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _filing_summary_block(
    latest: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    """Render the latest 990 form-line summary; return (markdown, extras)."""
    if not latest:
        return "", {}
    year = latest.get("tax_prd_yr") or "?"
    rows = [
        ("Total revenue", _fmt_money(latest.get("totrevenue"))),
        ("Total functional expenses", _fmt_money(latest.get("totfuncexpns"))),
        ("Total assets (end)", _fmt_money(latest.get("totassetsend"))),
        ("Total liabilities (end)", _fmt_money(latest.get("totliabend"))),
    ]
    officer_comp = latest.get("compnsatncurrofcr")
    if officer_comp not in (None, ""):
        rows.append(("Top officer compensation", _fmt_money(officer_comp)))
    pct_officer = latest.get("pct_compnsatncurrofcr")
    if pct_officer not in (None, ""):
        rows.append(("% officer comp / revenue", f"{pct_officer}"))

    lines = [f"## Latest filing (FY {year})", ""]
    for label, value in rows:
        lines.append(f"- **{label}:** {value}")
    md = "\n".join(lines)

    extras = {
        "latest_filing_year": year if isinstance(year, int) else None,
        "totrevenue": latest.get("totrevenue"),
        "totfuncexpns": latest.get("totfuncexpns"),
        "totassetsend": latest.get("totassetsend"),
        "totliabend": latest.get("totliabend"),
        "compnsatncurrofcr": latest.get("compnsatncurrofcr"),
        "pct_compnsatncurrofcr": latest.get("pct_compnsatncurrofcr"),
    }
    return md, extras


def _form_type_label(filing: dict[str, Any]) -> str:
    label = filing.get("formtype_str")
    if isinstance(label, str) and label.strip():
        return label.strip()
    code = filing.get("formtype")
    mapping = {0: "990", 1: "990-EZ", 2: "990-PF", 4: "990-T"}
    if isinstance(code, int) and code in mapping:
        return mapping[code]
    if code is not None:
        return f"form {code}"
    return "?"


def _filing_history_block(
    with_data: list[dict[str, Any]],
    without_data: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for f in with_data + without_data:
        if not isinstance(f, dict):
            continue
        rows.append(
            {
                "tax_period": f.get("tax_prd"),
                "year": f.get("tax_prd_yr"),
                "form_type": _form_type_label(f),
                "pdf_url": f.get("pdf_url") or "",
                "updated": f.get("updated") or "",
            }
        )
    if not rows:
        return "", rows

    rows_sorted = sorted(
        rows, key=lambda r: (r["year"] is None, r["year"]), reverse=True
    )
    lines = ["## Filings", ""]
    for r in rows_sorted:
        line = f"- FY {r['year'] or '?'} — {r['form_type']}"
        if r["pdf_url"]:
            line += f" — [PDF]({r['pdf_url']})"
        if r["updated"]:
            line += f" — updated {r['updated']}"
        lines.append(line)
    return "\n".join(lines), rows_sorted


def _related_orgs_block(org: dict[str, Any]) -> tuple[str, list[Any]]:
    """Render related orgs if the API surfaces them. Returns ('', []) when absent."""
    candidates = (
        org.get("related_orgs")
        or org.get("related_tax_exempt_orgs")
        or []
    )
    if not isinstance(candidates, list) or not candidates:
        return "", []
    lines = ["## Related organizations", ""]
    flat: list[Any] = []
    for entry in candidates:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("org_name") or ""
            ein = entry.get("ein") or ""
            ein_pretty = _format_ein(ein) if ein else ""
            label = name or ein_pretty or "?"
            tail = f" (EIN {ein_pretty})" if name and ein_pretty else ""
            lines.append(f"- {label}{tail}")
            flat.append({"name": name, "ein": ein_pretty})
        elif isinstance(entry, str) and entry.strip():
            lines.append(f"- {entry.strip()}")
            flat.append(entry.strip())
    return "\n".join(lines), flat


async def fetch(url: str, timeout: float = 30.0) -> Source | None:
    """Open a Nonprofit Explorer org page and return a :class:`Source`.

    The URL is classified via the path ``/nonprofits/organizations/<EIN>``;
    any URL not matching that shape (or pointing outside
    ``projects.propublica.org``) returns ``None``. The org JSON is cached at
    ``corpus/.cache/nonprofits/org-<EIN>.json`` so repeated fetches skip
    the network entirely.

    Per AC, IRS PDFs are NOT auto-fetched. Their URLs land in
    ``metadata['filings']`` so downstream extract_findings can route to
    the dedicated PDF pipeline.
    """
    if not url:
        return None
    ein_digits = _classify_url(url)
    if not ein_digits:
        return None

    cache = _cache_path(ein_digits)
    payload = _load_cache(cache)
    if payload is None:
        await _rate_limit_gate()
        api_url = urljoin(_BASE_URL, f"organizations/{ein_digits}.json")
        status, payload = await _http_get_json(api_url, timeout)
        if status is None or status >= 400 or not isinstance(payload, dict):
            if status is not None and status >= 400:
                logger.warning(
                    "nonprofits org HTTP %s for %s", status, api_url
                )
            return None
        _write_cache(cache, payload)

    org = payload.get("organization") if isinstance(payload, dict) else None
    if not isinstance(org, dict):
        return None

    name = (org.get("name") or "").strip()
    if not name:
        return None

    ein_pretty = _format_ein(org.get("ein") or ein_digits)
    subsection = _subsection_label(org.get("subsection_code"))
    ntee_code = (org.get("ntee_code") or "").strip()
    city = (org.get("city") or "").strip()
    state = (org.get("state") or "").strip()
    location = ", ".join(p for p in (city, state) if p)

    meta_parts = [f"EIN {ein_pretty}"] if ein_pretty else []
    if subsection:
        meta_parts.append(subsection)
    if ntee_code:
        meta_parts.append(f"NTEE {ntee_code}")
    if location:
        meta_parts.append(location)
    metadata_line = "_" + " · ".join(meta_parts) + "_" if meta_parts else ""

    filings_with_data = payload.get("filings_with_data") or []
    filings_without_data = payload.get("filings_without_data") or []
    if not isinstance(filings_with_data, list):
        filings_with_data = []
    if not isinstance(filings_without_data, list):
        filings_without_data = []

    latest = (
        filings_with_data[0]
        if filings_with_data and isinstance(filings_with_data[0], dict)
        else None
    )
    summary_md, summary_extras = _filing_summary_block(latest)
    history_md, filings_rows = _filing_history_block(
        filings_with_data, filings_without_data
    )
    related_md, related_flat = _related_orgs_block(org)

    sections = [f"# {name}"]
    if metadata_line:
        sections.append(metadata_line)
    if summary_md:
        sections.append(summary_md)
    if history_md:
        sections.append(history_md)
    if related_md:
        sections.append(related_md)

    cleaned_text = "\n\n".join(sections).strip()
    if not cleaned_text:
        return None

    metadata: dict[str, Any] = {
        "ein": ein_pretty,
        "ein_digits": ein_digits,
        "ntee_code": ntee_code,
        "ntee_classification": org.get("ntee_classification") or "",
        "subsection_code": org.get("subsection_code"),
        "subsection": subsection,
        "in_care_of_name": org.get("careofname") or org.get("in_care_of_name") or "",
        "city": city,
        "state": state,
        "exempt_status": org.get("exempt_organization_status_code"),
        "classification": org.get("classification_codes") or "",
        "filings": filings_rows,
        "latest_filing_year": summary_extras.get("latest_filing_year"),
        "related_orgs": related_flat,
    }

    return Source(
        url=url,
        title=name,
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="nonprofits",
        metadata=metadata,
    )


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


KIND = "nonprofits_search"


class _PayloadSchema(_BaseSearchPayload):
    max_results: int | None = None


_register_kind(
    KIND,
    payload_schema=_PayloadSchema,
    search_fn=search,
    fetch_fn=fetch,
    host_patterns=("projects.propublica.org",),
    skill_name=None,
    description="ProPublica Nonprofit Explorer (Form 990 filings, no auth)",
    optional_payload_knobs="—",
    example_query="Heritage Foundation",
    module_name="nonprofits",
)


__all__ = ["KIND", "fetch", "reset_for_tests", "search"]
