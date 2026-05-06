"""LinkedIn connector via third-party data broker (Proxycurl / Lix) — issue #115.

Public surface:

* ``async def search(query, *, kind="person", max_results=10)`` — hits the
  configured broker's person/company search endpoint and returns
  :class:`SearchResult` rows (name / company name, headline, location,
  profile URL).
* ``async def fetch(url)`` — accepts a ``linkedin.com/in/<slug>`` or
  ``linkedin.com/company/<slug>`` URL and returns a :class:`Source` with
  the rolled-up profile (employment, education, certifications, skills, or
  the parallel company facts: headcount, industry, locations).

LinkedIn has no official public API and direct scraping is TOS-blocked, so
we route through a data broker. The connector is **broker-pluggable**: the
default broker is Proxycurl (``LINKEDIN_BROKER=proxycurl``, key
``LINKEDIN_DATA_API_KEY``) and an alternate Lix recipe is wired in
(``LINKEDIN_BROKER=lix``, key ``LIX_API_KEY``). New brokers slot in by
adding a recipe to ``_BROKERS``.

**Per-lookup cost matters.** Both Proxycurl and Lix bill ≈ $0.01–$0.05 per
profile lookup. The orchestrator's synth/critique passes must NOT
auto-fetch every LinkedIn URL they encounter — fetches happen only when
the planner explicitly emits a ``linkedin_fetch`` task in
``tactical_replan`` after seeing the initial search results. A 1 RPS
per-process gate sits in front of every call as cheap insurance against
runaway loops.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from research_agent import config
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_FETCH_INTERVAL = 1.0

_PERSON_URL_RE = re.compile(r"^https?://(www\.)?linkedin\.com/in/[^/?#]+/?", re.IGNORECASE)
_COMPANY_URL_RE = re.compile(
    r"^https?://(www\.)?linkedin\.com/company/[^/?#]+/?", re.IGNORECASE
)

_VALID_KINDS = frozenset({"person", "company"})

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


# ---------------------------------------------------------------------------
# Broker recipes
# ---------------------------------------------------------------------------
#
# Each broker entry exposes the same callable shape so the upper layer can
# stay broker-agnostic. Adding a new broker means dropping a new entry into
# ``_BROKERS`` — the rest of the module reads only via these recipe hooks.


def _split_query(query: str) -> tuple[str, str]:
    parts = query.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


# --- Proxycurl ----------------------------------------------------------------


_PROXYCURL_PERSON_SEARCH = "https://nubela.co/proxycurl/api/v2/search/person/"
_PROXYCURL_COMPANY_SEARCH = "https://nubela.co/proxycurl/api/v2/search/company/"
_PROXYCURL_PERSON_FETCH = "https://nubela.co/proxycurl/api/v2/linkedin"
_PROXYCURL_COMPANY_FETCH = "https://nubela.co/proxycurl/api/linkedin/company"


def _proxycurl_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _proxycurl_search_person_params(query: str, max_results: int) -> dict[str, str]:
    first, last = _split_query(query)
    params: dict[str, str] = {
        "country": "us",
        "page_size": str(min(max(max_results, 1), 100)),
    }
    if first:
        params["first_name"] = first
    if last:
        params["last_name"] = last
    return params


def _proxycurl_search_company_params(query: str, max_results: int) -> dict[str, str]:
    return {
        "country": "us",
        "name": query,
        "page_size": str(min(max(max_results, 1), 100)),
    }


def _proxycurl_fetch_person_params(profile_url: str) -> dict[str, str]:
    return {"url": profile_url, "use_cache": "if-present"}


def _proxycurl_fetch_company_params(profile_url: str) -> dict[str, str]:
    return {"url": profile_url, "use_cache": "if-present"}


def _proxycurl_parse_search_person(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    raw = payload.get("results") or []
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        url = str(row.get("linkedin_profile_url") or "")
        if not url:
            continue
        profile = row.get("profile") if isinstance(row.get("profile"), dict) else {}
        full_name = str(
            profile.get("full_name")
            or " ".join(
                p
                for p in [profile.get("first_name"), profile.get("last_name")]
                if p
            )
            or url.rsplit("/", 1)[-1]
        )
        headline = str(profile.get("headline") or profile.get("occupation") or "")
        location = str(
            profile.get("city")
            or profile.get("country_full_name")
            or profile.get("country")
            or ""
        )
        current_company, current_title = _proxycurl_pick_current_role(profile)
        out.append(
            {
                "url": url,
                "title": full_name,
                "snippet": headline,
                "location": location,
                "current_company": current_company,
                "current_title": current_title,
            }
        )
    return out


def _proxycurl_pick_current_role(profile: dict[str, Any]) -> tuple[str, str]:
    experiences = profile.get("experiences")
    if isinstance(experiences, list):
        for exp in experiences:
            if not isinstance(exp, dict):
                continue
            ends_at = exp.get("ends_at")
            if ends_at in (None, {}):
                return str(exp.get("company") or ""), str(exp.get("title") or "")
        # Fall back to the first row if everything has an end-date.
        for exp in experiences:
            if isinstance(exp, dict):
                return str(exp.get("company") or ""), str(exp.get("title") or "")
    occupation = str(profile.get("occupation") or "")
    return "", occupation


def _proxycurl_parse_search_company(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    raw = payload.get("results") or []
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        url = str(row.get("linkedin_profile_url") or "")
        if not url:
            continue
        profile = row.get("profile") if isinstance(row.get("profile"), dict) else {}
        name = str(profile.get("name") or url.rsplit("/", 1)[-1])
        tagline = str(profile.get("tagline") or profile.get("description") or "")
        industry = str(profile.get("industry") or "")
        size = profile.get("company_size") or profile.get("company_size_on_linkedin")
        headcount = ""
        if isinstance(size, list) and size:
            headcount = "-".join(str(s) for s in size if s is not None)
        elif isinstance(size, int | str):
            headcount = str(size)
        hq = profile.get("hq") if isinstance(profile.get("hq"), dict) else {}
        hq_location = ", ".join(
            p for p in [hq.get("city"), hq.get("state"), hq.get("country")] if p
        )
        out.append(
            {
                "url": url,
                "title": name,
                "snippet": tagline,
                "industry": industry,
                "headcount": headcount,
                "hq_location": hq_location,
            }
        )
    return out


def _proxycurl_parse_fetch_person(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("full_name") or "")
    headline = str(payload.get("headline") or payload.get("occupation") or "")

    experiences: list[dict[str, Any]] = []
    raw_exp = payload.get("experiences") or []
    if isinstance(raw_exp, list):
        for exp in raw_exp:
            if not isinstance(exp, dict):
                continue
            experiences.append(
                {
                    "title": str(exp.get("title") or ""),
                    "company": str(exp.get("company") or ""),
                    "starts_at": _proxycurl_format_date(exp.get("starts_at")),
                    "ends_at": _proxycurl_format_date(exp.get("ends_at")),
                    "description": str(exp.get("description") or ""),
                    "location": str(exp.get("location") or ""),
                }
            )

    education: list[dict[str, Any]] = []
    raw_edu = payload.get("education") or []
    if isinstance(raw_edu, list):
        for edu in raw_edu:
            if not isinstance(edu, dict):
                continue
            education.append(
                {
                    "school": str(edu.get("school") or ""),
                    "degree": str(edu.get("degree_name") or ""),
                    "field": str(edu.get("field_of_study") or ""),
                    "starts_at": _proxycurl_format_date(edu.get("starts_at")),
                    "ends_at": _proxycurl_format_date(edu.get("ends_at")),
                }
            )

    certifications: list[dict[str, Any]] = []
    raw_certs = payload.get("certifications") or []
    if isinstance(raw_certs, list):
        for c in raw_certs:
            if not isinstance(c, dict):
                continue
            certifications.append(
                {
                    "name": str(c.get("name") or ""),
                    "authority": str(c.get("authority") or ""),
                    "starts_at": _proxycurl_format_date(c.get("starts_at")),
                    "ends_at": _proxycurl_format_date(c.get("ends_at")),
                }
            )

    skills: list[str] = []
    raw_skills = payload.get("skills") or []
    if isinstance(raw_skills, list):
        skills = [str(s) for s in raw_skills if s]

    return {
        "name": name,
        "headline": headline,
        "experiences": experiences,
        "education": education,
        "certifications": certifications,
        "skills": skills,
    }


def _proxycurl_parse_fetch_company(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "")
    tagline = str(payload.get("tagline") or "")
    industry = str(payload.get("industry") or "")
    description = str(payload.get("description") or "")

    size = payload.get("company_size") or payload.get("company_size_on_linkedin")
    headcount = ""
    if isinstance(size, list) and size:
        headcount = "-".join(str(s) for s in size if s is not None)
    elif isinstance(size, int | str):
        headcount = str(size)

    employee_count = payload.get("employee_count")

    hq = payload.get("hq") if isinstance(payload.get("hq"), dict) else {}
    hq_location = ", ".join(
        p for p in [hq.get("city"), hq.get("state"), hq.get("country")] if p
    )

    locations: list[str] = []
    raw_locs = payload.get("locations") or []
    if isinstance(raw_locs, list):
        for loc in raw_locs:
            if isinstance(loc, dict):
                locations.append(
                    ", ".join(
                        p for p in [loc.get("city"), loc.get("state"), loc.get("country")] if p
                    )
                )
            elif isinstance(loc, str):
                locations.append(loc)

    specialities = payload.get("specialities") or []
    if not isinstance(specialities, list):
        specialities = []
    specialities = [str(s) for s in specialities if s]

    updates: list[str] = []
    raw_updates = payload.get("updates") or []
    if isinstance(raw_updates, list):
        for u in raw_updates:
            if isinstance(u, dict):
                text = u.get("text") or u.get("posted_on")
                if text:
                    updates.append(str(text))

    return {
        "name": name,
        "tagline": tagline,
        "industry": industry,
        "description": description,
        "headcount": headcount,
        "employee_count": employee_count,
        "hq_location": hq_location,
        "locations": locations,
        "specialities": specialities,
        "updates": updates,
    }


def _proxycurl_format_date(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    year = value.get("year")
    month = value.get("month")
    if not year:
        return ""
    if month:
        return f"{int(year):04d}-{int(month):02d}"
    return f"{int(year):04d}"


# --- Lix (alternate broker) ---------------------------------------------------
#
# Lix uses a different REST surface. The shape below mirrors the public
# pricing-page docs and is wired in so the broker layer is exercised even
# before a Lix engagement lands a real key — tests can switch
# ``LINKEDIN_BROKER=lix`` and verify the recipe routes correctly. Endpoints
# and parameter names map to Lix's `/v1` profile + search endpoints.


_LIX_PERSON_SEARCH = "https://api.lix-it.com/v1/li/linkedin/search/people"
_LIX_COMPANY_SEARCH = "https://api.lix-it.com/v1/li/linkedin/search/companies"
_LIX_PERSON_FETCH = "https://api.lix-it.com/v1/person"
_LIX_COMPANY_FETCH = "https://api.lix-it.com/v1/organisations/by-linkedin-url"


def _lix_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": api_key}


def _lix_search_person_params(query: str, max_results: int) -> dict[str, str]:
    return {"query": query, "limit": str(min(max(max_results, 1), 100))}


def _lix_search_company_params(query: str, max_results: int) -> dict[str, str]:
    return {"query": query, "limit": str(min(max(max_results, 1), 100))}


def _lix_fetch_person_params(profile_url: str) -> dict[str, str]:
    return {"profile_link": profile_url}


def _lix_fetch_company_params(profile_url: str) -> dict[str, str]:
    return {"linkedin_url": profile_url}


def _lix_parse_search_person(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    raw = payload.get("people") or payload.get("results") or []
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        url = str(row.get("link") or row.get("linkedin_url") or "")
        if not url:
            continue
        out.append(
            {
                "url": url,
                "title": str(row.get("name") or row.get("full_name") or ""),
                "snippet": str(row.get("headline") or row.get("title") or ""),
                "location": str(row.get("location") or ""),
                "current_company": str(row.get("company") or ""),
                "current_title": str(row.get("position") or row.get("title") or ""),
            }
        )
    return out


def _lix_parse_search_company(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    raw = payload.get("companies") or payload.get("results") or []
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        url = str(row.get("link") or row.get("linkedin_url") or "")
        if not url:
            continue
        out.append(
            {
                "url": url,
                "title": str(row.get("name") or ""),
                "snippet": str(row.get("description") or row.get("tagline") or ""),
                "industry": str(row.get("industry") or ""),
                "headcount": str(row.get("employee_count") or row.get("size") or ""),
                "hq_location": str(row.get("location") or ""),
            }
        )
    return out


def _lix_parse_fetch_person(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(payload.get("name") or payload.get("full_name") or ""),
        "headline": str(payload.get("headline") or ""),
        "experiences": list(payload.get("positions") or payload.get("experience") or []),
        "education": list(payload.get("education") or []),
        "certifications": list(payload.get("certifications") or []),
        "skills": list(payload.get("skills") or []),
    }


def _lix_parse_fetch_company(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(payload.get("name") or ""),
        "tagline": str(payload.get("tagline") or ""),
        "industry": str(payload.get("industry") or ""),
        "description": str(payload.get("description") or ""),
        "headcount": str(payload.get("employee_count") or ""),
        "employee_count": payload.get("employee_count"),
        "hq_location": str(payload.get("location") or ""),
        "locations": list(payload.get("locations") or []),
        "specialities": list(payload.get("specialities") or []),
        "updates": list(payload.get("updates") or []),
    }


# --- Recipe registry ----------------------------------------------------------


_BROKERS: dict[str, dict[str, Any]] = {
    "proxycurl": {
        "key_env": "LINKEDIN_DATA_API_KEY",
        "signup_url": "https://nubela.co/proxycurl/",
        "build_headers": _proxycurl_headers,
        "search_person_url": _PROXYCURL_PERSON_SEARCH,
        "search_company_url": _PROXYCURL_COMPANY_SEARCH,
        "fetch_person_url": _PROXYCURL_PERSON_FETCH,
        "fetch_company_url": _PROXYCURL_COMPANY_FETCH,
        "build_search_person_params": _proxycurl_search_person_params,
        "build_search_company_params": _proxycurl_search_company_params,
        "build_fetch_person_params": _proxycurl_fetch_person_params,
        "build_fetch_company_params": _proxycurl_fetch_company_params,
        "parse_search_person": _proxycurl_parse_search_person,
        "parse_search_company": _proxycurl_parse_search_company,
        "parse_fetch_person": _proxycurl_parse_fetch_person,
        "parse_fetch_company": _proxycurl_parse_fetch_company,
    },
    "lix": {
        "key_env": "LIX_API_KEY",
        "signup_url": "https://lix-it.com/",
        "build_headers": _lix_headers,
        "search_person_url": _LIX_PERSON_SEARCH,
        "search_company_url": _LIX_COMPANY_SEARCH,
        "fetch_person_url": _LIX_PERSON_FETCH,
        "fetch_company_url": _LIX_COMPANY_FETCH,
        "build_search_person_params": _lix_search_person_params,
        "build_search_company_params": _lix_search_company_params,
        "build_fetch_person_params": _lix_fetch_person_params,
        "build_fetch_company_params": _lix_fetch_company_params,
        "parse_search_person": _lix_parse_search_person,
        "parse_search_company": _lix_parse_search_company,
        "parse_fetch_person": _lix_parse_fetch_person,
        "parse_fetch_company": _lix_parse_fetch_company,
    },
}


# ---------------------------------------------------------------------------
# Config / rate-limit helpers
# ---------------------------------------------------------------------------


def _resolve_broker() -> tuple[str, dict[str, Any]]:
    name = (config.get("LINKEDIN_BROKER") or "proxycurl").strip().lower()
    recipe = _BROKERS.get(name)
    if recipe is None:
        raise RuntimeError(
            f"Unknown LINKEDIN_BROKER {name!r}; expected one of "
            f"{sorted(_BROKERS)}."
        )
    return name, recipe


def _resolve_key(broker: str, recipe: dict[str, Any]) -> str:
    env_name = recipe["key_env"]
    key = config.get(env_name) or ""
    if not key.strip():
        raise RuntimeError(
            f"LinkedIn connector requires a {env_name} (broker={broker}). "
            f"Sign up at {recipe['signup_url']} and set {env_name} in your .env."
        )
    return key.strip()


def _user_agent() -> str:
    return config.get("RESEARCH_USER_AGENT") or (
        "research-agent/0.1 (+local; contact unset)"
    )


async def _rate_limit_gate() -> None:
    global _last_call_monotonic
    async with _rate_lock:
        if _last_call_monotonic is not None:
            elapsed = time.monotonic() - _last_call_monotonic
            wait = _FETCH_INTERVAL - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
        _last_call_monotonic = time.monotonic()


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def search(
    query: str,
    *,
    kind: str = "person",
    max_results: int = 10,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Run a LinkedIn person/company search through the configured broker."""
    if kind not in _VALID_KINDS:
        raise ValueError(
            f"unknown kind {kind!r}; expected one of {sorted(_VALID_KINDS)}"
        )

    broker, recipe = _resolve_broker()
    api_key = _resolve_key(broker, recipe)

    if kind == "person":
        url = recipe["search_person_url"]
        params = recipe["build_search_person_params"](query, max_results)
        parser = recipe["parse_search_person"]
    else:
        url = recipe["search_company_url"]
        params = recipe["build_search_company_params"](query, max_results)
        parser = recipe["parse_search_company"]

    headers = {
        "Accept": "application/json",
        "User-Agent": _user_agent(),
        **recipe["build_headers"](api_key),
    }

    await _rate_limit_gate()

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:
            response = await client.get(url, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("linkedin search failed for %r: %s", query, exc)
        return []

    if response.status_code != 200:
        logger.warning(
            "linkedin search returned HTTP %s for %r",
            response.status_code,
            query,
        )
        return []

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("linkedin search returned non-JSON for %r: %s", query, exc)
        return []

    if not isinstance(payload, dict):
        return []

    rows = parser(payload)
    out: list[SearchResult] = []
    for row in rows[:max_results]:
        extras: dict[str, Any] = {"kind": kind, "broker": broker}
        if kind == "person":
            extras.update(
                {
                    "location": row.get("location") or "",
                    "current_company": row.get("current_company") or "",
                    "current_title": row.get("current_title") or "",
                }
            )
        else:
            extras.update(
                {
                    "industry": row.get("industry") or "",
                    "headcount": row.get("headcount") or "",
                    "hq_location": row.get("hq_location") or "",
                }
            )
        out.append(
            SearchResult(
                url=str(row.get("url") or ""),
                title=str(row.get("title") or ""),
                snippet=str(row.get("snippet") or ""),
                source_kind="linkedin",
                extras=extras,
            )
        )
    return out


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def _classify_url(url: str) -> str | None:
    if _PERSON_URL_RE.match(url):
        return "person"
    if _COMPANY_URL_RE.match(url):
        return "company"
    return None


def _looks_like_linkedin(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.split("@")[-1].split(":", 1)[0].lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _render_person_markdown(facts: dict[str, Any]) -> str:
    lines: list[str] = [f"# {facts.get('name') or '(unknown)'}"]
    headline = facts.get("headline") or ""
    if headline:
        lines.append(headline)

    experiences = facts.get("experiences") or []
    if experiences:
        lines.append("")
        lines.append("## Experience")
        for exp in experiences:
            title = exp.get("title") or "?"
            company = exp.get("company") or "?"
            starts = exp.get("starts_at") or "?"
            ends = exp.get("ends_at") or "present"
            entry = f"- {title} @ {company} ({starts} – {ends})"
            location = exp.get("location") or ""
            if location:
                entry += f" — {location}"
            lines.append(entry)

    education = facts.get("education") or []
    if education:
        lines.append("")
        lines.append("## Education")
        for edu in education:
            school = edu.get("school") or "?"
            degree = edu.get("degree") or ""
            field = edu.get("field") or ""
            starts = edu.get("starts_at") or ""
            ends = edu.get("ends_at") or ""
            extras = ", ".join(p for p in [degree, field] if p)
            range_str = " – ".join(p for p in [starts, ends] if p)
            entry = f"- {school}"
            if extras:
                entry += f" — {extras}"
            if range_str:
                entry += f" ({range_str})"
            lines.append(entry)

    certifications = facts.get("certifications") or []
    if certifications:
        lines.append("")
        lines.append("## Certifications")
        for c in certifications:
            name = c.get("name") or "?"
            authority = c.get("authority") or ""
            entry = f"- {name}"
            if authority:
                entry += f" — {authority}"
            lines.append(entry)

    skills = facts.get("skills") or []
    if skills:
        lines.append("")
        lines.append("## Skills")
        lines.append(", ".join(str(s) for s in skills))

    return "\n".join(lines)


def _render_company_markdown(facts: dict[str, Any]) -> str:
    lines: list[str] = [f"# {facts.get('name') or '(unknown company)'}"]
    tagline = facts.get("tagline") or ""
    if tagline:
        lines.append(tagline)

    summary_parts: list[str] = []
    industry = facts.get("industry") or ""
    if industry:
        summary_parts.append(f"Industry: {industry}")
    employee_count = facts.get("employee_count")
    if employee_count:
        summary_parts.append(f"Employees: {employee_count}")
    headcount = facts.get("headcount") or ""
    if headcount and not employee_count:
        summary_parts.append(f"Headcount range: {headcount}")
    hq = facts.get("hq_location") or ""
    if hq:
        summary_parts.append(f"HQ: {hq}")
    if summary_parts:
        lines.append("")
        lines.append(" · ".join(summary_parts))

    description = facts.get("description") or ""
    if description:
        lines.append("")
        lines.append("## About")
        lines.append(description)

    locations = facts.get("locations") or []
    if locations:
        lines.append("")
        lines.append("## Locations")
        for loc in locations:
            lines.append(f"- {loc}")

    specialities = facts.get("specialities") or []
    if specialities:
        lines.append("")
        lines.append("## Specialities")
        lines.append(", ".join(str(s) for s in specialities))

    updates = facts.get("updates") or []
    if updates:
        lines.append("")
        lines.append("## Recent updates")
        for u in updates:
            lines.append(f"- {u}")

    return "\n".join(lines)


async def fetch(url: str, timeout: float = 30.0) -> Source | None:
    """Fetch a LinkedIn person/company profile and return a :class:`Source`.

    Returns ``None`` when ``url`` is not a LinkedIn person/company URL or
    when the broker call fails for any reason — the planner must never
    crash on a connector error.
    """
    if not url or not _looks_like_linkedin(url):
        return None

    kind = _classify_url(url)
    if kind is None:
        return None

    broker, recipe = _resolve_broker()
    api_key = _resolve_key(broker, recipe)

    if kind == "person":
        endpoint = recipe["fetch_person_url"]
        params = recipe["build_fetch_person_params"](url)
        parser = recipe["parse_fetch_person"]
    else:
        endpoint = recipe["fetch_company_url"]
        params = recipe["build_fetch_company_params"](url)
        parser = recipe["parse_fetch_company"]

    headers = {
        "Accept": "application/json",
        "User-Agent": _user_agent(),
        **recipe["build_headers"](api_key),
    }

    await _rate_limit_gate()

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:
            response = await client.get(endpoint, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("linkedin fetch failed for %s: %s", url, exc)
        return None

    if response.status_code >= 400:
        logger.warning(
            "linkedin fetch returned HTTP %s for %s",
            response.status_code,
            url,
        )
        return None

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("linkedin fetch returned non-JSON for %s: %s", url, exc)
        return None

    if not isinstance(payload, dict):
        return None

    facts = parser(payload)
    if kind == "person":
        cleaned_text = _render_person_markdown(facts)
        title = facts.get("name") or url
        metadata: dict[str, Any] = {
            "broker": broker,
            "broker_payload": payload,
            "profile_url": url,
            "kind": "person",
            "headline": facts.get("headline") or "",
            "employment_history": facts.get("experiences") or [],
            "education": facts.get("education") or [],
            "certifications": facts.get("certifications") or [],
            "skills": facts.get("skills") or [],
        }
    else:
        cleaned_text = _render_company_markdown(facts)
        title = facts.get("name") or url
        metadata = {
            "broker": broker,
            "broker_payload": payload,
            "profile_url": url,
            "kind": "company",
            "industry": facts.get("industry") or "",
            "headcount": facts.get("headcount") or "",
            "employee_count": facts.get("employee_count"),
            "hq_location": facts.get("hq_location") or "",
            "locations": facts.get("locations") or [],
            "specialities": facts.get("specialities") or [],
            "updates": facts.get("updates") or [],
        }

    return Source(
        url=url,
        title=str(title),
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="linkedin",
        metadata=metadata,
    )


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


__all__ = ["fetch", "reset_for_tests", "search"]
