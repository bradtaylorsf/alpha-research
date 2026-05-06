"""Congress.gov v3 API connector (issue #99).

Public surface:

* ``async def search(query, *, kind="bill", max_results=20) -> list[SearchResult]``
  hits ``api.congress.gov/v3/`` for ``bill``, ``member``, ``committee``,
  ``hearing``, or ``congressional-record``.
* ``async def fetch(url) -> Source | None`` opens a bill / member / hearing
  page and returns rolled-up content (bill text + actions; member voting
  record + committees; hearing committees + transcript URL). Hearings are
  fetched via the v3 API endpoint — the synthesised
  ``congress.gov/congressional-hearings/...`` permalink that ``search()``
  produces is recognised and routed through.

Auth: api.data.gov key in ``DATA_GOV_API_KEY`` — same key used by the FEC
connector (one signup at https://api.data.gov/signup/ unlocks both). The
authenticated tier is 5,000 req/hr; falling back to ``DEMO_KEY`` works for
smoke but is throttled to ~40 req/hr per IP and emits a warning.

Per Congress.gov v3 spec, page size is capped at 250.

For roll-call votes, the v3 API surfaces an XML link only — the URL is
recorded in ``Source.metadata['voting_record_xml_url']`` and a stub line
points to it; the XML is not inlined into ``cleaned_text`` so downstream
parsing stays a separate concern.
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

_BASE_URL = "https://api.congress.gov/v3/"
_SITE_BASE = "https://www.congress.gov/"
# 5,000 req/hr authenticated => ~0.72s; round to 0.8s for headroom.
_RATE_LIMIT_INTERVAL = 0.8
_CACHE_DIR = Path("corpus/.cache/congress")
_CACHE_TTL = 3600.0

_VALID_KINDS = {
    "bill",
    "member",
    "committee",
    "hearing",
    "congressional-record",
}

# Page size cap per Congress.gov v3 spec.
_PAGE_SIZE_CAP = 250

# Bill type code → human-readable slug for www.congress.gov permalinks.
_BILL_TYPE_SLUG = {
    "hr": "house-bill",
    "s": "senate-bill",
    "hjres": "house-joint-resolution",
    "sjres": "senate-joint-resolution",
    "hres": "house-resolution",
    "sres": "senate-resolution",
    "hconres": "house-concurrent-resolution",
    "sconres": "senate-concurrent-resolution",
}

# www.congress.gov path patterns the fetch() classifier accepts.
_BILL_URL_RE = re.compile(
    r"^/bill/(?P<congress>\d+)(?:[a-z]{2})?-congress/"
    r"(?P<bill_slug>[a-z\-]+)/(?P<number>\d+)/?$"
)
_MEMBER_URL_RE = re.compile(r"^/member/(?P<bioguide>[A-Za-z0-9]+)/?$")
# Matches the hearing permalink that ``search()`` synthesises for hearing hits:
# ``/congressional-hearings/{congress}th-congress/{chamber}/{jacket}``. The
# www.congress.gov site does not host a stable HTML page at this path, so
# ``fetch()`` routes the URL straight to the v3 API hearing endpoint.
_HEARING_URL_RE = re.compile(
    r"^/congressional-hearings/(?P<congress>\d+)(?:[a-z]{2})?-congress/"
    r"(?P<chamber>house|senate|joint)/(?P<jacket>\d+)/?$"
)

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


# ---------------------------------------------------------------------------
# Config / rate-limit helpers
# ---------------------------------------------------------------------------


def _resolve_api_key() -> str:
    """Return the configured api.data.gov key, falling back to DEMO_KEY."""
    raw = config.get("DATA_GOV_API_KEY") or ""
    key = raw.strip()
    if not key:
        logger.warning(
            "DATA_GOV_API_KEY not set — falling back to DEMO_KEY (40 req/hr "
            "per IP). Sign up at https://api.data.gov/signup/ for the 5000 "
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


def _bill_permalink(congress: Any, bill_type: Any, number: Any) -> str:
    bt = str(bill_type or "").strip().lower()
    slug = _BILL_TYPE_SLUG.get(bt, bt)
    return f"{_SITE_BASE}bill/{congress}th-congress/{slug}/{number}"


def _member_permalink(bioguide_id: str) -> str:
    return f"{_SITE_BASE}member/{bioguide_id}"


def _committee_permalink(chamber: Any, system_code: Any) -> str:
    return f"{_SITE_BASE}committee/{str(chamber).lower()}/{system_code}"


# ---------------------------------------------------------------------------
# search() result builders
# ---------------------------------------------------------------------------


def _first_sponsor_name(sponsors: Any) -> str:
    if not isinstance(sponsors, list) or not sponsors:
        return ""
    first = sponsors[0]
    if not isinstance(first, dict):
        return ""
    name = (
        first.get("fullName")
        or first.get("name")
        or " ".join(
            x for x in (first.get("firstName"), first.get("lastName")) if x
        ).strip()
    )
    return (name or "").strip()


def _build_bill_result(hit: dict[str, Any]) -> SearchResult | None:
    title = (hit.get("title") or "").strip()
    number = hit.get("number") or hit.get("billNumber")
    bill_type = hit.get("type") or hit.get("billType")
    congress = hit.get("congress")
    if not title or not number or not bill_type or not congress:
        return None

    latest_action = hit.get("latestAction") or {}
    if not isinstance(latest_action, dict):
        latest_action = {}
    action_text = (latest_action.get("text") or "").strip()
    action_date = latest_action.get("actionDate") or latest_action.get("date")

    sponsor = _first_sponsor_name(hit.get("sponsors"))
    origin_chamber = (hit.get("originChamber") or "").strip()
    session = hit.get("session") or hit.get("sessionNumber")

    snippet_bits: list[str] = []
    snippet_bits.append(f"{str(bill_type).upper()} {number}")
    snippet_bits.append(f"{congress}th Congress")
    if session:
        snippet_bits.append(f"Sess. {session}")
    if sponsor:
        snippet_bits.append(f"Sponsor: {sponsor}")
    if action_text:
        latest = action_text if not action_date else f"{action_date} — {action_text}"
        snippet_bits.append(f"Latest: {latest}")
    snippet = " — ".join(snippet_bits)

    extras: dict[str, Any] = {
        "congress": congress,
        "session": session,
        "bill_type": str(bill_type).lower(),
        "bill_number": number,
        "sponsor": sponsor,
        "origin_chamber": origin_chamber,
        "latest_action": action_text,
        "latest_action_date": action_date,
        "api_url": hit.get("url"),
    }
    published_at = _parse_iso_date(action_date) or _parse_iso_date(
        hit.get("updateDate")
    )
    return SearchResult(
        url=_bill_permalink(congress, bill_type, number),
        title=title,
        snippet=snippet,
        published_at=published_at,
        source_kind="congress",
        extras=extras,
    )


def _build_member_result(hit: dict[str, Any]) -> SearchResult | None:
    bioguide = (hit.get("bioguideId") or "").strip()
    name = (hit.get("name") or hit.get("directOrderName") or "").strip()
    if not bioguide or not name:
        return None
    party = (hit.get("partyName") or hit.get("party") or "").strip()
    state = (hit.get("state") or "").strip()
    district = hit.get("district")
    terms_field = hit.get("terms")
    if isinstance(terms_field, dict):
        terms_list = terms_field.get("item") or []
    elif isinstance(terms_field, list):
        terms_list = terms_field
    else:
        terms_list = []
    leadership = hit.get("leadership") or []
    if not isinstance(leadership, list):
        leadership = []

    snippet_bits = [name]
    if party:
        snippet_bits.append(party)
    if state:
        snippet_bits.append(state)
    if district is not None:
        snippet_bits.append(f"District {district}")
    snippet = " — ".join(snippet_bits)

    extras: dict[str, Any] = {
        "bioguide_id": bioguide,
        "party": party,
        "state": state,
        "district": district,
        "terms": terms_list,
        "leadership": leadership,
        "api_url": hit.get("url"),
    }
    return SearchResult(
        url=_member_permalink(bioguide),
        title=name,
        snippet=snippet,
        published_at=_parse_iso_date(hit.get("updateDate")),
        source_kind="congress",
        extras=extras,
    )


def _build_committee_result(hit: dict[str, Any]) -> SearchResult | None:
    name = (hit.get("name") or "").strip()
    system_code = (hit.get("systemCode") or "").strip()
    chamber = (hit.get("chamber") or "").strip()
    if not name or not system_code or not chamber:
        return None
    chair = ""
    chair_field = hit.get("chair") or hit.get("currentChair")
    if isinstance(chair_field, dict):
        chair = (chair_field.get("fullName") or chair_field.get("name") or "").strip()
    elif isinstance(chair_field, str):
        chair = chair_field.strip()
    committee_type = (hit.get("committeeTypeCode") or hit.get("committeeType") or "").strip()

    snippet_bits = [name, chamber]
    if chair:
        snippet_bits.append(f"Chair: {chair}")
    if committee_type:
        snippet_bits.append(committee_type)
    snippet = " — ".join(snippet_bits)

    extras: dict[str, Any] = {
        "system_code": system_code,
        "chamber": chamber,
        "chair": chair,
        "committee_type": committee_type,
        "api_url": hit.get("url"),
    }
    return SearchResult(
        url=_committee_permalink(chamber, system_code),
        title=name,
        snippet=snippet,
        published_at=_parse_iso_date(hit.get("updateDate")),
        source_kind="congress",
        extras=extras,
    )


def _build_hearing_result(hit: dict[str, Any]) -> SearchResult | None:
    title = (hit.get("title") or "").strip()
    jacket = hit.get("jacketNumber")
    congress = hit.get("congress")
    chamber = (hit.get("chamber") or "").strip()
    api_url = (hit.get("url") or "").strip()
    if not jacket or not congress:
        return None
    if not title:
        title = f"Hearing {jacket}"

    committee = ""
    committees_field = hit.get("committees") or []
    if isinstance(committees_field, list) and committees_field:
        first = committees_field[0]
        if isinstance(first, dict):
            committee = (first.get("name") or "").strip()
    elif isinstance(hit.get("committee"), str):
        committee = hit["committee"].strip()

    hearing_date = (
        hit.get("date")
        or hit.get("hearingDate")
        or hit.get("updateDate")
    )

    snippet_bits = [title]
    if committee:
        snippet_bits.append(committee)
    if chamber:
        snippet_bits.append(chamber)
    if hearing_date:
        snippet_bits.append(str(hearing_date))
    snippet_bits.append(f"Jacket {jacket}")
    snippet = " — ".join(snippet_bits)

    # Permalink: prefer API-provided url field; fall back to a constructed
    # www.congress.gov path. The `url` field on hearing search hits points at
    # api.congress.gov, so when available we build the human URL ourselves.
    permalink = (
        f"{_SITE_BASE}congressional-hearings/{congress}th-congress/{chamber.lower()}/{jacket}"
        if chamber
        else api_url
    )

    extras: dict[str, Any] = {
        "congress": congress,
        "chamber": chamber,
        "jacket_number": jacket,
        "committee": committee,
        "hearing_date": hearing_date,
        "api_url": api_url,
    }
    return SearchResult(
        url=permalink,
        title=title,
        snippet=snippet,
        published_at=_parse_iso_date(hearing_date),
        source_kind="congress",
        extras=extras,
    )


def _build_congressional_record_result(hit: dict[str, Any]) -> SearchResult | None:
    issue_date = (
        hit.get("PublishDate")
        or hit.get("Date")
        or hit.get("date")
        or hit.get("issueDate")
    )
    volume = hit.get("Volume") or hit.get("volume")
    issue_no = hit.get("Issue") or hit.get("issue")
    congress = hit.get("Congress") or hit.get("congress")
    session = hit.get("Session") or hit.get("session")
    sections = hit.get("Sections") or hit.get("sections") or []
    if not isinstance(sections, list):
        sections = []
    api_url = (hit.get("Url") or hit.get("url") or "").strip()

    if not issue_date and not volume:
        return None

    title_bits: list[str] = []
    title_bits.append("Congressional Record")
    if volume:
        title_bits.append(f"Vol. {volume}")
    if issue_no:
        title_bits.append(f"No. {issue_no}")
    if issue_date:
        title_bits.append(str(issue_date))
    title = " — ".join(title_bits)

    section_titles: list[str] = []
    for s in sections:
        if isinstance(s, dict):
            t = s.get("Name") or s.get("name") or ""
            if t:
                section_titles.append(str(t))

    snippet_bits = []
    if congress:
        snippet_bits.append(f"{congress}th Congress")
    if session:
        snippet_bits.append(f"Sess. {session}")
    if section_titles:
        snippet_bits.append("Sections: " + ", ".join(section_titles))
    snippet = " — ".join(snippet_bits) or title

    extras: dict[str, Any] = {
        "issue_date": issue_date,
        "volume": volume,
        "issue": issue_no,
        "congress": congress,
        "session": session,
        "sections": section_titles,
        "api_url": api_url,
    }
    return SearchResult(
        url=api_url or f"{_SITE_BASE}congressional-record",
        title=title,
        snippet=snippet,
        published_at=_parse_iso_date(issue_date),
        source_kind="congress",
        extras=extras,
    )


_KIND_TO_ENDPOINT: dict[str, str] = {
    "bill": "bill",
    "member": "member",
    "committee": "committee",
    "hearing": "hearing",
    "congressional-record": "congressional-record",
}

_KIND_TO_BUILDER = {
    "bill": _build_bill_result,
    "member": _build_member_result,
    "committee": _build_committee_result,
    "hearing": _build_hearing_result,
    "congressional-record": _build_congressional_record_result,
}


def _extract_results(payload: dict[str, Any], kind: str) -> list[Any]:
    """Pull the list of hits out of the API response for ``kind``.

    Congress.gov v3 uses a different top-level key per resource; the
    congressional-record endpoint nests its results under ``Results.Issues``.
    """
    if not isinstance(payload, dict):
        return []
    if kind == "bill":
        out = payload.get("bills")
    elif kind == "member":
        out = payload.get("members")
    elif kind == "committee":
        out = payload.get("committees")
    elif kind == "hearing":
        out = payload.get("hearings")
    elif kind == "congressional-record":
        results = payload.get("Results")
        if isinstance(results, dict):
            out = results.get("Issues") or results.get("issues")
        else:
            out = payload.get("issues") or payload.get("results")
    else:
        return []
    return out if isinstance(out, list) else []


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def search(
    query: str,
    *,
    kind: str = "bill",
    max_results: int = 20,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Run a Congress.gov v3 search and return up to ``max_results`` hits.

    ``kind`` selects the resource — ``bill``, ``member``, ``committee``,
    ``hearing`` or ``congressional-record``.

    Returns ``[]`` on transport / HTTP error / non-JSON body or unknown
    ``kind`` — connector failures must never crash the planner.
    """
    if kind not in _VALID_KINDS:
        logger.warning(
            "congress.search: unknown kind %r; expected one of %s",
            kind,
            sorted(_VALID_KINDS),
        )
        return []

    endpoint = _KIND_TO_ENDPOINT[kind]
    builder = _KIND_TO_BUILDER[kind]

    limit = min(max(1, int(max_results)), _PAGE_SIZE_CAP)
    params: dict[str, Any] = {
        "api_key": _resolve_api_key(),
        "query": query,
        "format": "json",
        "limit": limit,
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
        logger.warning("congress search failed for %r (%s): %s", query, kind, exc)
        return []

    if response.status_code != 200:
        logger.warning(
            "congress search returned HTTP %s for %r (%s)",
            response.status_code,
            query,
            kind,
        )
        return []

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("congress search returned non-JSON for %r: %s", query, exc)
        return []

    raw_hits = _extract_results(payload, kind)
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


def _ordinal_from_congress_segment(segment: str) -> int | None:
    """Parse e.g. ``117th-congress`` → 117."""
    m = re.match(r"^(\d+)(?:[a-z]{2})?-congress$", segment)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _bill_type_from_slug(slug: str) -> str | None:
    """Reverse-lookup ``house-bill`` → ``hr``; passthrough for raw codes."""
    slug = slug.lower()
    for code, s in _BILL_TYPE_SLUG.items():
        if s == slug:
            return code
    if slug in _BILL_TYPE_SLUG:
        return slug
    return None


def _classify_url(url: str) -> tuple[str | None, dict[str, Any]]:
    """Return ``(resource, ids)`` for supported www.congress.gov URLs.

    Strict host check — anything outside ``www.congress.gov`` (look-alikes
    like ``www.congress.gov.attacker.example`` must not pass) returns
    ``(None, {})``.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    if host != "www.congress.gov":
        return None, {}
    path = parsed.path or ""

    m = _BILL_URL_RE.match(path)
    if m:
        congress_seg = path.split("/")[2] if len(path.split("/")) > 2 else ""
        congress = _ordinal_from_congress_segment(congress_seg)
        bill_type = _bill_type_from_slug(m.group("bill_slug"))
        if congress and bill_type:
            return "bill", {
                "congress": congress,
                "bill_type": bill_type,
                "number": m.group("number"),
            }

    m = _MEMBER_URL_RE.match(path)
    if m:
        return "member", {"bioguide_id": m.group("bioguide")}

    m = _HEARING_URL_RE.match(path)
    if m:
        try:
            congress = int(m.group("congress"))
        except ValueError:
            return None, {}
        return "hearing", {
            "congress": congress,
            "chamber": m.group("chamber"),
            "jacket_number": m.group("jacket"),
        }

    return None, {}


def _cache_path(prefix: str, resource_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9]", "_", resource_id)
    return _CACHE_DIR / f"{prefix}-{safe}.json"


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def _load_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age > _CACHE_TTL:
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
        logger.warning("congress fetch failed for %s: %s", url, exc)
        return None, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


async def _fetch_json_cached(
    cache_key_prefix: str,
    cache_id: str,
    api_url: str,
    timeout: float,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    cache = _cache_path(cache_key_prefix, cache_id)
    payload = _load_cache(cache)
    if payload is not None:
        return payload
    await _rate_limit_gate()
    status, payload = await _http_get_json(api_url, timeout, params=params)
    if status is None or status >= 400 or not isinstance(payload, dict):
        if status is not None and status >= 400:
            logger.warning("congress %s HTTP %s for %s", cache_key_prefix, status, api_url)
        return None
    _write_cache(cache, payload)
    return payload


# ---------------------------------------------------------------------------
# fetch() — bill
# ---------------------------------------------------------------------------


def _bill_text_pick(payload: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Choose the most recent bill-text version + its preferred URL.

    Preference: Formatted Text → PDF → first available. Returns
    ``(format_label, url)`` or ``(None, None)``.
    """
    if not payload:
        return None, None
    versions = payload.get("textVersions") or []
    if not isinstance(versions, list) or not versions:
        return None, None
    # API returns most recent first per docs; take the first version.
    latest = next((v for v in versions if isinstance(v, dict)), None)
    if not latest:
        return None, None
    formats = latest.get("formats") or []
    if not isinstance(formats, list):
        return None, None
    by_type = {
        (f.get("type") or "").strip(): (f.get("url") or "").strip()
        for f in formats
        if isinstance(f, dict)
    }
    for label in ("Formatted Text", "PDF", "Formatted XML"):
        if by_type.get(label):
            return label, by_type[label]
    if formats and isinstance(formats[0], dict):
        return (
            (formats[0].get("type") or "").strip() or None,
            (formats[0].get("url") or "").strip() or None,
        )
    return None, None


def _bill_actions_md(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not payload:
        return []
    actions = payload.get("actions") or []
    if not isinstance(actions, list):
        return []
    out: list[dict[str, Any]] = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        out.append(
            {
                "date": a.get("actionDate") or a.get("date"),
                "text": (a.get("text") or "").strip(),
                "type": (a.get("type") or "").strip(),
            }
        )
    return out


async def _fetch_bill(
    congress: int, bill_type: str, number: str, source_url: str, timeout: float
) -> Source | None:
    common_params = {"api_key": _resolve_api_key(), "format": "json"}
    cache_id = f"{congress}-{bill_type}-{number}"

    header_payload = await _fetch_json_cached(
        "bill",
        cache_id,
        urljoin(_BASE_URL, f"bill/{congress}/{bill_type}/{number}"),
        timeout,
        params=common_params,
    )
    if not header_payload:
        return None
    bill = header_payload.get("bill")
    if not isinstance(bill, dict):
        return None

    title = (bill.get("title") or "").strip()
    if not title:
        return None

    text_payload = await _fetch_json_cached(
        "bill-text",
        cache_id,
        urljoin(_BASE_URL, f"bill/{congress}/{bill_type}/{number}/text"),
        timeout,
        params=common_params,
    )
    text_label, text_url = _bill_text_pick(text_payload)

    actions_payload = await _fetch_json_cached(
        "bill-actions",
        cache_id,
        urljoin(_BASE_URL, f"bill/{congress}/{bill_type}/{number}/actions"),
        timeout,
        params=common_params,
    )
    actions = _bill_actions_md(actions_payload)

    sponsors = bill.get("sponsors") or []
    sponsor_name = _first_sponsor_name(sponsors)
    origin_chamber = (bill.get("originChamber") or "").strip()

    summaries_field = bill.get("summaries")
    summary_text = ""
    if isinstance(summaries_field, list) and summaries_field:
        last = summaries_field[-1]
        if isinstance(last, dict):
            summary_text = (last.get("text") or "").strip()
    elif isinstance(summaries_field, dict):
        summary_text = (summaries_field.get("text") or "").strip()

    latest_action = bill.get("latestAction") or {}
    if not isinstance(latest_action, dict):
        latest_action = {}

    sections: list[str] = [f"# {title}"]
    meta_bits: list[str] = []
    meta_bits.append(f"{str(bill_type).upper()} {number}")
    meta_bits.append(f"{congress}th Congress")
    if origin_chamber:
        meta_bits.append(origin_chamber)
    if sponsor_name:
        meta_bits.append(f"Sponsor: {sponsor_name}")
    sections.append("_" + " · ".join(meta_bits) + "_")

    if summary_text:
        sections.append("## Summary\n\n" + summary_text)

    if actions:
        lines = ["## Actions", "", "| Date | Action |", "| --- | --- |"]
        for a in actions[:50]:
            d = a["date"] or "—"
            t = a["text"].replace("|", "\\|") or "—"
            lines.append(f"| {d} | {t} |")
        sections.append("\n".join(lines))
    elif latest_action.get("text"):
        sections.append(
            "## Actions\n\n"
            f"- {latest_action.get('actionDate', '—')} — "
            f"{latest_action.get('text')}"
        )

    if text_url:
        sections.append(
            "## Text\n\n"
            f"- Format: {text_label or '—'}\n"
            f"- URL: {text_url}"
        )
    else:
        sections.append("## Text\n\n_No public text available yet._")

    cleaned_text = "\n\n".join(sections).strip()

    metadata: dict[str, Any] = {
        "congress": congress,
        "bill_type": bill_type,
        "bill_number": number,
        "sponsor": sponsor_name,
        "origin_chamber": origin_chamber,
        "latest_action": latest_action,
        "text_url": text_url,
        "text_format": text_label,
        "actions": actions,
    }

    return Source(
        url=source_url,
        title=title,
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="congress",
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# fetch() — member
# ---------------------------------------------------------------------------


def _short_bill_line(item: dict[str, Any]) -> str:
    title = (item.get("title") or "").strip()
    bill_type = (item.get("type") or item.get("billType") or "").strip().upper()
    number = item.get("number") or item.get("billNumber") or ""
    congress = item.get("congress") or ""
    latest = item.get("latestAction") or {}
    if isinstance(latest, dict):
        latest_text = (latest.get("text") or "").strip()
        latest_date = latest.get("actionDate") or ""
    else:
        latest_text, latest_date = "", ""
    head = f"{bill_type} {number} ({congress})" if bill_type and number else title
    body = title if head != title and title else ""
    suffix = f" — {latest_date} {latest_text}".strip(" —") if latest_text else ""
    return " — ".join(x for x in (head, body, suffix) if x)


def _member_voting_record_url(payload: dict[str, Any] | None) -> str | None:
    """Find the roll-call XML link in a member-detail response, if any."""
    if not payload:
        return None
    member = payload.get("member") if isinstance(payload, dict) else None
    if not isinstance(member, dict):
        member = payload
    # Surfaced under a few different keys depending on member status; look
    # for any field that looks like a roll-call XML pointer.
    for key in ("rollCallVotes", "rollCallVotesXml", "votingRecordXmlUrl"):
        v = member.get(key)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict):
            url = v.get("url") or v.get("xmlUrl")
            if isinstance(url, str) and url:
                return url
    return None


async def _fetch_member(bioguide_id: str, source_url: str, timeout: float) -> Source | None:
    common_params = {"api_key": _resolve_api_key(), "format": "json"}

    header_payload = await _fetch_json_cached(
        "member",
        bioguide_id,
        urljoin(_BASE_URL, f"member/{bioguide_id}"),
        timeout,
        params=common_params,
    )
    if not header_payload:
        return None
    member = header_payload.get("member")
    if not isinstance(member, dict):
        return None

    name = (
        member.get("directOrderName")
        or member.get("invertedOrderName")
        or member.get("name")
        or ""
    ).strip()
    if not name:
        return None
    party = (
        member.get("partyName")
        or member.get("currentParty")
        or member.get("party")
        or ""
    ).strip()
    state = (member.get("state") or "").strip()
    district = member.get("district")

    terms_field = member.get("terms")
    if isinstance(terms_field, dict):
        terms_list = terms_field.get("item") or []
    elif isinstance(terms_field, list):
        terms_list = terms_field
    else:
        terms_list = []

    sponsored = await _fetch_json_cached(
        "member-sponsored",
        bioguide_id,
        urljoin(_BASE_URL, f"member/{bioguide_id}/sponsored-legislation"),
        timeout,
        params={**common_params, "limit": 10},
    )
    cosponsored = await _fetch_json_cached(
        "member-cosponsored",
        bioguide_id,
        urljoin(_BASE_URL, f"member/{bioguide_id}/cosponsored-legislation"),
        timeout,
        params={**common_params, "limit": 10},
    )

    voting_xml_url = _member_voting_record_url(header_payload)

    sections: list[str] = [f"# {name}"]
    meta_bits = [b for b in (party, state) if b]
    if district is not None:
        meta_bits.append(f"District {district}")
    if meta_bits:
        sections.append("_" + " · ".join(meta_bits) + "_")

    committees_field = member.get("committees")
    committee_names: list[str] = []
    if isinstance(committees_field, list):
        for c in committees_field:
            if isinstance(c, dict):
                cn = (c.get("name") or "").strip()
                if cn:
                    committee_names.append(cn)
    elif isinstance(committees_field, dict):
        items = committees_field.get("item") or []
        if isinstance(items, list):
            for c in items:
                if isinstance(c, dict):
                    cn = (c.get("name") or "").strip()
                    if cn:
                        committee_names.append(cn)
    if committee_names:
        sections.append(
            "## Committees\n\n" + "\n".join(f"- {c}" for c in committee_names)
        )

    sponsored_items = (sponsored or {}).get("sponsoredLegislation") or []
    if isinstance(sponsored_items, list) and sponsored_items:
        lines = ["## Recent sponsored bills", ""]
        for it in sponsored_items[:10]:
            if not isinstance(it, dict):
                continue
            lines.append(f"- {_short_bill_line(it)}")
        sections.append("\n".join(lines))

    cosponsored_items = (cosponsored or {}).get("cosponsoredLegislation") or []
    if isinstance(cosponsored_items, list) and cosponsored_items:
        lines = ["## Recent cosponsored bills", ""]
        for it in cosponsored_items[:10]:
            if not isinstance(it, dict):
                continue
            lines.append(f"- {_short_bill_line(it)}")
        sections.append("\n".join(lines))

    if voting_xml_url:
        sections.append(
            "## Voting record\n\n"
            f"_Roll-call votes are surfaced as raw XML by the v3 API; "
            f"parse downstream._\n\n- XML: {voting_xml_url}"
        )

    cleaned_text = "\n\n".join(sections).strip()

    metadata: dict[str, Any] = {
        "bioguide_id": bioguide_id,
        "party": party,
        "state": state,
        "district": district,
        "terms": terms_list,
        "committees": committee_names,
        "sponsored_count": len(sponsored_items) if isinstance(sponsored_items, list) else 0,
        "cosponsored_count": len(cosponsored_items)
        if isinstance(cosponsored_items, list)
        else 0,
    }
    if voting_xml_url:
        metadata["voting_record_xml_url"] = voting_xml_url

    return Source(
        url=source_url,
        title=name,
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="congress",
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# fetch() — hearing
# ---------------------------------------------------------------------------


def _hearing_transcript_pick(formats: Any) -> tuple[str | None, str | None]:
    """Pick the preferred transcript URL from a hearing ``formats`` list."""
    if not isinstance(formats, list):
        return None, None
    by_type = {
        (f.get("type") or "").strip(): (f.get("url") or "").strip()
        for f in formats
        if isinstance(f, dict)
    }
    for label in ("Formatted Text", "PDF", "Formatted XML"):
        if by_type.get(label):
            return label, by_type[label]
    if formats and isinstance(formats[0], dict):
        return (
            (formats[0].get("type") or "").strip() or None,
            (formats[0].get("url") or "").strip() or None,
        )
    return None, None


async def _fetch_hearing(
    congress: int, chamber: str, jacket: str, source_url: str, timeout: float
) -> Source | None:
    common_params = {"api_key": _resolve_api_key(), "format": "json"}
    cache_id = f"{congress}-{chamber}-{jacket}"

    payload = await _fetch_json_cached(
        "hearing",
        cache_id,
        urljoin(_BASE_URL, f"hearing/{congress}/{chamber}/{jacket}"),
        timeout,
        params=common_params,
    )
    if not payload:
        return None
    hearing = payload.get("hearing")
    if not isinstance(hearing, dict):
        return None

    title = (hearing.get("title") or "").strip() or f"Hearing {jacket}"
    citation = (hearing.get("citation") or "").strip()

    dates_field = hearing.get("dates") or []
    hearing_dates: list[str] = []
    if isinstance(dates_field, list):
        for d in dates_field:
            if isinstance(d, dict):
                v = d.get("date") or d.get("hearingDate")
                if v:
                    hearing_dates.append(str(v))
            elif isinstance(d, str):
                hearing_dates.append(d)

    committees_field = hearing.get("committees") or []
    committee_names: list[str] = []
    if isinstance(committees_field, list):
        for c in committees_field:
            if isinstance(c, dict):
                cn = (c.get("name") or "").strip()
                if cn:
                    committee_names.append(cn)

    transcript_label, transcript_url = _hearing_transcript_pick(
        hearing.get("formats")
    )

    sections: list[str] = [f"# {title}"]
    meta_bits: list[str] = [f"{congress}th Congress", chamber.title()]
    if hearing_dates:
        meta_bits.append("/".join(hearing_dates))
    if citation:
        meta_bits.append(citation)
    sections.append("_" + " · ".join(meta_bits) + "_")

    if committee_names:
        sections.append(
            "## Committees\n\n" + "\n".join(f"- {c}" for c in committee_names)
        )

    if transcript_url:
        sections.append(
            "## Transcript\n\n"
            f"_Witnesses are listed inside the official transcript; the v3 "
            f"hearing endpoint does not surface them as structured data._\n\n"
            f"- Format: {transcript_label or '—'}\n"
            f"- URL: {transcript_url}"
        )
    else:
        sections.append("## Transcript\n\n_No public transcript available yet._")

    cleaned_text = "\n\n".join(sections).strip()

    metadata: dict[str, Any] = {
        "congress": congress,
        "chamber": chamber,
        "jacket_number": jacket,
        "citation": citation,
        "dates": hearing_dates,
        "committees": committee_names,
        "transcript_url": transcript_url,
        "transcript_format": transcript_label,
    }

    return Source(
        url=source_url,
        title=title,
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="congress",
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def fetch(url: str, timeout: float = 30.0) -> Source | None:
    """Open a bill / member / hearing page and return a rolled-up Source.

    Accepts ``www.congress.gov`` URLs for bills and members, plus the
    synthetic hearing permalink that ``search()`` produces (the public
    site does not host a stable HTML page for hearings — the URL is
    routed straight to the v3 API). Returns ``None`` for unrecognised
    URLs (anything outside ``www.congress.gov``) and for any transport /
    HTTP / parse failure. Congressional-record issues are not yet
    supported by ``fetch()`` — use ``api_url`` from search results to
    drive a direct API fetch downstream if needed.
    """
    if not url:
        return None
    resource, ids = _classify_url(url)
    if resource == "bill":
        return await _fetch_bill(
            ids["congress"], ids["bill_type"], ids["number"], url, timeout
        )
    if resource == "member":
        return await _fetch_member(ids["bioguide_id"], url, timeout)
    if resource == "hearing":
        return await _fetch_hearing(
            ids["congress"], ids["chamber"], ids["jacket_number"], url, timeout
        )
    return None


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


__all__ = ["fetch", "reset_for_tests", "search"]
