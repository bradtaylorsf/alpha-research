"""OFAC + EU + UK sanctions screening connector (issue #116).

Public surface:

* ``async def search(query, *, max_results=20, kinds=None) -> list[SearchResult]``
  searches the local SDN / EU / UK sanctions index by name / alias / EIN /
  passport. Returns matched designation, sanctioning agency, designation date,
  list (SDN, FSE, NS-PLC, EU, UK), and a permalink.
* ``async def fetch(url) -> Source | None`` opens an OFAC sanctions-search
  details page, the OFAC Recent Actions listing, or an EU/UK list URL and
  returns a markdown roll-up with cite.

Treasury's SDN list (XML/CSV/JSON) is free and authoritative; commercial
screening services charge thousands for a cleaned-up version. The connector
downloads the bulk file on startup (or whenever the cached index is older
than 24h), parses it into a local SQLite + FTS5 index, and lets the agent
auto-screen any named individual / company.

Future direction: this is structured cleanly enough that the parsed,
queryable JSON could ship as a stand-alone "clean parsed sanctions API"
side product. Keep search/fetch decoupled from the rest of the agent so
that's a viable spin-out.

Data sources:

* SDN Advanced XML — https://www.treasury.gov/ofac/downloads/sdn_advanced.xml
* OFAC Recent Actions — https://home.treasury.gov/policy-issues/financial-sanctions/recent-actions
* EU Consolidated — https://webgate.ec.europa.eu/europeaid/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content
* UK OFSI — https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.csv

Indexed in a dedicated SQLite DB (``data/sanctions.sqlite`` by default,
overridable via ``SANCTIONS_DB_PATH``) so refreshes can be done atomically
without disturbing the main project index.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import sqlite3
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from research_agent import config
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

SDN_ADVANCED_URL = "https://www.treasury.gov/ofac/downloads/sdn_advanced.xml"
EU_CONSOLIDATED_URL = (
    "https://webgate.ec.europa.eu/europeaid/fsd/fsf/public/files/"
    "xmlFullSanctionsList_1_1/content"
)
UK_OFSI_URL = (
    "https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.csv"
)
RECENT_ACTIONS_URL = (
    "https://home.treasury.gov/policy-issues/financial-sanctions/recent-actions"
)
SDN_DETAILS_BASE = "https://sanctionssearch.ofac.treas.gov/Details.aspx"

_DEFAULT_DB_PATH = Path("data/sanctions.sqlite")
_CACHE_TTL_SECONDS = 24 * 60 * 60
_RATE_LIMIT_INTERVAL = 0.5

_ACCEPTED_HOSTS = frozenset(
    {
        "sanctionssearch.ofac.treas.gov",
        "home.treasury.gov",
        "www.treasury.gov",
        "webgate.ec.europa.eu",
        "ofsistorage.blob.core.windows.net",
        "www.gov.uk",
    }
)

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None
_index_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Config / paths / rate-limit
# ---------------------------------------------------------------------------


def _index_path() -> Path:
    raw = os.environ.get("SANCTIONS_DB_PATH") or config.get("SANCTIONS_DB_PATH")
    if raw:
        return Path(raw)
    return _DEFAULT_DB_PATH


def _headers() -> dict[str, str]:
    return {
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
# HTTP helpers
# ---------------------------------------------------------------------------


async def _http_get(url: str, *, timeout: float = 60.0) -> tuple[int | None, bytes]:
    """GET ``url`` and return ``(status, body_bytes)``. Status ``None`` on transport error."""
    await _rate_limit_gate()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout, headers=_headers()
        ) as client:
            response = await client.get(url)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("sanctions GET failed for %s: %s", url, exc)
        return None, b""
    return response.status_code, response.content


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Lowercase + strip diacritics + drop punctuation. For fuzzy matching."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    no_marks = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in no_marks)
    return " ".join(cleaned.split())


# ---------------------------------------------------------------------------
# SDN Advanced XML parsing
# ---------------------------------------------------------------------------


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _findall_by_local(root: ET.Element, name: str) -> list[ET.Element]:
    """Find all descendants whose local-name (namespace-stripped) equals ``name``."""
    return [el for el in root.iter() if _strip_ns(el.tag) == name]


def _children_by_local(parent: ET.Element, name: str) -> list[ET.Element]:
    return [el for el in list(parent) if _strip_ns(el.tag) == name]


def _child_text(parent: ET.Element, name: str) -> str:
    for el in list(parent):
        if _strip_ns(el.tag) == name and el.text:
            return el.text.strip()
    return ""


def _parse_sdn_advanced(payload: bytes) -> list[dict[str, Any]]:
    """Parse SDN advanced XML bytes and return a list of normalized entry dicts.

    Tolerates both the real OFAC schema and the simplified shape we use in
    fixtures: any element with a local-name of ``sdnEntry`` that contains
    ``uid``, ``firstName``/``lastName``/``sdnType`` siblings is accepted.
    """
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        logger.warning("sanctions SDN parse failed: %s", exc)
        return []

    entries: list[dict[str, Any]] = []
    for entry in _findall_by_local(root, "sdnEntry"):
        uid = _child_text(entry, "uid")
        if not uid:
            continue
        sdn_type = _child_text(entry, "sdnType") or "Individual"
        first = _child_text(entry, "firstName")
        last = _child_text(entry, "lastName")
        full = " ".join(p for p in (first, last) if p).strip()
        if not full:
            full = last or first
        if not full:
            continue

        programs: list[str] = []
        program_list = _children_by_local(entry, "programList")
        if program_list:
            for el in _findall_by_local(program_list[0], "program"):
                if el.text:
                    programs.append(el.text.strip())

        aliases: list[dict[str, str]] = []
        aka_list = _children_by_local(entry, "akaList")
        if aka_list:
            for aka in _children_by_local(aka_list[0], "aka"):
                a_first = _child_text(aka, "firstName")
                a_last = _child_text(aka, "lastName")
                a_name = " ".join(p for p in (a_first, a_last) if p).strip()
                if not a_name:
                    continue
                aliases.append(
                    {
                        "name": a_name,
                        "type": _child_text(aka, "type") or _child_text(aka, "category"),
                    }
                )

        ids: list[dict[str, str]] = []
        id_list = _children_by_local(entry, "idList")
        if id_list:
            for id_el in _children_by_local(id_list[0], "id"):
                kind = _child_text(id_el, "idType")
                value = _child_text(id_el, "idNumber")
                if kind and value:
                    ids.append({"kind": kind, "value": value})

        addresses: list[str] = []
        addr_list = _children_by_local(entry, "addressList")
        if addr_list:
            for addr in _children_by_local(addr_list[0], "address"):
                bits = [
                    _child_text(addr, key)
                    for key in (
                        "address1",
                        "address2",
                        "city",
                        "stateOrProvince",
                        "postalCode",
                        "country",
                    )
                    if _child_text(addr, key)
                ]
                if bits:
                    addresses.append(", ".join(bits))

        designation_date = _child_text(entry, "publishDate") or _child_text(
            entry, "designationDate"
        )
        sanctioning_agency = _child_text(entry, "sanctioningAgency") or "OFAC"
        list_kind = _child_text(entry, "listKind") or "SDN"

        entries.append(
            {
                "uid": uid,
                "name": full,
                "type": sdn_type,
                "programs": programs,
                "aliases": aliases,
                "ids": ids,
                "addresses": addresses,
                "designation_date": designation_date,
                "sanctioning_agency": sanctioning_agency,
                "list_kind": list_kind,
            }
        )
    return entries


# ---------------------------------------------------------------------------
# EU + UK list parsing
# ---------------------------------------------------------------------------


def _parse_eu(payload: bytes) -> list[dict[str, Any]]:
    """Parse EU consolidated XML.

    Accepts either real EU schema (``ENTITY`` elements with name/alias children)
    or the simplified fixture shape. Best-effort: returns ``[]`` on parse error.
    """
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        logger.warning("sanctions EU parse failed: %s", exc)
        return []

    entries: list[dict[str, Any]] = []
    candidates: list[ET.Element] = []
    for tag in ("entity", "ENTITY", "sanctionEntity"):
        candidates.extend(_findall_by_local(root, tag))
    for entry in candidates:
        uid = (
            _child_text(entry, "uid")
            or _child_text(entry, "logicalId")
            or entry.get("logicalId")
            or entry.get("Id")
            or ""
        )
        if not uid:
            continue
        first = _child_text(entry, "firstName") or _child_text(entry, "wholeName")
        last = _child_text(entry, "lastName")
        name = " ".join(p for p in (first, last) if p).strip() or _child_text(
            entry, "wholeName"
        )
        if not name:
            continue
        designation_date = _child_text(entry, "publishDate") or _child_text(
            entry, "designationDate"
        )
        programs_text = _child_text(entry, "programme") or _child_text(entry, "programs")
        programs = [p.strip() for p in programs_text.split(",") if p.strip()]
        aliases_raw: list[ET.Element] = []
        for tag in ("nameAlias", "alias"):
            aliases_raw.extend(_findall_by_local(entry, tag))
        aliases = []
        for el in aliases_raw:
            a_name = (el.text or "").strip() or _child_text(el, "wholeName")
            if a_name:
                aliases.append({"name": a_name, "type": el.get("type") or ""})
        entries.append(
            {
                "uid": f"EU-{uid}",
                "name": name,
                "type": entry.get("type") or _child_text(entry, "subjectType") or "Entity",
                "programs": programs,
                "aliases": aliases,
                "ids": [],
                "addresses": [],
                "designation_date": designation_date,
                "sanctioning_agency": "EU Council",
                "list_kind": "EU",
            }
        )
    return entries


def _parse_uk(payload: bytes) -> list[dict[str, Any]]:
    """Parse UK OFSI consolidated CSV.

    The 2022 OFSI format has columns including ``Group ID``, ``Name 6`` (the
    "primary" name), ``DOB``, ``Regime`` (program), ``Listed On``. We tolerate
    minor column shape variation by matching headers case-insensitively.
    """
    try:
        text = payload.decode("utf-8-sig", errors="replace")
    except Exception as exc:  # pragma: no cover
        logger.warning("sanctions UK decode failed: %s", exc)
        return []
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return []

    field_map = {(name or "").strip().lower(): name for name in reader.fieldnames}

    def col(*candidates: str) -> str | None:
        for c in candidates:
            key = c.lower()
            if key in field_map:
                return field_map[key]
        return None

    name_col = col("name 6", "name", "primary name")
    group_col = col("group id", "group_id", "groupid")
    regime_col = col("regime", "program", "programme")
    listed_col = col("listed on", "designation date")
    type_col = col("group type", "type")

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    grouped: dict[str, dict[str, Any]] = {}
    for row in reader:
        if not row:
            continue
        primary = (row.get(name_col) or "").strip() if name_col else ""
        group_id = (row.get(group_col) or "").strip() if group_col else ""
        if not primary or not group_id:
            continue
        record = grouped.setdefault(
            group_id,
            {
                "uid": f"UK-{group_id}",
                "name": primary,
                "type": (row.get(type_col) or "Entity").strip() if type_col else "Entity",
                "programs": [(row.get(regime_col) or "").strip()]
                if regime_col and (row.get(regime_col) or "").strip()
                else [],
                "aliases": [],
                "ids": [],
                "addresses": [],
                "designation_date": (row.get(listed_col) or "").strip()
                if listed_col
                else "",
                "sanctioning_agency": "UK OFSI",
                "list_kind": "UK",
            },
        )
        # Subsequent rows for the same group id are aliases.
        if record["name"] != primary and primary not in seen:
            record["aliases"].append({"name": primary, "type": "alias"})
            seen.add(primary)
    entries.extend(grouped.values())
    return entries


# ---------------------------------------------------------------------------
# SQLite schema + index build
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    uid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT,
    programs TEXT,
    designation_date TEXT,
    list_kind TEXT,
    sanctioning_agency TEXT,
    source_url TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS aliases (
    uid TEXT,
    alias TEXT,
    type TEXT,
    FOREIGN KEY (uid) REFERENCES entries(uid)
);

CREATE TABLE IF NOT EXISTS ids (
    uid TEXT,
    kind TEXT,
    value TEXT,
    FOREIGN KEY (uid) REFERENCES entries(uid)
);

CREATE INDEX IF NOT EXISTS idx_aliases_uid ON aliases(uid);
CREATE INDEX IF NOT EXISTS idx_ids_uid ON ids(uid);
CREATE INDEX IF NOT EXISTS idx_ids_value ON ids(value);
CREATE INDEX IF NOT EXISTS idx_entries_list_kind ON entries(list_kind);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    name, aliases, ein, address,
    content='', tokenize='unicode61 remove_diacritics 2'
);
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")
    return conn


def _permalink(entry: dict[str, Any]) -> str:
    list_kind = entry.get("list_kind") or "SDN"
    uid = entry.get("uid") or ""
    if list_kind in {"SDN", "FSE", "NS-PLC"}:
        # SDN Advanced uids are numeric; strip any prefix we may have added.
        bare = uid.split("-", 1)[1] if uid.startswith("OFAC-") else uid
        return f"{SDN_DETAILS_BASE}?id={bare}"
    if list_kind == "EU":
        return EU_CONSOLIDATED_URL
    if list_kind == "UK":
        return UK_OFSI_URL
    return SDN_DETAILS_BASE


def _build_index(
    db_path: Path,
    *,
    sdn_entries: list[dict[str, Any]],
    eu_entries: list[dict[str, Any]],
    uk_entries: list[dict[str, Any]],
) -> None:
    """Rebuild the SQLite index atomically.

    We write to a temp DB next to the target then ``os.replace`` so a partial
    download never poisons the queryable index.
    """
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    conn = _connect(tmp_path)
    try:
        conn.executescript(_SCHEMA)
        rows = sdn_entries + eu_entries + uk_entries
        with conn:
            for entry in rows:
                aliases_text = " ".join(a["name"] for a in entry.get("aliases", []))
                ein_text = " ".join(
                    i["value"] for i in entry.get("ids", []) if i.get("value")
                )
                address_text = " ".join(entry.get("addresses", []))
                conn.execute(
                    "INSERT OR REPLACE INTO entries "
                    "(uid, name, type, programs, designation_date, list_kind, "
                    " sanctioning_agency, source_url, raw_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry["uid"],
                        entry["name"],
                        entry.get("type"),
                        ",".join(entry.get("programs", [])),
                        entry.get("designation_date"),
                        entry.get("list_kind"),
                        entry.get("sanctioning_agency"),
                        _permalink(entry),
                        json.dumps(entry, ensure_ascii=False),
                    ),
                )
                conn.execute(
                    "DELETE FROM aliases WHERE uid = ?", (entry["uid"],)
                )
                conn.execute("DELETE FROM ids WHERE uid = ?", (entry["uid"],))
                for alias in entry.get("aliases", []):
                    conn.execute(
                        "INSERT INTO aliases (uid, alias, type) VALUES (?, ?, ?)",
                        (entry["uid"], alias["name"], alias.get("type") or ""),
                    )
                for id_row in entry.get("ids", []):
                    conn.execute(
                        "INSERT INTO ids (uid, kind, value) VALUES (?, ?, ?)",
                        (entry["uid"], id_row["kind"], id_row["value"]),
                    )
                conn.execute(
                    "INSERT INTO entries_fts (rowid, name, aliases, ein, address) "
                    "VALUES ((SELECT rowid FROM entries WHERE uid = ?), ?, ?, ?, ?)",
                    (
                        entry["uid"],
                        entry["name"],
                        aliases_text,
                        ein_text,
                        address_text,
                    ),
                )
    finally:
        conn.close()

    # Atomic swap. ``os.replace`` is atomic on POSIX + Windows for files on the
    # same filesystem.
    os.replace(tmp_path, db_path)


# ---------------------------------------------------------------------------
# Refresh logic
# ---------------------------------------------------------------------------


def _is_fresh(db_path: Path, *, now: float | None = None) -> bool:
    if not db_path.exists():
        return False
    age = (now if now is not None else time.time()) - db_path.stat().st_mtime
    return age < _CACHE_TTL_SECONDS


async def _ensure_index(
    *,
    force: bool = False,
    http_get: Callable[..., Any] = _http_get,
) -> Path:
    db_path = _index_path()
    async with _index_lock:
        if not force and _is_fresh(db_path):
            return db_path

        sdn_status, sdn_bytes = await http_get(SDN_ADVANCED_URL)
        if sdn_status is None or sdn_status >= 400 or not sdn_bytes:
            logger.warning("sanctions SDN refresh failed (status=%s)", sdn_status)
            if db_path.exists():
                # Touch the existing file so we don't retry on every search.
                db_path.touch()
                return db_path
            # No prior index AND remote failed: build an empty one so queries
            # still return ``[]`` cleanly rather than blowing up.
            _build_index(db_path, sdn_entries=[], eu_entries=[], uk_entries=[])
            return db_path

        sdn_entries = _parse_sdn_advanced(sdn_bytes)

        eu_entries: list[dict[str, Any]] = []
        try:
            eu_status, eu_bytes = await http_get(EU_CONSOLIDATED_URL)
            if eu_status and 200 <= eu_status < 300 and eu_bytes:
                eu_entries = _parse_eu(eu_bytes)
            else:
                logger.warning("sanctions EU refresh failed (status=%s)", eu_status)
        except Exception as exc:  # noqa: BLE001 — best-effort secondary list
            logger.warning("sanctions EU refresh exception: %s", exc)

        uk_entries: list[dict[str, Any]] = []
        try:
            uk_status, uk_bytes = await http_get(UK_OFSI_URL)
            if uk_status and 200 <= uk_status < 300 and uk_bytes:
                uk_entries = _parse_uk(uk_bytes)
            else:
                logger.warning("sanctions UK refresh failed (status=%s)", uk_status)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sanctions UK refresh exception: %s", exc)

        _build_index(
            db_path,
            sdn_entries=sdn_entries,
            eu_entries=eu_entries,
            uk_entries=uk_entries,
        )
        return db_path


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


def _row_to_search_result(row: sqlite3.Row, *, fuzzy: bool = False) -> SearchResult:
    raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    list_kind = row["list_kind"] or "SDN"
    programs = (row["programs"] or "").split(",") if row["programs"] else []
    programs = [p for p in programs if p]
    designation_date = row["designation_date"] or ""
    bits: list[str] = []
    if list_kind:
        bits.append(list_kind)
    if programs:
        bits.append(",".join(programs))
    snippet_head = " ".join(bits)
    snippet = (
        f"{snippet_head} — designated {designation_date}"
        if designation_date
        else snippet_head
    )

    extras: dict[str, Any] = {
        "uid": row["uid"],
        "programs": programs,
        "list_kind": list_kind,
        "designation_date": designation_date,
        "sanctioning_agency": row["sanctioning_agency"] or "",
        "type": row["type"] or "",
        "aliases": raw.get("aliases", []),
        "ids": raw.get("ids", []),
    }
    if fuzzy:
        extras["fuzzy"] = True

    published_at = None
    if designation_date:
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                published_at = datetime.strptime(designation_date, fmt).replace(
                    tzinfo=UTC
                )
                break
            except ValueError:
                continue

    return SearchResult(
        url=row["source_url"] or _permalink({"uid": row["uid"], "list_kind": list_kind}),
        title=row["name"],
        snippet=snippet,
        published_at=published_at,
        source_kind="sanctions",
        extras=extras,
    )


def _fts_query(query: str) -> str:
    """Build an FTS5 MATCH expression that's tolerant to short / odd inputs."""
    tokens = [t for t in _normalize(query).split() if t]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


def _fuzzy_search(
    conn: sqlite3.Connection, query: str, *, max_results: int
) -> list[SearchResult]:
    """Substring-on-normalized fallback when FTS returns nothing.

    Walks every row's name + aliases. Cheap enough for tens of thousands of
    rows; if the index ever grew past that, we'd swap in trigram FTS.
    """
    needle = _normalize(query)
    if not needle:
        return []
    rows = conn.execute(
        "SELECT uid, name, type, programs, designation_date, list_kind, "
        "sanctioning_agency, source_url, raw_json FROM entries"
    ).fetchall()
    matches: list[SearchResult] = []
    for row in rows:
        haystack = _normalize(row["name"])
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
        for alias in raw.get("aliases", []):
            haystack += " " + _normalize(alias.get("name", ""))
        if needle in haystack:
            matches.append(_row_to_search_result(row, fuzzy=True))
            if len(matches) >= max_results:
                break
    return matches


async def search(
    query: str,
    *,
    max_results: int = 20,
    kinds: list[str] | None = None,
    http_get: Callable[..., Any] = _http_get,
) -> list[SearchResult]:
    """Search the local sanctions index.

    ``kinds`` filters on ``list_kind`` (e.g. ``["SDN", "EU"]``). On a zero-FTS
    hit, falls back to a normalized substring scan of names + aliases and
    flags those rows with ``extras['fuzzy']=True`` so callers can downgrade
    confidence.
    """
    if not query.strip():
        return []
    db_path = await _ensure_index(http_get=http_get)
    if not db_path.exists():
        return []

    fts_expr = _fts_query(query)
    conn = _connect(db_path)
    try:
        results: list[SearchResult] = []
        if fts_expr:
            sql = (
                "SELECT e.uid AS uid, e.name AS name, e.type AS type, "
                "  e.programs AS programs, e.designation_date AS designation_date, "
                "  e.list_kind AS list_kind, e.sanctioning_agency AS sanctioning_agency, "
                "  e.source_url AS source_url, e.raw_json AS raw_json "
                "FROM entries_fts f "
                "JOIN entries e ON e.rowid = f.rowid "
                "WHERE entries_fts MATCH ? "
            )
            params: list[Any] = [fts_expr]
            if kinds:
                placeholders = ",".join("?" for _ in kinds)
                sql += f"AND e.list_kind IN ({placeholders}) "
                params.extend(kinds)
            sql += "LIMIT ?"
            params.append(max_results)
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                logger.warning("sanctions FTS query failed (%s); falling back", exc)
                rows = []
            for row in rows:
                results.append(_row_to_search_result(row))

        if not results:
            results = _fuzzy_search(conn, query, max_results=max_results)
            if kinds:
                results = [r for r in results if r.extras.get("list_kind") in kinds]

        return results[:max_results]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def _render_entry_markdown(entry: dict[str, Any]) -> str:
    name = entry.get("name") or ""
    list_kind = entry.get("list_kind") or "SDN"
    sanctioning_agency = entry.get("sanctioning_agency") or "OFAC"
    designation_date = entry.get("designation_date") or ""
    programs = entry.get("programs") or []
    type_ = entry.get("type") or ""
    aliases = entry.get("aliases") or []
    ids = entry.get("ids") or []
    addresses = entry.get("addresses") or []

    lines: list[str] = [f"# {name}"]
    meta = " · ".join(
        b
        for b in (
            list_kind,
            type_,
            sanctioning_agency,
            f"designated {designation_date}" if designation_date else "",
        )
        if b
    )
    if meta:
        lines.append(f"_{meta}_")
    if programs:
        lines.append("")
        lines.append(f"**Programs:** {', '.join(programs)}")
    if aliases:
        lines.append("")
        lines.append("## Aliases")
        lines.append("")
        for alias in aliases:
            a_type = alias.get("type") or ""
            label = f"- {alias.get('name', '')}"
            if a_type:
                label += f" _({a_type})_"
            lines.append(label)
    if ids:
        lines.append("")
        lines.append("## Identifiers")
        lines.append("")
        for id_row in ids:
            lines.append(f"- {id_row.get('kind', '?')}: {id_row.get('value', '')}")
    if addresses:
        lines.append("")
        lines.append("## Addresses")
        lines.append("")
        for addr in addresses:
            lines.append(f"- {addr}")
    return "\n".join(lines).strip()


async def _fetch_sdn_details(
    url: str, *, http_get: Callable[..., Any]
) -> Source | None:
    """Resolve an OFAC sanctionssearch Details URL via the local index."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query or "")
    raw_id = (qs.get("id") or [""])[0].strip()
    if not raw_id:
        return None

    db_path = await _ensure_index(http_get=http_get)
    if not db_path.exists():
        return None
    conn = _connect(db_path)
    try:
        # Try direct SDN uid match first (numeric uids), then any uid suffix.
        row = conn.execute(
            "SELECT uid, raw_json FROM entries WHERE uid = ?", (raw_id,)
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT uid, raw_json FROM entries WHERE uid LIKE ?",
                (f"%-{raw_id}",),
            ).fetchone()
        if row is None:
            return None
        entry = json.loads(row["raw_json"])
    finally:
        conn.close()

    body = _render_entry_markdown(entry)
    if not body:
        return None
    return Source(
        url=url,
        title=entry.get("name") or "",
        cleaned_text=body,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="sanctions",
        metadata={
            "uid": entry.get("uid"),
            "list_kind": entry.get("list_kind"),
            "designation_date": entry.get("designation_date"),
            "programs": entry.get("programs"),
            "aliases": entry.get("aliases"),
            "ids": entry.get("ids"),
            "sanctioning_agency": entry.get("sanctioning_agency"),
            "type": entry.get("type"),
        },
    )


async def _fetch_recent_actions(
    url: str, *, http_get: Callable[..., Any]
) -> Source | None:
    status, body = await http_get(url)
    if status is None or status >= 400 or not body:
        return None
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover
        return None
    # Very light scrape: collect <li> items inside the page body.
    import re

    items = re.findall(r"<li[^>]*>(.*?)</li>", text, flags=re.IGNORECASE | re.DOTALL)
    cleaned: list[str] = []
    tag_re = re.compile(r"<[^>]+>")
    for raw in items:
        plain = tag_re.sub("", raw)
        plain = " ".join(plain.split())
        if plain and len(plain) > 10:
            cleaned.append(plain)
    if not cleaned:
        cleaned = [" ".join(tag_re.sub("", text).split())[:2000]]
    md = "# OFAC Recent Actions\n\n" + "\n".join(f"- {line}" for line in cleaned[:50])
    md += f"\n\nSource: <{url}> retrieved {datetime.now(UTC).isoformat()}"
    return Source(
        url=url,
        title="OFAC Recent Actions",
        cleaned_text=md,
        raw_html=None,
        fetched_at=datetime.now(UTC),
        source_kind="sanctions",
        metadata={"list_kind": "SDN", "sanctioning_agency": "OFAC"},
    )


async def fetch(
    url: str,
    *,
    http_get: Callable[..., Any] = _http_get,
) -> Source | None:
    """Fetch a sanctions URL and return a :class:`Source`.

    Three accepted shapes:

    1. ``sanctionssearch.ofac.treas.gov/Details.aspx?id=<uid>`` — resolved
       from the local index (offline).
    2. ``home.treasury.gov/policy-issues/financial-sanctions/recent-actions`` —
       light HTML scrape of the bulleted action list.
    3. EU / UK list URLs — returns a small markdown placeholder that records
       the cite; the per-entry content lives in the search index.

    Returns ``None`` for any other host or on transport / parse error.
    """
    if not url:
        return None
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    if host not in _ACCEPTED_HOSTS:
        return None

    if host == "sanctionssearch.ofac.treas.gov" and parsed.path.lower().endswith(
        "/details.aspx"
    ):
        return await _fetch_sdn_details(url, http_get=http_get)
    if host == "home.treasury.gov" and "recent-actions" in parsed.path:
        return await _fetch_recent_actions(url, http_get=http_get)
    if host in {
        "webgate.ec.europa.eu",
        "ofsistorage.blob.core.windows.net",
        "www.gov.uk",
    }:
        list_kind = "EU" if host == "webgate.ec.europa.eu" else "UK"
        agency = "EU Council" if list_kind == "EU" else "UK OFSI"
        body = (
            f"# {agency} consolidated sanctions list\n\n"
            f"This URL is the canonical bulk feed for the {list_kind} list. "
            "Per-entry detail is queryable via `sanctions.search()` against "
            "the local index.\n\n"
            f"Source: <{url}>"
        )
        return Source(
            url=url,
            title=f"{agency} consolidated sanctions list",
            cleaned_text=body,
            raw_html=None,
            fetched_at=datetime.now(UTC),
            source_kind="sanctions",
            metadata={"list_kind": list_kind, "sanctioning_agency": agency},
        )
    return None


# ---------------------------------------------------------------------------
# Test hooks
# ---------------------------------------------------------------------------


def reset_for_tests() -> None:
    """Clear per-process rate-limit + index-lock state."""
    global _last_call_monotonic, _rate_lock, _index_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()
    _index_lock = asyncio.Lock()


__all__ = [
    "fetch",
    "reset_for_tests",
    "search",
    "SDN_ADVANCED_URL",
    "EU_CONSOLIDATED_URL",
    "UK_OFSI_URL",
]
