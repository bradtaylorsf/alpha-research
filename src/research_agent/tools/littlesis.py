"""LittleSis connector — relational power-mapping database (issue #97).

Public surface:

* ``async def search(query, *, kind="entities", max_results=20) -> list[SearchResult]``
  hits the LittleSis REST API (``littlesis.org/api/``). ``kind`` is either
  ``entities`` (people / orgs) or ``relationships`` (positions, donations,
  family ties, ownership stakes, …).
* ``async def fetch(url) -> Source | None`` opens an entity page and returns
  markdown of the entity's roles, categorised relationships, and connected
  organisations.

LittleSis is **user-contributed** ("Wikipedia for power"). Treat results as a
**lead**, not as evidence — verify against primary sources (FEC, IRS Form
990, court records, EDGAR, …) before any factual claim ends up in a report.

No auth required. Per AC: per-host 1 RPS gate, mirroring ``tools/lda.py``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from research_agent import config
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_BASE_URL = "https://littlesis.org/api/"
_SITE_BASE = "https://littlesis.org"
_ACCEPTED_HOSTS = frozenset({"littlesis.org", "www.littlesis.org"})
# AC: per-host rate of 1 RPS.
_RATE_LIMIT_INTERVAL = 1.0

_VALID_KINDS = {"entities", "relationships"}

# Entity URLs look like /entity/<id>(-Slug)? or /api/entities/<id>. Numeric id
# is the only stable handle; the trailing slug is cosmetic.
_ENTITY_URL_RE = re.compile(
    r"^/(?:api/entities|entity)/(?P<id>\d+)(?:-[^/]*)?/?$"
)

# LittleSis category_id → human label. Stable IDs published in the API docs;
# unknown ids are tolerated and grouped under ``Other`` so a future schema
# addition doesn't crash the renderer.
_CATEGORY_LABELS: dict[int, str] = {
    1: "Position",
    2: "Education",
    3: "Membership",
    4: "Family",
    5: "Donation",
    6: "Transaction",
    7: "Lobbying",
    8: "Social",
    9: "Professional",
    10: "Ownership",
    11: "Hierarchy",
    12: "Generic",
}

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


# ---------------------------------------------------------------------------
# Config / rate-limit helpers
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity_attrs(item: Any) -> dict[str, Any]:
    """Return the entity attribute dict regardless of envelope shape.

    LittleSis returns JSON:API-flavoured records — top-level fields live
    under ``attributes``. Some endpoints flatten this on older responses,
    so we tolerate both shapes.
    """
    if not isinstance(item, dict):
        return {}
    attrs = item.get("attributes")
    if isinstance(attrs, dict):
        merged = dict(attrs)
        if "id" not in merged and "id" in item:
            merged["id"] = item["id"]
        return merged
    return item


def _entity_permalink(entity_id: Any) -> str:
    return f"{_SITE_BASE}/entity/{entity_id}"


def _entity_self_link(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    links = item.get("links")
    if not isinstance(links, dict):
        return None
    self_link = links.get("self")
    if isinstance(self_link, dict):
        web = self_link.get("web")
        return str(web) if web else None
    if isinstance(self_link, str):
        return self_link
    return None


def _category_label(category_id: Any, fallback: str = "") -> str:
    try:
        cid = int(category_id)
    except (TypeError, ValueError):
        return fallback or "Other"
    return _CATEGORY_LABELS.get(cid, fallback or "Other")


# ---------------------------------------------------------------------------
# search() builders
# ---------------------------------------------------------------------------


def _build_entity_result(item: Any) -> SearchResult | None:
    attrs = _entity_attrs(item)
    name = (attrs.get("name") or "").strip()
    entity_id = attrs.get("id")
    if not name or entity_id in (None, ""):
        return None

    primary_ext = (attrs.get("primary_ext") or "").strip()
    types = attrs.get("types") or []
    if not isinstance(types, list):
        types = []

    summary = (attrs.get("summary") or "").strip()
    blurb = (attrs.get("blurb") or "").strip()
    snippet = blurb or summary

    url = _entity_self_link(item) or _entity_permalink(entity_id)

    extras: dict[str, Any] = {
        "entity_id": entity_id,
        "primary_ext": primary_ext,
        "types": list(types),
        "summary": summary,
        "blurb": blurb,
    }
    return SearchResult(
        url=url,
        title=name,
        snippet=snippet,
        published_at=None,
        source_kind="littlesis",
        extras=extras,
    )


def _build_relationship_result(rel: Any) -> SearchResult | None:
    attrs = _entity_attrs(rel)
    rel_id = attrs.get("id")
    if rel_id in (None, ""):
        return None

    entity1_id = attrs.get("entity1_id")
    entity2_id = attrs.get("entity2_id")
    entity1_name = (attrs.get("entity1_name") or "").strip()
    entity2_name = (attrs.get("entity2_name") or "").strip()
    category_id = attrs.get("category_id")
    category_label = _category_label(category_id)
    description = (
        attrs.get("description")
        or attrs.get("description1")
        or attrs.get("description2")
        or ""
    )
    description = str(description).strip()
    amount = attrs.get("amount")
    start_date = (attrs.get("start_date") or "").strip() if attrs.get("start_date") else None
    end_date = (attrs.get("end_date") or "").strip() if attrs.get("end_date") else None

    if entity1_name and entity2_name:
        title = f"{entity1_name} → {entity2_name}"
    elif entity1_name or entity2_name:
        title = entity1_name or entity2_name
    else:
        title = f"LittleSis relationship {rel_id}"

    snippet_bits: list[str] = []
    if category_label:
        snippet_bits.append(category_label)
    if description:
        snippet_bits.append(description)
    snippet = ": ".join(snippet_bits)

    url = _entity_self_link(rel) or f"{_SITE_BASE}/relationship/{rel_id}"

    extras: dict[str, Any] = {
        "relationship_id": rel_id,
        "category_id": category_id,
        "category_label": category_label,
        "entity1_id": entity1_id,
        "entity2_id": entity2_id,
        "entity1_name": entity1_name,
        "entity2_name": entity2_name,
        # ``related_*`` keeps a one-step-removed handle handy when the
        # caller already knows which side they searched from.
        "related_id": entity2_id,
        "related_name": entity2_name,
        "amount": amount,
        "start_date": start_date,
        "end_date": end_date,
        "description": description,
    }
    return SearchResult(
        url=url,
        title=title,
        snippet=snippet,
        published_at=None,
        source_kind="littlesis",
        extras=extras,
    )


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


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
        logger.warning("littlesis GET failed for %s: %s", url, exc)
        return None, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        return response.status_code, response.json()
    except ValueError as exc:
        logger.warning("littlesis returned non-JSON for %s: %s", url, exc)
        return response.status_code, None


async def search(
    query: str,
    *,
    kind: str = "entities",
    max_results: int = 20,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Run a LittleSis search and return up to ``max_results`` hits.

    ``kind="entities"`` queries ``/api/entities/search``. ``kind="relationships"``
    runs an entity search first, then surfaces the top hit's relationships
    so a caller asking "who is this person connected to?" gets one round-trip
    of network and a flat list of typed edges.

    Returns ``[]`` on transport / HTTP error / non-JSON body or unknown
    ``kind`` — connector failures must never crash the planner.
    """
    if kind not in _VALID_KINDS:
        logger.warning(
            "littlesis.search: unknown kind %r; expected one of %s",
            kind,
            sorted(_VALID_KINDS),
        )
        return []

    await _rate_limit_gate()
    search_url = urljoin(_BASE_URL, "entities/search")
    status, payload = await _http_get_json(
        search_url,
        params={"q": query, "num": max_results},
        timeout=timeout,
    )
    if status is None or status >= 400 or not isinstance(payload, dict):
        if status is not None and status >= 400:
            logger.warning(
                "littlesis search HTTP %s for %r (%s)", status, query, kind
            )
        return []

    raw_hits = payload.get("data") or payload.get("results") or []
    if not isinstance(raw_hits, list):
        return []

    if kind == "entities":
        out: list[SearchResult] = []
        for hit in raw_hits[:max_results]:
            result = _build_entity_result(hit)
            if result is not None:
                out.append(result)
        return out

    # kind == "relationships": follow the top entity hit and list its edges.
    top_attrs: dict[str, Any] = {}
    for hit in raw_hits:
        attrs = _entity_attrs(hit)
        if attrs.get("id") not in (None, ""):
            top_attrs = attrs
            break
    top_id = top_attrs.get("id")
    if top_id in (None, ""):
        return []

    await _rate_limit_gate()
    rel_url = urljoin(_BASE_URL, f"entities/{top_id}/relationships")
    status, rel_payload = await _http_get_json(
        rel_url, params=None, timeout=timeout
    )
    if status is None or status >= 400 or not isinstance(rel_payload, dict):
        if status is not None and status >= 400:
            logger.warning(
                "littlesis relationships HTTP %s for entity %s", status, top_id
            )
        return []
    rel_hits = rel_payload.get("data") or rel_payload.get("results") or []
    if not isinstance(rel_hits, list):
        return []

    out = []
    for rel in rel_hits[:max_results]:
        result = _build_relationship_result(rel)
        if result is not None:
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def _classify_url(url: str) -> str | None:
    """Return the entity id when ``url`` is a LittleSis entity page.

    Strict host match against ``_ACCEPTED_HOSTS`` so look-alikes like
    ``littlesis.org.attacker.example`` are rejected. Accepts both the
    human-facing ``/entity/<id>(-slug)?`` form and the API form
    ``/api/entities/<id>``.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    if host not in _ACCEPTED_HOSTS:
        return None
    m = _ENTITY_URL_RE.match(parsed.path or "")
    if not m:
        return None
    return m.group("id")


def _roles_block(relationships: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Render the Position-category relationships as a roles list."""
    rows = [r for r in relationships if r.get("category_label") == "Position"]
    if not rows:
        return "", rows
    lines = ["## Roles / Positions", ""]
    for r in rows:
        label = r.get("entity2_name") or r.get("related_name") or ""
        description = r.get("description") or ""
        start = r.get("start_date") or ""
        end = r.get("end_date") or ""
        date_bits = []
        if start:
            date_bits.append(start)
        if end:
            date_bits.append(end)
        date_str = "–".join(date_bits) if date_bits else ""
        bits = [b for b in (description, label, date_str) if b]
        if bits:
            lines.append("- " + " — ".join(bits))
    return "\n".join(lines), rows


def _relationships_block(
    relationships: list[dict[str, Any]],
) -> str:
    if not relationships:
        return ""
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for r in relationships:
        label = r.get("category_label") or "Other"
        by_cat.setdefault(label, []).append(r)

    # Stable category order: known labels first (in canonical order), then
    # any unknown labels alphabetically. Keeps diff-ability across runs.
    canonical_order = list(_CATEGORY_LABELS.values())
    ordered: list[str] = [c for c in canonical_order if c in by_cat]
    extras = sorted(c for c in by_cat if c not in canonical_order)
    ordered.extend(extras)

    lines = ["## Relationships", ""]
    for label in ordered:
        lines.append(f"### {label}")
        lines.append("")
        for r in by_cat[label]:
            counterpart = r.get("entity2_name") or r.get("related_name") or "?"
            description = r.get("description") or ""
            start = r.get("start_date") or ""
            end = r.get("end_date") or ""
            amount = r.get("amount")
            bits: list[str] = [counterpart]
            if description:
                bits.append(description)
            date_bits = []
            if start:
                date_bits.append(start)
            if end:
                date_bits.append(end)
            if date_bits:
                bits.append("–".join(date_bits))
            if amount not in (None, "", "null"):
                bits.append(f"amount={amount}")
            lines.append("- " + " — ".join(bits))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _connected_orgs_block(
    relationships: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """De-duplicated list of org-side counterparts across all relationships."""
    names: list[str] = []
    seen: set[str] = set()
    for r in relationships:
        name = (r.get("entity2_name") or r.get("related_name") or "").strip()
        if not name:
            continue
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    if not names:
        return "", names
    lines = ["## Connected organizations", ""]
    for name in names:
        lines.append(f"- {name}")
    return "\n".join(lines), names


def _normalize_relationship(rel: Any) -> dict[str, Any] | None:
    attrs = _entity_attrs(rel)
    rel_id = attrs.get("id")
    if rel_id in (None, ""):
        return None
    category_id = attrs.get("category_id")
    return {
        "relationship_id": rel_id,
        "category_id": category_id,
        "category_label": _category_label(category_id),
        "entity1_id": attrs.get("entity1_id"),
        "entity2_id": attrs.get("entity2_id"),
        "entity1_name": (attrs.get("entity1_name") or "").strip(),
        "entity2_name": (attrs.get("entity2_name") or "").strip(),
        "related_id": attrs.get("entity2_id"),
        "related_name": (attrs.get("entity2_name") or "").strip(),
        "description": (
            (attrs.get("description") or "")
            or (attrs.get("description1") or "")
            or (attrs.get("description2") or "")
        ).strip(),
        "amount": attrs.get("amount"),
        "start_date": (attrs.get("start_date") or "").strip() if attrs.get("start_date") else None,
        "end_date": (attrs.get("end_date") or "").strip() if attrs.get("end_date") else None,
    }


async def fetch(url: str, timeout: float = 30.0) -> Source | None:
    """Open a LittleSis entity page and return a :class:`Source`.

    Returns ``None`` for anything outside ``_ACCEPTED_HOSTS`` or paths other
    than ``/entity/<id>`` / ``/api/entities/<id>``, and for any transport /
    HTTP / parse failure.
    """
    if not url:
        return None
    entity_id = _classify_url(url)
    if not entity_id:
        return None

    entity_url = urljoin(_BASE_URL, f"entities/{entity_id}")
    await _rate_limit_gate()
    status, payload = await _http_get_json(entity_url, params=None, timeout=timeout)
    if status is None or status >= 400 or not isinstance(payload, dict):
        if status is not None and status >= 400:
            logger.warning("littlesis entity HTTP %s for %s", status, entity_url)
        return None

    data = payload.get("data") or payload
    attrs = _entity_attrs(data)
    name = (attrs.get("name") or "").strip()
    if not name:
        return None
    primary_ext = (attrs.get("primary_ext") or "").strip()
    types = attrs.get("types") or []
    if not isinstance(types, list):
        types = []
    summary = (attrs.get("summary") or "").strip()
    blurb = (attrs.get("blurb") or "").strip()

    rel_url = urljoin(_BASE_URL, f"entities/{entity_id}/relationships")
    await _rate_limit_gate()
    rel_status, rel_payload = await _http_get_json(
        rel_url, params=None, timeout=timeout
    )
    relationships: list[dict[str, Any]] = []
    if rel_status == 200 and isinstance(rel_payload, dict):
        raw_rels = rel_payload.get("data") or rel_payload.get("results") or []
        if isinstance(raw_rels, list):
            for rel in raw_rels:
                norm = _normalize_relationship(rel)
                if norm is not None:
                    relationships.append(norm)

    permalink = _entity_permalink(entity_id)
    meta_bits = [b for b in (primary_ext, ", ".join(types) if types else "", permalink) if b]
    meta_line = "_" + " · ".join(meta_bits) + "_" if meta_bits else ""

    sections = [f"# {name}"]
    if meta_line:
        sections.append(meta_line)
    summary_text = summary or blurb
    if summary_text:
        sections.append("## Summary\n\n" + summary_text)

    roles_md, _roles_rows = _roles_block(relationships)
    if roles_md:
        sections.append(roles_md)

    rels_md = _relationships_block(relationships)
    if rels_md:
        sections.append(rels_md.rstrip())

    orgs_md, connected_orgs = _connected_orgs_block(relationships)
    if orgs_md:
        sections.append(orgs_md)

    cleaned_text = "\n\n".join(sections).strip()
    if not cleaned_text:
        return None

    metadata: dict[str, Any] = {
        "entity_id": entity_id,
        "primary_ext": primary_ext,
        "types": list(types),
        "summary": summary,
        "blurb": blurb,
        "relationships": relationships,
        "connected_orgs": connected_orgs,
    }

    return Source(
        url=url,
        title=name,
        cleaned_text=cleaned_text,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="littlesis",
        metadata=metadata,
    )


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


__all__ = ["fetch", "reset_for_tests", "search"]
