"""Synthesis module — turns findings into a sourced narrative report.

Implements the synthesizer pass from the implementation guide: cloud
``frontier`` tier with the ``synthesizer.md`` prompt. The module reads the
top N findings (by confidence) plus the sources they cite, the prior
synthesis (if any), and the latest critique (if any), packs them into a
JSON context payload, and asks the model to emit a markdown report.

Two public entry points:

* :func:`synthesize` — the in-loop pass triggered by the loop's
  ``HEURISTIC_CHECK_EVERY_N`` cadence. Defaults to the top
  :data:`TOP_N_FINDINGS` findings.
* :func:`final_synthesis` — the end-of-job pass; uses the larger
  :data:`FINAL_TOP_N` window and tags the context with ``final=True`` so
  the prompt knows to be more thorough.

Both functions persist via :func:`write_synthesis` and rotate ``report.md``
into ``report.history/`` via :func:`write_report` (atomic per §16).
The loop may also write ``synthesis/low_yield.json`` as a list of
``{kind, query_stem, count, suggested_unblocker}`` records for later
Confirmed Gaps rendering.

Budget handling implements the §16 anti-pattern guard: if a frontier call
hits :class:`BudgetExceeded`, we log WARN, emit a ``warning`` event, and
fall back to ``frontier_speed``. If that *also* exceeds the cap, we write a
stub report explaining the cap and exit cleanly so the user always gets a
report file even on truncation.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import traceback
from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent

from research_agent import config
from research_agent.llm.budgets import BudgetExceeded
from research_agent.observability.events import emit
from research_agent.prompts.loader import load_prompt, load_prompt_meta
from research_agent.storage import db
from research_agent.storage.markdown import (
    assemble_report,
    latest_fragment,
    latest_fragments,
    reconcile_report_sources,
    write_fragment,
    write_report,
    write_synthesis,
    write_synthesis_failed,
)

if TYPE_CHECKING:
    from research_agent.llm.router import Router
    from research_agent.orchestrator.plan import Plan
    from research_agent.storage.jobs import Job

logger = logging.getLogger(__name__)

TOP_N_FINDINGS = 50
FINAL_TOP_N = 200
DEFAULT_FRAGMENT_TAGS: tuple[str, ...] = ("open-questions",)

_FOLLOWUP_RECIPES: str | None = None
_FOLLOWUP_RECIPES_WARN_LOGGED = False

_PAID_UNBLOCK_RECIPES: str | None = None
_PAID_UNBLOCK_RECIPES_WARN_LOGGED = False


def _load_followup_recipes() -> str:
    """Read ``prompts/followup_recipes.md`` raw and cache the body.

    The file is reference data (no YAML frontmatter), so the prompt loader
    is bypassed. A missing file is tolerated — synth must keep running
    even if an operator deletes the catalog. The WARN log fires once.
    """
    global _FOLLOWUP_RECIPES, _FOLLOWUP_RECIPES_WARN_LOGGED
    if _FOLLOWUP_RECIPES is not None:
        return _FOLLOWUP_RECIPES
    try:
        body = (files("research_agent.prompts") / "followup_recipes.md").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        if not _FOLLOWUP_RECIPES_WARN_LOGGED:
            logger.warning("synth: followup_recipes.md unavailable: %s", exc)
            _FOLLOWUP_RECIPES_WARN_LOGGED = True
        body = ""
    _FOLLOWUP_RECIPES = body
    return body


def _load_paid_unblock_recipes() -> str:
    """Read ``prompts/paid_unblock_recipes.md`` raw and cache the body.

    Same contract as :func:`_load_followup_recipes` — reference data,
    tolerates a missing file with a one-time WARN log.
    """
    global _PAID_UNBLOCK_RECIPES, _PAID_UNBLOCK_RECIPES_WARN_LOGGED
    if _PAID_UNBLOCK_RECIPES is not None:
        return _PAID_UNBLOCK_RECIPES
    try:
        body = (files("research_agent.prompts") / "paid_unblock_recipes.md").read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, OSError) as exc:
        if not _PAID_UNBLOCK_RECIPES_WARN_LOGGED:
            logger.warning("synth: paid_unblock_recipes.md unavailable: %s", exc)
            _PAID_UNBLOCK_RECIPES_WARN_LOGGED = True
        body = ""
    _PAID_UNBLOCK_RECIPES = body
    return body


_BUDGET_STUB_REPORT = (
    "# Report (truncated)\n\n"
    "Research budget cap was reached before a synthesis could be completed.\n"
    "The job's findings are still on disk under `findings/` and the source\n"
    "material under `sources/`; raise the budget cap and rerun synthesis to\n"
    "produce a full report.\n"
)


class SynthesisOutput(BaseModel):
    """Return shape from :func:`synthesize` and :func:`final_synthesis`."""

    model_config = ConfigDict(extra="forbid")

    version: int
    content: str
    model: str
    cost_usd: float | None
    report_path: str
    truncated: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fragment_aliases() -> dict[str, str]:
    from research_agent.orchestrator.fragments import all_fragments

    aliases: dict[str, str] = {}
    for fragment in all_fragments():
        aliases[fragment.id] = fragment.id
        normalized_title = _normalize_fragment_label(fragment.title)
        aliases[normalized_title] = fragment.id
    return aliases


def _normalize_fragment_label(value: str) -> str:
    text = value.strip().lower().replace("_", "-")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text


def normalize_fragment_tags(raw: Any, *, job: Job | None = None) -> list[str]:
    """Validate model-provided fragment tags against the canonical registry.

    Empty, malformed, or fully invalid model output falls back to a conservative
    fragment so new evidence still reaches fragment synthesis. Legacy storage
    callers can preserve ``NULL`` by not calling this helper.
    """

    from research_agent.orchestrator.fragments import fragment_ids

    valid = fragment_ids()
    aliases = _fragment_aliases()
    if raw is None:
        raw_items: list[Any] = []
    elif isinstance(raw, str):
        raw_items = [raw]
    elif isinstance(raw, list):
        raw_items = list(raw)
    else:
        raw_items = []

    normalized: list[str] = []
    unknown: list[str] = []
    invalid_count = 0
    for item in raw_items:
        if not isinstance(item, str) or not item.strip():
            invalid_count += 1
            continue
        candidate = _normalize_fragment_label(item)
        fragment_id = aliases.get(candidate)
        if fragment_id is None and candidate in valid:
            fragment_id = candidate
        if fragment_id is None:
            unknown.append(candidate[:80] or "<empty>")
            continue
        if fragment_id not in normalized:
            normalized.append(fragment_id)

    used_fallback = False
    if not normalized:
        normalized = list(DEFAULT_FRAGMENT_TAGS)
        used_fallback = True

    if job is not None and (unknown or invalid_count or used_fallback):
        emit(
            job,
            "WARN",
            "synth",
            "finding_fragment_classification_miss",
            {
                "raw_count": len(raw_items),
                "unknown_fragment_ids": sorted(set(unknown)),
                "invalid_count": invalid_count,
                "fallback_fragment_ids": normalized if used_fallback else [],
            },
        )
    return normalized


def _load_top_findings(job: Job, n: int) -> list[dict[str, Any]]:
    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, claim, confidence, source_ids, tags, target_fragments
            FROM findings
            WHERE job_id = ?
            ORDER BY confidence DESC, id ASC
            LIMIT ?
            """,
            (job.id, n),
        ).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        source_ids_raw = row["source_ids"]
        tags_raw = row["tags"]
        target_fragments_raw = row["target_fragments"]
        finding_id = int(row["id"])
        original_claim = row["claim"]
        translated_claim = _load_finding_translation(job, finding_id)
        item = {
            "id": finding_id,
            "claim": translated_claim or original_claim,
            "confidence": float(row["confidence"]),
            "source_ids": json.loads(source_ids_raw) if source_ids_raw else [],
            "tags": json.loads(tags_raw) if tags_raw else [],
            "target_fragments": (
                json.loads(target_fragments_raw) if target_fragments_raw else []
            ),
        }
        if translated_claim is not None:
            item["original_claim"] = original_claim
            item["translated"] = True
        out.append(item)
    return out


def _load_finding_translation(job: Job, finding_id: int) -> str | None:
    path = job.root / f"findings/{finding_id:06d}.translation.md"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            text = text[end + 4 :].strip()
    return text or None


def _load_sources_for(job: Job, finding_rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    ids: set[int] = set()
    for f in finding_rows:
        for sid in f.get("source_ids", []):
            if isinstance(sid, int):
                ids.add(sid)
    if not ids:
        return {}

    placeholders = ",".join("?" for _ in ids)
    sql = (
        f"SELECT id, url, title, fetched_at, archive_url FROM sources WHERE id IN ({placeholders})"
    )
    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(sql, tuple(ids)).fetchall()
    finally:
        conn.close()

    return {
        int(r["id"]): {
            "id": int(r["id"]),
            "url": r["url"],
            "title": r["title"],
            "fetched_at": int(r["fetched_at"]) if r["fetched_at"] is not None else None,
            "archive_url": r["archive_url"],
        }
        for r in rows
    }


def _load_prior_synthesis(job: Job) -> str | None:
    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            "SELECT md_path FROM syntheses WHERE job_id = ? ORDER BY version DESC LIMIT 1",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    md_path = job.root / row["md_path"]
    if not md_path.exists():
        return None
    return md_path.read_text(encoding="utf-8")


def _load_latest_critique(job: Job) -> str | None:
    """Best-effort read of the latest critique. Tolerates missing table (#28)."""
    try:
        conn = db.connect(job.db_path)
    except sqlite3.OperationalError:
        return None
    try:
        try:
            row = conn.execute(
                "SELECT md_path FROM critiques WHERE job_id = ? ORDER BY version DESC LIMIT 1",
                (job.id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    finally:
        conn.close()
    if row is None:
        return None
    md_path = job.root / row["md_path"]
    if not md_path.exists():
        return None
    return md_path.read_text(encoding="utf-8")


_DEPARTMENT_ALIASES: list[tuple[str, list[str]]] = [
    ("DOJ", ["DOJ", "Department of Justice", "Justice Department", "Justice"]),
    (
        "HHS",
        [
            "HHS",
            "Department of Health and Human Services",
            "Health and Human Services",
            "Department of Health",
            "Health Department",
            "FDA",
            "Food and Drug Administration",
        ],
    ),
    (
        "DOD",
        ["DOD", "Department of Defense", "Defense Department", "Pentagon", "Defense"],
    ),
    ("DHS", ["DHS", "Department of Homeland Security", "Homeland Security"]),
    (
        "Education",
        ["Department of Education", "Education Department"],
    ),
    (
        "Commerce",
        ["Department of Commerce", "Commerce Department", "NOAA"],
    ),
    (
        "Treasury",
        ["Department of the Treasury", "Treasury Department", "Treasury"],
    ),
    ("State", ["Department of State", "State Department"]),
    (
        "USDA",
        ["USDA", "Department of Agriculture", "Agriculture Department"],
    ),
    ("EPA", ["EPA", "Environmental Protection Agency"]),
    (
        "VA",
        ["Department of Veterans Affairs", "Veterans Affairs", "Veterans"],
    ),
    ("HUD", ["HUD", "Housing and Urban Development"]),
    (
        "Labor",
        ["Department of Labor", "Labor Department", "DOL"],
    ),
    (
        "Interior",
        ["Department of the Interior", "Interior Department"],
    ),
    ("OPM", ["OPM", "Office of Personnel Management", "Personnel"]),
]


_DEPARTMENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        canonical,
        re.compile(
            r"\b(?:" + "|".join(re.escape(a) for a in aliases) + r")\b",
            re.IGNORECASE,
        ),
    )
    for canonical, aliases in _DEPARTMENT_ALIASES
]


def _compute_department_coverage(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scan finding ``claim`` text for federal department mentions.

    Returns a list of ``{"department": <canonical>, "count": <n>}`` ranked
    high→low by count (canonical name as a stable tiebreaker). Each finding
    contributes at most one increment per department even if multiple
    aliases for that department appear in its claim text. The result feeds
    the synthesizer prompt as a structural hint so it can enumerate the
    Departmental Policy Tracker by data rather than by template.
    """
    counts: dict[str, int] = {}
    for f in findings:
        claim = f.get("claim", "")
        if not isinstance(claim, str) or not claim:
            continue
        seen_in_finding: set[str] = set()
        for canonical, pattern in _DEPARTMENT_PATTERNS:
            if canonical in seen_in_finding:
                continue
            if pattern.search(claim):
                seen_in_finding.add(canonical)
        for c in seen_in_finding:
            counts[c] = counts.get(c, 0) + 1

    return sorted(
        [{"department": d, "count": c} for d, c in counts.items()],
        key=lambda item: (-int(item["count"]), str(item["department"])),
    )


def _load_jsonl_events(job: Job) -> list[dict[str, Any]]:
    path = job.root / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _latest_inconclusive_subgoal_ids(job: Job) -> set[int]:
    inconclusive: set[int] = set()
    for event in _load_jsonl_events(job):
        if event.get("kind") != "plan_subgoals_updated":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        raw_ids = payload.get("inconclusive")
        if not isinstance(raw_ids, list):
            continue
        next_ids: set[int] = set()
        for raw_id in raw_ids:
            if isinstance(raw_id, bool):
                continue
            if isinstance(raw_id, int):
                next_ids.add(raw_id)
            elif isinstance(raw_id, str) and raw_id.strip().isdigit():
                next_ids.add(int(raw_id.strip()))
        inconclusive = next_ids
    return inconclusive


def _load_low_yield_records(job: Job) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()

    def _add(item: dict[str, Any]) -> None:
        kind = item.get("kind")
        stem = item.get("query_stem")
        count = item.get("count")
        if not isinstance(kind, str) or not isinstance(stem, str):
            return
        try:
            count_i = int(count)
        except (TypeError, ValueError):
            count_i = 0
        if count_i < 3:
            return
        key = (kind, stem, count_i)
        if key in seen:
            return
        seen.add(key)
        records.append(dict(item, count=count_i))

    for event in _load_jsonl_events(job):
        if event.get("kind") != "low_yield_connector":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            _add(payload)

    path = job.root / "synthesis" / "low_yield.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    _add(item)
    return records


def _load_failed_task_rows(job: Job) -> list[dict[str, Any]]:
    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, kind, payload_json, status, result_json, error
            FROM tasks
            WHERE job_id = ? AND status = 'failed'
            ORDER BY id ASC
            """,
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _raw_query_from_payload(payload: dict[str, Any]) -> str:
    for key in ("query", "q", "sub_question", "url", "source_id", "arxiv_id"):
        value = payload.get(key)
        if value not in (None, "", []):
            return str(value)
    return ""


def _query_stem_for_payload(payload: dict[str, Any]) -> str:
    from research_agent.orchestrator.plan import _attempt_query_stem

    return _attempt_query_stem(payload)


def _source_label_from_query(query: str) -> str | None:
    city_match = re.search(r"\bcity\s+of\s+([a-z][a-z\s.'-]{2,60})", query, re.IGNORECASE)
    if city_match:
        name = " ".join(city_match.group(1).split())
        return f"City of {name.title()}"
    site_match = re.search(r"\bsite:([a-z0-9.-]+)", query, re.IGNORECASE)
    host = site_match.group(1).lower() if site_match else ""
    if host == "alamedaca.gov":
        return "City of Alameda"
    if host.endswith(".gov"):
        label = host.split(".")[0]
        label = re.sub(r"(ca|ny|tx|fl|wa|or|il|ma|pa|oh|mi|ga|nc|nj|va)$", "", label)
        label = label.replace("-", " ").strip()
        if label:
            return label.title()
    return None


def _fallback_unblocker(kind: str, query: str, failure_reason: str | None) -> str:
    lower = f"{kind} {query} {failure_reason or ''}".lower()
    source_label = _source_label_from_query(query)
    if source_label:
        return f"FOIA the {source_label} Clerk or records custodian for records matching '{query}'"
    if "courtlistener" in lower or "403" in lower:
        return (
            "Use a CourtListener API token from https://www.courtlistener.com/api/ "
            f"or PACER/RECAP for '{query or kind}'"
        )
    if "calaccess" in lower or "form 460" in lower:
        return (
            "Request Form 460 records from the named city clerk or county elections "
            f"office for '{query}'"
        )
    if "fec" in lower:
        return f"Check FEC.gov candidate and committee filings directly for '{query}'"
    if "edgar" in lower:
        return f"Check SEC EDGAR directly or state SoS records for '{query}'"
    if "licensing" in lower:
        return f"Search the state licensing-board portal for '{query}'"
    if "sos" in lower or "opencorporates" in lower:
        return f"Search the state Secretary of State business registry for '{query}'"
    if query:
        return f"Ask the named records custodian or source owner for records matching '{query}'"
    return f"Use the source owner or records custodian for {kind}"


def _specific_unblocker(
    *,
    kind: str,
    query: str,
    failure_reason: str | None = None,
    suggested: str | None = None,
    gap_reason: str | None = None,
) -> str:
    for candidate in (suggested, gap_reason):
        if isinstance(candidate, str) and candidate.strip():
            text = candidate.strip().rstrip(".")
            source_label = _source_label_from_query(query)
            lower = text.lower()
            if source_label and ("city clerk" in lower or "records request" in lower):
                return f"FOIA the {source_label} Clerk for records matching '{query}'"
            if query and query.lower() not in lower and len(text) < 180:
                return f"{text} for '{query}'"
            return text
    return _fallback_unblocker(kind, query, failure_reason)


def _topic_for_stem(stem: str, query: str) -> str:
    text = query or stem
    text = re.sub(r"\bsite:[^\s]+", "", text, flags=re.IGNORECASE).strip()
    return text or "unresolved source gap"


def _compute_confirmed_gaps(job: Job, plan: Plan) -> list[dict[str, Any]]:
    """Aggregate failed/low-yield work into report-ready confirmed gaps."""
    from research_agent.orchestrator.plan import _failure_reason_for_attempt, _task_matches_subgoal

    subgoals_by_id = {sg.id: sg for sg in plan.subgoals}
    inconclusive_ids = _latest_inconclusive_subgoal_ids(job)
    low_yield_records = _load_low_yield_records(job)
    low_yield_by_key: dict[tuple[str, str], dict[str, Any]] = {
        (str(r.get("kind") or ""), str(r.get("query_stem") or "")): r
        for r in low_yield_records
    }
    failed_rows = _load_failed_task_rows(job)

    gaps: dict[str, dict[str, Any]] = {}
    attempt_seen: set[tuple[str, str, str, str]] = set()
    consumed_low_yield_keys: set[tuple[str, str]] = set()

    def _ensure_gap(
        topic: str,
        *,
        gap_reason: str | None = None,
        suggested_unblocker: str | None = None,
    ) -> dict[str, Any]:
        normalized_topic = " ".join(topic.split()).strip() or "unresolved source gap"
        gap = gaps.get(normalized_topic)
        if gap is None:
            gap = {
                "topic": normalized_topic,
                "attempts": [],
                "failure_summary": "",
                "suggested_unblocker": suggested_unblocker or "",
                "_gap_reason": gap_reason,
                "_reasons": {},
            }
            gaps[normalized_topic] = gap
        else:
            if gap_reason and not gap.get("_gap_reason"):
                gap["_gap_reason"] = gap_reason
            if suggested_unblocker and not gap.get("suggested_unblocker"):
                gap["suggested_unblocker"] = suggested_unblocker
        return gap

    def _add_attempt(
        gap: dict[str, Any],
        *,
        kind: str,
        query: str,
        failure_reason: str,
        count: int,
        suggested_unblocker: str | None = None,
    ) -> None:
        key = (str(gap["topic"]), kind, query, failure_reason)
        if key in attempt_seen:
            for attempt in gap["attempts"]:
                if (
                    attempt["task_kind"] == kind
                    and attempt["query"] == query
                    and attempt["failure_reason"] == failure_reason
                ):
                    current = int(attempt["count"])
                    incoming = int(count)
                    updated = max(current, incoming) if incoming > 1 else current + 1
                    attempt["count"] = updated
                    reasons = gap["_reasons"]
                    reasons[failure_reason] = max(
                        int(reasons.get(failure_reason, 0)),
                        updated,
                    )
                    break
            return
        attempt_seen.add(key)
        gap["attempts"].append(
            {
                "task_kind": kind,
                "query": query,
                "failure_reason": failure_reason,
                "count": int(count),
            }
        )
        reasons = gap["_reasons"]
        reasons[failure_reason] = int(reasons.get(failure_reason, 0)) + int(count)
        if suggested_unblocker:
            gap["suggested_unblocker"] = suggested_unblocker

    from research_agent.storage import coverage

    for unit in coverage.list_units(job, {"confirmed_gap"}):
        topic = unit.dim_key
        unblocker = (
            unit.unblocker
            or "Use a non-public records request or wait for the source owner "
            "to publish this coverage unit"
        )
        gap = _ensure_gap(
            topic,
            gap_reason="coverage unit confirmed gap",
            suggested_unblocker=unblocker,
        )
        attempts = unit.recent_attempts or []
        if not attempts:
            _add_attempt(
                gap,
                kind="coverage_ledger",
                query=topic,
                failure_reason="coverage unit marked confirmed_gap",
                count=1,
                suggested_unblocker=unblocker,
            )
        for attempt in attempts:
            _add_attempt(
                gap,
                kind=attempt.task_kind or "coverage_ledger",
                query=topic,
                failure_reason=attempt.reason or "coverage attempt did not complete unit",
                count=1,
                suggested_unblocker=unblocker,
            )

    subgoal_payload_attempts: dict[int, list[tuple[dict[str, Any], dict[str, Any], str]]] = {
        sid: [] for sid in subgoals_by_id
    }
    unmatched_failed: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for row in failed_rows:
        payload = _json_dict(row.get("payload_json"))
        result = _json_dict(row.get("result_json"))
        reason = _failure_reason_for_attempt(row, result) or str(row.get("error") or "failed")
        matched_any = False
        for sid, sg in subgoals_by_id.items():
            if _task_matches_subgoal(sg, payload):
                subgoal_payload_attempts.setdefault(sid, []).append((row, payload, reason))
                matched_any = True
        if not matched_any:
            unmatched_failed.append((row, payload, reason))

    for sid, sg in subgoals_by_id.items():
        gap_reason = sg.gap_reason if isinstance(sg.gap_reason, str) else None
        if sid not in inconclusive_ids and not gap_reason:
            continue
        representative_query = ""
        attempts = subgoal_payload_attempts.get(sid) or []
        if attempts:
            representative_query = _raw_query_from_payload(attempts[0][1])
        unblocker = _specific_unblocker(
            kind="subgoal",
            query=representative_query or sg.description,
            gap_reason=gap_reason,
        )
        gap = _ensure_gap(
            sg.description,
            gap_reason=gap_reason,
            suggested_unblocker=unblocker,
        )
        for row, payload, reason in attempts:
            kind = str(row.get("kind") or "task")
            query = _raw_query_from_payload(payload) or _query_stem_for_payload(payload)
            stem = _query_stem_for_payload(payload)
            low_yield_key = (kind, stem)
            suggested = low_yield_by_key.get(low_yield_key, {}).get("suggested_unblocker")
            attempt_unblocker = (
                _specific_unblocker(
                    kind=kind,
                    query=query,
                    failure_reason=reason,
                    suggested=suggested,
                )
                if isinstance(suggested, str) and suggested.strip()
                else None
            )
            _add_attempt(
                gap,
                kind=kind,
                query=query,
                failure_reason=reason,
                count=1,
                suggested_unblocker=attempt_unblocker,
            )
            if low_yield_key in low_yield_by_key:
                consumed_low_yield_keys.add(low_yield_key)

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row, payload, reason in unmatched_failed:
        kind = str(row.get("kind") or "task")
        stem = _query_stem_for_payload(payload)
        query = _raw_query_from_payload(payload) or stem
        key = (kind, stem, reason)
        group = grouped.setdefault(
            key,
            {
                "kind": kind,
                "stem": stem,
                "query": query,
                "reason": reason,
                "count": 0,
            },
        )
        group["count"] = int(group["count"]) + 1

    for group in grouped.values():
        kind = str(group["kind"])
        stem = str(group["stem"])
        query = str(group["query"])
        reason = str(group["reason"])
        low_yield = low_yield_by_key.get((kind, stem), {})
        suggested = low_yield.get("suggested_unblocker")
        unblocker = _specific_unblocker(
            kind=kind,
            query=query,
            failure_reason=reason,
            suggested=suggested if isinstance(suggested, str) else None,
        )
        gap = _ensure_gap(_topic_for_stem(stem, query), suggested_unblocker=unblocker)
        _add_attempt(
            gap,
            kind=kind,
            query=query,
            failure_reason=reason,
            count=int(group["count"]),
            suggested_unblocker=unblocker,
        )
        if (kind, stem) in low_yield_by_key:
            consumed_low_yield_keys.add((kind, stem))

    for record in low_yield_records:
        kind = str(record.get("kind") or "search")
        stem = str(record.get("query_stem") or "")
        if (kind, stem) in consumed_low_yield_keys:
            continue
        query = stem
        reason = f"0 results from {kind}"
        suggested = record.get("suggested_unblocker")
        unblocker = _specific_unblocker(
            kind=kind,
            query=query,
            failure_reason=reason,
            suggested=suggested if isinstance(suggested, str) else None,
        )
        gap = _ensure_gap(_topic_for_stem(stem, query), suggested_unblocker=unblocker)
        _add_attempt(
            gap,
            kind=kind,
            query=query,
            failure_reason=reason,
            count=int(record.get("count") or 3),
            suggested_unblocker=unblocker,
        )

    out: list[dict[str, Any]] = []
    for gap in gaps.values():
        attempts = list(gap["attempts"])
        if not attempts and not gap.get("_gap_reason"):
            continue
        reasons = gap.get("_reasons") if isinstance(gap.get("_reasons"), dict) else {}
        if gap.get("_gap_reason"):
            summary = f"Could not resolve {gap['topic']}: {gap['_gap_reason']}."
        elif reasons:
            first_reason = max(reasons.items(), key=lambda item: int(item[1]))[0]
            kinds = sorted({a["task_kind"] for a in attempts})
            summary = (
                f"Tried {', '.join(kinds)} for {gap['topic']}; "
                f"the strongest failure signal was {first_reason}."
            )
        else:
            summary = f"Could not resolve {gap['topic']} from available public sources."
        unblocker = str(gap.get("suggested_unblocker") or "").strip()
        if not unblocker:
            first = attempts[0] if attempts else {}
            unblocker = _fallback_unblocker(
                str(first.get("task_kind") or "source"),
                str(first.get("query") or gap["topic"]),
                str(first.get("failure_reason") or ""),
            )
        out.append(
            {
                "topic": gap["topic"],
                "attempts": attempts,
                "failure_summary": summary,
                "suggested_unblocker": unblocker,
            }
        )
    return sorted(out, key=lambda item: str(item["topic"]).lower())


def _load_current_hypotheses(job: Job, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from research_agent.storage import hypotheses

    rows = hypotheses.list_hypotheses(job)
    if not rows:
        return []
    findings_by_id = {
        int(f["id"]): f
        for f in findings
        if isinstance(f.get("id"), int)
    }
    out: list[dict[str, Any]] = []
    for row in rows:
        item = {
            "id": row["id"],
            "statement": row["statement"],
            "confidence": row["confidence"],
            "supports": row["supports"],
            "refutes": row["refutes"],
            "status": row["status"],
            "plan_version": row["plan_version"],
            "supporting_findings": [
                findings_by_id[fid]
                for fid in row["supports"]
                if isinstance(fid, int) and fid in findings_by_id
            ],
            "refuting_findings": [
                findings_by_id[fid]
                for fid in row["refutes"]
                if isinstance(fid, int) and fid in findings_by_id
            ],
        }
        out.append(item)
    return out


def _load_artifacts_for_context(job: Job) -> list[dict[str, Any]]:
    from research_agent.storage import artifacts

    return artifacts.list_artifacts(job)


def _load_coverage_for_context(job: Job) -> list[dict[str, Any]]:
    from research_agent.storage import coverage

    return [unit.model_dump(mode="json") for unit in coverage.list_units(job)]


def _build_context(
    *,
    goal: str,
    plan: Plan,
    findings: list[dict[str, Any]],
    sources: dict[int, dict[str, Any]],
    prior: str | None,
    critique: str | None,
    followup_recipes: str,
    paid_unblock_recipes: str,
    confirmed_gaps: list[dict[str, Any]] | None = None,
    current_hypotheses: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    final: bool = False,
) -> str:
    payload: dict[str, Any] = {
        "goal": goal,
        "scope_class": str(plan.scope_class) if plan.scope_class else None,
        "subgoals": [
            {
                "id": sg.id,
                "description": sg.description,
                "done": sg.done,
                "gap_reason": sg.gap_reason,
                "gap_status": sg.gap_status,
            }
            for sg in plan.subgoals
        ],
        "findings": findings,
        "sources": {str(k): v for k, v in sources.items()},
        "prior_synthesis": prior,
        "critique": critique,
        "followup_recipes": followup_recipes,
        "paid_unblock_recipes": paid_unblock_recipes,
        "department_coverage": _compute_department_coverage(findings),
        "confirmed_gaps": confirmed_gaps or [],
        "current_hypotheses": current_hypotheses or [],
        "artifacts": artifacts or [],
    }
    if final:
        payload["final"] = True
    return json.dumps(payload, sort_keys=True, default=str)


_SUBGOAL_STATUS_FENCE_RE = re.compile(r"```[ \t]*json[ \t]*\n(.*?)\n```\s*$", re.DOTALL)
_ANY_JSON_FENCE_RE = re.compile(r"```[ \t]*json[ \t]*\n(.*?)\n```", re.DOTALL)
_LEVEL_TWO_HEADING_RE = re.compile(r"^##[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)
_TRAILING_STATUS_KEY_RE = re.compile(
    r'"(?:subgoal_status|closed|reopened|inconclusive)"',
    re.IGNORECASE,
)
_HYPOTHESIS_UPDATES_KEY_RE = re.compile(r'"hypothesis_updates"', re.IGNORECASE)
_PROSE_SUBGOAL_STATUS_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:[-*]\s*)?(?:\*\*)?"
    r"(?:(?:H)|(?:Subgoal))\s*#?(?P<id>\d+)(?:\*\*)?\s*[:\-]\s*"
    r"(?:\*\*)?(?P<status>confirmed|refuted|inconclusive)\b"
)
_STATUS_ALIASES = {
    "confirmed": "confirmed",
    "confirm": "confirmed",
    "closed": "confirmed",
    "refuted": "refuted",
    "refute": "refuted",
    "inconclusive": "inconclusive",
    "open": "inconclusive",
    "reopened": "inconclusive",
}


@dataclass(frozen=True)
class _StatusCandidate:
    body: str
    stripped_md: str
    source: str


def _stripped_with_trailer_removed(raw: str, start: int) -> str:
    prefix = raw[:start].rstrip()
    return prefix + ("\n" if prefix else "")


def _coerce_subgoal_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(?:(?:H)|(?:Subgoal))?\s*#?(\d+)\s*", value)
        if match:
            return int(match.group(1))
    return None


def _normalize_status(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return _STATUS_ALIASES.get(value.strip().lower())


def _normalize_subgoal_status_payload(data: Any) -> dict[int, str] | None:
    if not isinstance(data, dict):
        return None

    status_map: dict[int, str] = {}
    subgoal_status = data.get("subgoal_status")
    if isinstance(subgoal_status, dict):
        for raw_id, raw_status in subgoal_status.items():
            sid = _coerce_subgoal_id(raw_id)
            status = _normalize_status(raw_status)
            if sid is not None and status is not None:
                status_map[sid] = status

    for key, status in (
        ("closed", "confirmed"),
        ("reopened", "inconclusive"),
        ("inconclusive", "inconclusive"),
    ):
        raw_ids = data.get(key)
        if not isinstance(raw_ids, list):
            continue
        for raw_id in raw_ids:
            sid = _coerce_subgoal_id(raw_id)
            if sid is not None:
                status_map[sid] = status

    return status_map or None


def _json_object_from_section_body(body: str) -> str | None:
    matches = list(_ANY_JSON_FENCE_RE.finditer(body))
    if matches:
        return matches[-1].group(1).strip()

    start = body.find("{")
    end = body.rfind("}")
    if start == -1 or end <= start:
        return None
    return body[start : end + 1].strip()


def _candidate_from_final_subgoal_status_section(raw: str) -> _StatusCandidate | None:
    headings = list(_LEVEL_TWO_HEADING_RE.finditer(raw))
    if not headings:
        return None

    last_heading = headings[-1]
    title = last_heading.group("title").strip().strip("#").strip().lower()
    if title != "subgoal status":
        return None

    section_body = raw[last_heading.end() :]
    json_body = _json_object_from_section_body(section_body)
    if json_body is None:
        return None

    return _StatusCandidate(
        body=json_body,
        stripped_md=_stripped_with_trailer_removed(raw, last_heading.start()),
        source="subgoal_status_section",
    )


def _candidate_from_subgoal_status_json_fence(raw: str) -> _StatusCandidate | None:
    """Pick the first ```json fence whose body carries subgoal_status (issue #389).

    Whole-report synthesis puts hypothesis_updates in the *last* fence, so the
    legacy trailing-fence matcher alone would read the wrong block.
    """
    for match in _ANY_JSON_FENCE_RE.finditer(raw):
        body = match.group(1).strip()
        if not _TRAILING_STATUS_KEY_RE.search(body):
            continue
        return _StatusCandidate(
            body=body,
            stripped_md=_stripped_with_trailer_removed(raw, match.start()),
            source="subgoal_status_json_fence",
        )
    return None


def _candidate_from_trailing_fence(raw: str) -> _StatusCandidate | None:
    match = _SUBGOAL_STATUS_FENCE_RE.search(raw)
    if match is None:
        return None
    return _StatusCandidate(
        body=match.group(1).strip(),
        stripped_md=_stripped_with_trailer_removed(raw, match.start()),
        source="trailing_json_fence",
    )


def _candidate_from_raw_trailing_json(raw: str) -> _StatusCandidate | None:
    stripped = raw.rstrip()
    if not stripped.endswith("}"):
        return None

    decoder = json.JSONDecoder()
    for match in reversed(list(re.finditer(r"\{", stripped))):
        start = match.start()
        suffix = stripped[start:]
        if not _TRAILING_STATUS_KEY_RE.search(suffix):
            continue
        try:
            _data, end = decoder.raw_decode(suffix)
        except json.JSONDecodeError:
            continue
        if suffix[end:].strip():
            continue
        return _StatusCandidate(
            body=suffix.strip(),
            stripped_md=_stripped_with_trailer_removed(raw, start),
            source="raw_trailing_json",
        )
    return None


def _find_structured_status_candidate(raw: str) -> _StatusCandidate | None:
    return (
        _candidate_from_final_subgoal_status_section(raw)
        or _candidate_from_subgoal_status_json_fence(raw)
        or _candidate_from_trailing_fence(raw)
        or _candidate_from_raw_trailing_json(raw)
    )


def _parse_status_candidate(job: Job, candidate: _StatusCandidate) -> dict[int, str] | None:
    try:
        data = json.loads(candidate.body)
    except json.JSONDecodeError as exc:
        emit(
            job,
            "WARN",
            "synth",
            "warning",
            {
                "stage": "subgoal_status",
                "source": candidate.source,
                "error": f"json parse failed: {exc}",
            },
        )
        return None

    status_map = _normalize_subgoal_status_payload(data)
    if status_map is None:
        emit(
            job,
            "WARN",
            "synth",
            "warning",
            {
                "stage": "subgoal_status",
                "source": candidate.source,
                "error": "missing recognized subgoal status payload",
            },
        )
    return status_map


def _status_event_payload(status_map: dict[int, str]) -> dict[str, Any]:
    return {
        "status": {str(k): v for k, v in sorted(status_map.items())},
        "closed": sorted(k for k, v in status_map.items() if v in {"confirmed", "refuted"}),
        "inconclusive": sorted(k for k, v in status_map.items() if v == "inconclusive"),
    }


def _extract_prose_subgoal_status(raw: str, subgoal_ids: set[int]) -> dict[int, str] | None:
    status_map: dict[int, str] = {}
    for match in _PROSE_SUBGOAL_STATUS_RE.finditer(raw):
        sid = _coerce_subgoal_id(match.group("id"))
        status = _normalize_status(match.group("status"))
        if sid is None or status is None:
            continue
        if subgoal_ids and sid not in subgoal_ids:
            continue
        status_map[sid] = status
    return status_map or None


def _extract_subgoal_status(
    job: Job,
    raw: str,
    *,
    subgoal_ids: list[int] | None = None,
) -> tuple[str, dict[int, str] | None]:
    """Split structured subgoal status from the markdown report.

    Returns ``(stripped_md, status_map)``. ``status_map`` is ``None`` when
    no structured payload or status-like prose can be read. Structured
    trailers are stripped before persistence even when malformed.
    """
    subgoal_id_set = set(subgoal_ids or [])
    candidate = _find_structured_status_candidate(raw)
    stripped_md = raw
    if candidate is not None:
        stripped_md = candidate.stripped_md
        status_map = _parse_status_candidate(job, candidate)
        if status_map:
            return stripped_md, status_map

    prose_status = _extract_prose_subgoal_status(stripped_md, subgoal_id_set)
    if prose_status:
        emit(
            job,
            "INFO",
            "synth",
            "synth_status_from_prose",
            _status_event_payload(prose_status),
        )
        return stripped_md, prose_status

    emit(
        job,
        "WARN",
        "synth",
        "synth_status_missing",
        {
            "structured_candidate": candidate.source if candidate is not None else None,
            "subgoal_ids": sorted(subgoal_id_set),
        },
    )
    return stripped_md, None


def _strip_range(raw: str, start: int, end: int) -> str:
    prefix = raw[:start].rstrip()
    suffix = raw[end:].lstrip()
    if prefix and suffix:
        return prefix + "\n\n" + suffix
    if prefix:
        return prefix + "\n"
    return suffix


def _coerce_finding_id_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            out.append(item)
        elif isinstance(item, str) and item.strip().isdigit():
            out.append(int(item.strip()))
    return out


def _normalize_hypothesis_updates_payload(data: Any) -> list[dict[str, Any]] | None:
    if not isinstance(data, dict):
        return None
    raw_updates = data.get("hypothesis_updates")
    if not isinstance(raw_updates, list):
        return None
    updates: list[dict[str, Any]] = []
    for raw in raw_updates:
        if not isinstance(raw, dict):
            continue
        statement = raw.get("statement")
        status = raw.get("status")
        confidence_raw = raw.get("confidence")
        if not isinstance(statement, str) or not statement.strip():
            continue
        if status not in {"open", "confirmed", "refuted", "inconclusive"}:
            continue
        if not isinstance(confidence_raw, (int, float)) or isinstance(confidence_raw, bool):
            continue
        confidence = max(0.0, min(1.0, float(confidence_raw)))
        item: dict[str, Any] = {
            "statement": statement.strip(),
            "confidence": confidence,
            "supports": _coerce_finding_id_list(raw.get("supports")),
            "refutes": _coerce_finding_id_list(raw.get("refutes")),
            "status": status,
        }
        raw_id = raw.get("id")
        if isinstance(raw_id, int) and not isinstance(raw_id, bool):
            item["id"] = raw_id
        elif isinstance(raw_id, str) and raw_id.strip().isdigit():
            item["id"] = int(raw_id.strip())
        updates.append(item)
    return updates


def _extract_hypothesis_updates(
    job: Job,
    raw: str,
) -> tuple[str, list[dict[str, Any]] | None]:
    """Split a ``hypothesis_updates`` JSON fence from the markdown report."""
    for match in reversed(list(_ANY_JSON_FENCE_RE.finditer(raw))):
        body = match.group(1).strip()
        if not _HYPOTHESIS_UPDATES_KEY_RE.search(body):
            continue
        stripped_md = _strip_range(raw, match.start(), match.end())
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            emit(
                job,
                "WARN",
                "synth",
                "warning",
                {
                    "stage": "hypothesis_updates",
                    "error": f"json parse failed: {exc}",
                },
            )
            return stripped_md, None
        updates = _normalize_hypothesis_updates_payload(data)
        if updates is None:
            emit(
                job,
                "WARN",
                "synth",
                "warning",
                {
                    "stage": "hypothesis_updates",
                    "error": "missing recognized hypothesis_updates payload",
                },
            )
            return stripped_md, None
        return stripped_md, updates
    return raw, None


def _apply_hypothesis_updates(
    job: Job,
    plan: Plan,
    updates: list[dict[str, Any]],
) -> list[int]:
    from research_agent.storage import hypotheses

    updated_ids: list[int] = []
    for update in updates:
        hid = hypotheses.upsert_hypothesis(
            job,
            id=update.get("id"),
            plan_version=plan.version,
            statement=update["statement"],
            confidence=float(update["confidence"]),
            supports=update.get("supports") or [],
            refutes=update.get("refutes") or [],
            status=update["status"],
        )
        updated_ids.append(hid)
        emit(
            job,
            "INFO",
            "synth",
            "hypothesis_updated",
            {
                "id": hid,
                "plan_version": plan.version,
                "status": update["status"],
                "confidence": float(update["confidence"]),
            },
        )
    return updated_ids


def _apply_subgoal_status(
    job: Job,
    plan: Plan,  # noqa: ARG001 — kept for clarity at call sites; helper reloads from DB
    status_map: dict[int, str],
) -> None:
    """Persist a synthesizer-emitted ``subgoal_status`` map onto the latest plan."""
    from research_agent.orchestrator import plan as _plan_mod

    _plan_mod.update_subgoal_done(job, status_map)


_SOURCES_HEADING_RE = re.compile(r"^##\s+Sources\s*$", re.MULTILINE)


def _reconcile_sources(
    job: Job,
    md: str,
    sources_by_id: dict[int, dict[str, Any]],
) -> str:
    """Append inline-cited source IDs that the model dropped from ``## Sources``.

    Issue #207: synthesizer often emits a curated Sources list that is a
    strict subset of the IDs cited inline. Parse every ``[N]`` (and grouped
    ``[N, M]``) citation in the body, parse the IDs the model enumerated
    under the trailing ``## Sources`` heading, and append any missing ones
    using the canonical row shape so a reader can resolve every cited ID.

    Emits a ``source_list_reconciled`` INFO event whenever any inline-cited
    ID was missing from the enumerated section, recording both the IDs we
    appended (``added``) and any IDs we couldn't resolve against
    ``sources_by_id`` (``unresolved``).
    """
    reconciled, metadata = reconcile_report_sources(md, sources_by_id)
    if not metadata["added"] and not metadata["unresolved"]:
        return md

    emit(job, "INFO", "synth", "source_list_reconciled", metadata)
    return reconciled


async def _run_synth(
    job: Job,
    router: Router,
    tier: str,
    context: str,
) -> str:
    rendered = load_prompt("synthesizer", job=job, goal=job.goal)
    agent = Agent(router.model_for(tier), output_type=str, system_prompt=rendered)
    result = await router.call(tier, agent, context)
    output = result.output
    if not isinstance(output, str):
        output = str(output)
    return output


def _model_name_for(router: Router, tier: str) -> str:
    """Pull the configured model name for ``tier`` from router config."""
    spec = router.tiers.get(tier, {})
    name = spec.get("model")
    if not isinstance(name, str) or not name:
        return tier
    return name


def _provider_name_for(router: Router, tier: str) -> str:
    spec = router.tiers.get(tier, {})
    provider = spec.get("provider")
    if not isinstance(provider, str) or not provider:
        return "unknown"
    return provider


_PROVIDER_FORMAT_HINTS = (
    "http 400",
    "400 bad request",
    "bad request",
    "invalid_request",
    "invalid request",
    "reasoning",
    "channel",
    "openai-compatible",
    "provider",
)


def _safe_exception_snippet(exc: BaseException, *, max_chars: int = 500) -> str:
    text = str(exc).strip() or repr(exc)
    text = re.sub(r"\s+", " ", text)
    for marker in (" system_prompt=", " prompt=", " context=", " messages="):
        idx = text.lower().find(marker.strip().lower())
        if idx > 0:
            text = text[:idx].rstrip()
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def _is_provider_format_specific(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(hint in text for hint in _PROVIDER_FORMAT_HINTS)


def _emit_synthesis_llm_failed(
    job: Job,
    router: Router,
    *,
    tier: str,
    exc: BaseException,
    attempt_count: int,
    post_cap: bool = False,
) -> None:
    emit(
        job,
        "WARN",
        "synth",
        "synthesis_llm_failed",
        {
            "tier": tier,
            "model": _model_name_for(router, tier),
            "provider": _provider_name_for(router, tier),
            "attempt_count": attempt_count,
            "error_type": type(exc).__name__,
            "diagnostic_snippet": _safe_exception_snippet(exc),
            "provider_format_specific": _is_provider_format_specific(exc),
            "post_cap": post_cap,
        },
    )


def _emit_fallback_tier_used(
    job: Job,
    router: Router,
    *,
    primary_tier: str,
    fallback_tier: str,
    reason: str,
) -> None:
    emit(
        job,
        "INFO",
        "synth",
        "synthesis_fallback_tier_used",
        {
            "primary_tier": primary_tier,
            "fallback_tier": fallback_tier,
            "fallback_model": _model_name_for(router, fallback_tier),
            "fallback_provider": _provider_name_for(router, fallback_tier),
            "reason": reason,
        },
    )


def _env_flag_enabled(name: str) -> bool:
    value = config.get(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def fragment_synth_enabled() -> bool:
    """Return True when section-fragment synthesis is explicitly enabled."""

    return _env_flag_enabled("RESEARCH_FRAGMENT_SYNTH")


def _emit_synthesis_mode(job: Job, *, mode: str, entrypoint: str, final: bool) -> None:
    emit(
        job,
        "INFO",
        "synth",
        "synthesis_mode",
        {
            "mode": mode,
            "entrypoint": entrypoint,
            "final": final,
        },
    )


def _load_fragment_findings(job: Job) -> list[dict[str, Any]]:
    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, claim, confidence, source_ids, tags, target_fragments
            FROM findings
            WHERE job_id = ?
            ORDER BY id ASC
            """,
            (job.id,),
        ).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        finding_id = int(row["id"])
        translated_claim = _load_finding_translation(job, finding_id)
        item = {
            "id": finding_id,
            "claim": translated_claim or row["claim"],
            "confidence": float(row["confidence"]),
            "source_ids": json.loads(row["source_ids"]) if row["source_ids"] else [],
            "tags": json.loads(row["tags"]) if row["tags"] else [],
            "target_fragments": (
                json.loads(row["target_fragments"]) if row["target_fragments"] else []
            ),
        }
        if translated_claim is not None:
            item["original_claim"] = row["claim"]
            item["translated"] = True
        out.append(item)
    return out


def _select_stale_fragments(job: Job, plan: Plan) -> tuple[str, ...]:
    """Select dependency-closed stale fragments from persisted finding tags.

    A directly-tagged section is stale only when its current set of tagged
    finding IDs differs from the ``source_finding_ids`` recorded on its
    latest persisted fragment (or it has no fragment yet). This keeps
    synthesis cost scaling with *changed* sections instead of re-running
    every tagged section on every pass. Dependency closures of genuinely
    stale sections are still pulled so dependent context stays consistent.
    """

    from research_agent.orchestrator.fragments import (
        dependency_closure,
        fragment_ids,
        synthesis_order,
    )

    _ = plan
    valid = fragment_ids()
    tagged: dict[str, set[int]] = {}
    for finding in _load_fragment_findings(job):
        finding_id = int(finding["id"])
        for target in finding.get("target_fragments") or []:
            if target not in valid:
                continue
            tagged.setdefault(target, set()).add(finding_id)

    stale: set[str] = set()
    for section_id, finding_ids in tagged.items():
        prior = latest_fragment(job, section_id)
        if prior is not None:
            prior_ids = {int(i) for i in prior.get("source_finding_ids") or []}
            if finding_ids == prior_ids:
                continue
        stale.add(section_id)
        stale.update(dependency_closure(section_id))
    return synthesis_order(stale)


def _latest_synthesis_version(job: Job) -> int | None:
    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            "SELECT MAX(version) AS version FROM syntheses WHERE job_id = ?",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row["version"] is None:
        return None
    return int(row["version"])


def _build_fragment_context(
    job: Job,
    section_id: str,
    plan: Plan,
    *,
    findings: list[dict[str, Any]] | None = None,
    final: bool = False,
) -> tuple[str, list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """Build a section-bounded JSON context for one fragment synthesis call."""

    from research_agent.orchestrator.fragments import dependency_closure, get_fragment

    fragment = get_fragment(section_id)
    all_findings = findings if findings is not None else _load_fragment_findings(job)
    relevant_findings = [
        finding
        for finding in all_findings
        if section_id in set(finding.get("target_fragments") or [])
    ]
    sources = _load_sources_for(job, relevant_findings)
    prior = latest_fragment(job, section_id)

    dependency_fragments: dict[str, dict[str, Any]] = {}
    for dependency_id in dependency_closure(section_id):
        dep = latest_fragment(job, dependency_id)
        if dep is None:
            continue
        dependency_fragments[dependency_id] = {
            "section_id": dependency_id,
            "version": dep["version"],
            "content": dep["content"],
            "created_at": dep["created_at"],
        }

    payload = {
        "goal": job.goal,
        "final": final,
        "section": {
            "id": fragment.id,
            "title": fragment.title,
            "prompt_hint": fragment.prompt_hint,
            "resource_hint": fragment.resource_hint,
        },
        "plan": {
            "version": plan.version,
            "objective": plan.objective,
            "scope_class": str(plan.scope_class) if plan.scope_class else None,
            "subgoals": [
                {
                    "id": sg.id,
                    "description": sg.description,
                    "done": sg.done,
                    "gap_reason": sg.gap_reason,
                    "gap_status": sg.gap_status,
                }
                for sg in plan.subgoals
            ],
        },
        "prior_fragment": (
            {
                "version": prior["version"],
                "content": prior["content"],
                "created_at": prior["created_at"],
            }
            if prior is not None
            else None
        ),
        "dependency_fragments": dependency_fragments,
        "findings": relevant_findings,
        "sources": {str(k): v for k, v in sources.items()},
    }
    return json.dumps(payload, sort_keys=True, default=str), relevant_findings, sources


async def _run_fragment_llm(
    job: Job,
    router: Router,
    *,
    tier: str,
    context: str,
) -> str:
    rendered = load_prompt("fragment_synthesizer", job=job, goal=job.goal)
    agent = Agent(router.model_for(tier), output_type=str, system_prompt=rendered)
    result = await router.call(tier, agent, context)
    output = result.output
    if not isinstance(output, str):
        output = str(output)
    return output


async def _run_subgoal_status_pass(
    job: Job,
    plan: Plan,
    *,
    router: Router,
    final: bool = False,
) -> None:
    """Close open subgoals after fragment assembly (issue #389 / epic #386)."""
    open_subgoals = [sg for sg in plan.subgoals if not sg.done]
    if not open_subgoals:
        return

    from research_agent.storage import coverage

    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM findings WHERE job_id = ?",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    findings_count = int(row["n"]) if row is not None else 0

    context = json.dumps(
        {
            "goal": job.goal,
            "subgoals": [sg.model_dump() for sg in plan.subgoals],
            "coverage_state": coverage.replan_context(job),
            "findings_count": findings_count,
            "final": final,
            "corpus_dossier": bool((job.intake or {}).get("corpus_dossier")),
        },
        sort_keys=True,
        default=str,
    )

    prompt_meta = load_prompt_meta("subgoal_status", job=job)
    tier = prompt_meta.model_tier
    try:
        rendered = load_prompt("subgoal_status", job=job, goal=job.goal)
        agent = Agent(router.model_for(tier), output_type=str, system_prompt=rendered)
        result = await router.call(tier, agent, context)
        raw = result.output if isinstance(result.output, str) else str(result.output)
    except Exception as exc:  # noqa: BLE001 — must not break fragment synth
        emit(
            job,
            "WARN",
            "synth",
            "warning",
            {"stage": "subgoal_status_pass", "error": str(exc), "final": final},
        )
        return

    _, status_map = _extract_subgoal_status(
        job,
        raw,
        subgoal_ids=[sg.id for sg in plan.subgoals],
    )
    if status_map:
        _apply_subgoal_status(job, plan, status_map)


async def _run_fragment_synth(
    job: Job,
    plan: Plan,
    *,
    router: Router,
    final: bool = False,
) -> SynthesisOutput:
    stale_sections = _select_stale_fragments(job, plan)
    prompt_meta = load_prompt_meta("fragment_synthesizer", job=job)
    tier = prompt_meta.model_tier
    model_name = _model_name_for(router, tier)
    all_findings = _load_fragment_findings(job)
    synthesis_version = _latest_synthesis_version(job)

    updated: list[dict[str, Any]] = []
    total_cost = 0.0
    saw_cost = False

    for section_id in stale_sections:
        context, relevant_findings, sources = _build_fragment_context(
            job,
            section_id,
            plan,
            findings=all_findings,
            final=final,
        )
        finding_ids = [int(f["id"]) for f in relevant_findings]
        try:
            content = await _run_fragment_llm(job, router, tier=tier, context=context)
        except BudgetExceeded as exc:
            emit(
                job,
                "WARN",
                "synth",
                "warning",
                {
                    "stage": f"fragment:{section_id}",
                    "tier": tier,
                    "model": model_name,
                    "budget_capped": True,
                    "error": str(exc),
                },
            )
            continue
        except Exception as exc:  # noqa: BLE001
            _emit_synthesis_llm_failed(
                job,
                router,
                tier=tier,
                exc=exc,
                attempt_count=1,
            )
            continue

        if not content.strip():
            emit(
                job,
                "WARN",
                "synth",
                "warning",
                {
                    "stage": f"fragment:{section_id}",
                    "tier": tier,
                    "model": model_name,
                    "error": "empty_fragment_output",
                },
            )
            continue

        cost = getattr(router.budget, "last_cost", None)
        cost_val: float | None = float(cost) if isinstance(cost, (int, float)) else None
        if cost_val is not None:
            total_cost += cost_val
            saw_cost = True
        version = write_fragment(
            job,
            section_id,
            content,
            source_finding_ids=finding_ids,
            cited_source_ids=sorted(sources.keys()),
            synthesis_version=synthesis_version,
            model=model_name,
            tier=tier,
            status="ok",
        )
        emit(
            job,
            "INFO",
            "synth",
            "fragment_update",
            {
                "section_id": section_id,
                "version": version,
                "finding_ids": finding_ids,
                "tier": tier,
                "model": model_name,
                "cost_usd": cost_val,
            },
        )
        updated.append(
            {
                "section_id": section_id,
                "version": version,
                "finding_ids": finding_ids,
            }
        )

    assembled = assemble_report(job)
    if _SOURCES_HEADING_RE.search(assembled):
        assembled = _reconcile_sources(job, assembled, _load_sources_for(job, all_findings))
    cost_usd = total_cost if saw_cost else None
    version = write_synthesis(
        job,
        assembled,
        model="fragment_assembly",
        cost_usd=cost_usd,
    )
    synth_md = (job.root / f"synthesis/{version:04d}.md").read_text(encoding="utf-8")
    report_path = write_report(job, synth_md)
    fragment_versions = {
        section_id: int(item["version"]) for section_id, item in latest_fragments(job).items()
    }
    emit(
        job,
        "INFO",
        "synth",
        "synthesis_written",
        {
            "version": version,
            "tier": tier,
            "truncated": False,
            "report_path": str(report_path),
            "mode": "fragments",
            "fragment_model": model_name,
            "fragment_versions": fragment_versions,
            "stale_sections": list(stale_sections),
            "updated_sections": updated,
            "final": final,
        },
    )

    if updated or final:
        await _run_subgoal_status_pass(job, plan, router=router, final=final)

    return SynthesisOutput(
        version=version,
        content=synth_md,
        model="fragment_assembly",
        cost_usd=cost_usd,
        report_path=str(report_path),
        truncated=False,
    )


async def synthesize_fragments(
    job: Job,
    plan: Plan,
    *,
    router: Router,
    final: bool = False,
) -> SynthesisOutput:
    """Run section-fragment synthesis and deterministically assemble ``report.md``."""

    _emit_synthesis_mode(job, mode="fragments", entrypoint="synthesize_fragments", final=final)
    return await _run_fragment_synth(job, plan, router=router, final=final)


async def _do_synthesis(
    job: Job,
    plan: Plan,
    *,
    router: Router,
    top_n: int,
    final: bool,
) -> SynthesisOutput:
    findings = _load_top_findings(job, top_n)
    sources = _load_sources_for(job, findings)
    prior = _load_prior_synthesis(job)
    critique = _load_latest_critique(job)
    followup_recipes = _load_followup_recipes()
    paid_unblock_recipes = _load_paid_unblock_recipes()
    confirmed_gaps = _compute_confirmed_gaps(job, plan)
    current_hypotheses = _load_current_hypotheses(job, findings)
    artifact_rows = _load_artifacts_for_context(job)

    context = _build_context(
        goal=job.goal,
        plan=plan,
        findings=findings,
        sources=sources,
        prior=prior,
        critique=critique,
        followup_recipes=followup_recipes,
        paid_unblock_recipes=paid_unblock_recipes,
        confirmed_gaps=confirmed_gaps,
        current_hypotheses=current_hypotheses,
        artifacts=artifact_rows,
        final=final,
    )

    primary_tier = "frontier"
    fallback_tier = "frontier_speed"

    try:
        content = await _run_synth(job, router, primary_tier, context)
        used_tier = primary_tier
    except BudgetExceeded as exc:
        logger.warning("synth: budget exceeded on %s tier: %s", primary_tier, exc)
        emit(
            job,
            "WARN",
            "synth",
            "warning",
            {"stage": primary_tier, "error": str(exc)},
        )
        _emit_fallback_tier_used(
            job,
            router,
            primary_tier=primary_tier,
            fallback_tier=fallback_tier,
            reason="budget_exceeded",
        )
        try:
            content = await _run_synth(job, router, fallback_tier, context)
            used_tier = fallback_tier
        except BudgetExceeded as exc2:
            logger.warning("synth: budget exceeded on %s tier: %s", fallback_tier, exc2)
            emit(
                job,
                "WARN",
                "synth",
                "warning",
                {"stage": fallback_tier, "error": str(exc2), "budget_capped": True},
            )
            return _write_stub_output(job)
        except Exception as exc2:  # noqa: BLE001 — terminal retry exhaustion
            logger.warning("synth: %s tier failed after retries: %s", fallback_tier, exc2)
            _emit_synthesis_llm_failed(
                job,
                router,
                tier=fallback_tier,
                exc=exc2,
                attempt_count=2,
            )
            return _write_deterministic_fallback_output(
                job,
                plan,
                primary_tier=primary_tier,
                fallback_tier=fallback_tier,
                primary_exc=exc,
                fallback_exc=exc2,
                top_n=top_n,
                final=final,
            )
    except Exception as exc:  # noqa: BLE001 — terminal retry exhaustion
        logger.warning("synth: %s tier failed after retries: %s", primary_tier, exc)
        _emit_synthesis_llm_failed(
            job,
            router,
            tier=primary_tier,
            exc=exc,
            attempt_count=1,
        )
        _emit_fallback_tier_used(
            job,
            router,
            primary_tier=primary_tier,
            fallback_tier=fallback_tier,
            reason="primary_llm_failed",
        )
        try:
            content = await _run_synth(job, router, fallback_tier, context)
            used_tier = fallback_tier
        except BudgetExceeded as exc2:
            logger.warning("synth: budget exceeded on %s tier: %s", fallback_tier, exc2)
            emit(
                job,
                "WARN",
                "synth",
                "warning",
                {"stage": fallback_tier, "error": str(exc2), "budget_capped": True},
            )
            return _write_template_stub_output(job)
        except Exception as exc2:  # noqa: BLE001 — deterministic fallback handles terminal LLM failure
            logger.warning("synth: %s tier failed after retries: %s", fallback_tier, exc2)
            _emit_synthesis_llm_failed(
                job,
                router,
                tier=fallback_tier,
                exc=exc2,
                attempt_count=2,
            )
            return _write_deterministic_fallback_output(
                job,
                plan,
                primary_tier=primary_tier,
                fallback_tier=fallback_tier,
                primary_exc=exc,
                fallback_exc=exc2,
                top_n=top_n,
                final=final,
            )

    cost = getattr(router.budget, "last_cost", None)
    cost_val: float | None = float(cost) if isinstance(cost, (int, float)) else None
    model_name = _model_name_for(router, used_tier)

    content_without_hypotheses, hypothesis_updates = _extract_hypothesis_updates(job, content)
    stripped_md, status_map = _extract_subgoal_status(
        job,
        content_without_hypotheses,
        subgoal_ids=[sg.id for sg in plan.subgoals],
    )
    stripped_md = _reconcile_sources(job, stripped_md, sources)

    version = write_synthesis(job, stripped_md, model=model_name, cost_usd=cost_val)
    synth_md = (job.root / f"synthesis/{version:04d}.md").read_text(encoding="utf-8")
    report_path = write_report(job, synth_md)

    if status_map:
        _apply_subgoal_status(job, plan, status_map)
    if hypothesis_updates:
        _apply_hypothesis_updates(job, plan, hypothesis_updates)

    emit(
        job,
        "INFO",
        "synth",
        "synthesis_written",
        {
            "version": version,
            "tier": used_tier,
            "truncated": False,
            "report_path": str(report_path),
        },
    )

    return SynthesisOutput(
        version=version,
        content=stripped_md,
        model=model_name,
        cost_usd=cost_val,
        report_path=str(report_path),
        truncated=False,
    )


def _write_failed_output(
    job: Job,
    *,
    tier: str,
    exc: BaseException,
    attempt_count: int,
    partial_content: str = "",
) -> SynthesisOutput:
    """Persist whatever content we managed to assemble + emit ``synthesis_failed``.

    The current call path is non-streaming so ``partial_content`` is "" — the
    failed file still records the traceback for post-run debugging.
    Returns a degraded :class:`SynthesisOutput` (``model='synthesis_failed'``,
    ``truncated=True``) so callers don't have to thread an exception type.
    """
    traceback_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    failed_version = write_synthesis_failed(
        job,
        partial_content,
        model="synthesis_failed",
        traceback_text=traceback_text,
    )
    failed_path = job.root / f"synthesis/{failed_version:04d}.failed.md"
    emit(
        job,
        "ERROR",
        "synth",
        "synthesis_failed",
        {
            "tier": tier,
            "reason": str(exc),
            "attempt_count": attempt_count,
            "failed_path": str(failed_path),
            "traceback": traceback_text,
        },
    )
    return SynthesisOutput(
        version=failed_version,
        content=partial_content,
        model="synthesis_failed",
        cost_usd=None,
        report_path=str(failed_path),
        truncated=True,
    )


def _write_stub_output(job: Job) -> SynthesisOutput:
    version = write_synthesis(
        job,
        _BUDGET_STUB_REPORT,
        model="budget_capped",
        cost_usd=None,
    )
    report_path = write_report(job, _BUDGET_STUB_REPORT)
    emit(
        job,
        "INFO",
        "synth",
        "synthesis_written",
        {
            "version": version,
            "tier": "frontier_speed",
            "truncated": True,
            "report_path": str(report_path),
        },
    )
    return SynthesisOutput(
        version=version,
        content=_BUDGET_STUB_REPORT,
        model="budget_capped",
        cost_usd=None,
        report_path=str(report_path),
        truncated=True,
    )


def _confidence_bucket(conf: float) -> str:
    if conf >= 0.8:
        return "high"
    if conf >= 0.5:
        return "medium"
    return "low"


def _render_template_stub(
    *,
    goal: str,
    findings: list[dict[str, Any]],
    sources: dict[int, dict[str, Any]],
    heading: str = "# Report (budget cap — template stub)",
    intro_lines: list[str] | None = None,
    confirmed_gaps: list[dict[str, Any]] | None = None,
    coverage_units: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> str:
    """Render a no-LLM markdown report from on-disk findings + sources.

    Used when even ``frontier_speed`` precheck blows the cap: every byte
    here comes from the SQLite mirror, so the user always gets a readable
    report.md even with $0 left in the budget.
    """
    intro = intro_lines or [
        "Research budget cap was reached before any synthesis call could run.",
        "This report is a template-rendered summary of the findings already on",
        "disk; no LLM call was made.",
    ]
    lines: list[str] = [
        heading,
        "",
        *intro,
        "",
        "## Goal",
        "",
        goal.strip(),
        "",
        "## Findings",
        "",
    ]

    if not findings:
        lines.append("_No findings recorded before the cap was hit._")
        lines.append("")
    else:
        buckets: dict[str, list[dict[str, Any]]] = {"high": [], "medium": [], "low": []}
        for f in findings:
            buckets[_confidence_bucket(float(f["confidence"]))].append(f)

        for bucket_name in ("high", "medium", "low"):
            bucket = buckets[bucket_name]
            if not bucket:
                continue
            lines.append(f"### {bucket_name.capitalize()} confidence")
            lines.append("")
            for f in bucket:
                sids = f.get("source_ids") or []
                sids_str = (
                    " (sources: " + ", ".join(f"#{sid}" for sid in sids) + ")" if sids else ""
                )
                lines.append(f"- {f['claim']}{sids_str}")
            lines.append("")

    lines.append("## Sources")
    lines.append("")
    if not sources:
        lines.append("_No sources cited by the findings above._")
        lines.append("")
    else:
        for sid in sorted(sources.keys()):
            src = sources[sid]
            title = src.get("title") or "(untitled)"
            url = src.get("url") or "(no url)"
            lines.append(f"- [{sid}] {title} — {url}")
        lines.append("")

    artifact_rows = artifacts or []
    if artifact_rows:
        lines.append("## Artifacts")
        lines.append("")
        for artifact in artifact_rows:
            name = artifact.get("name") or "artifact"
            row_count = artifact.get("row_count")
            csv_path = artifact.get("csv_path") or ""
            coverage = artifact.get("source_coverage") or ""
            row_label = f"{row_count} rows" if row_count is not None else "rows unavailable"
            if csv_path:
                line = f"- {name}: {row_label} — [CSV]({csv_path})"
            else:
                line = f"- {name}: {row_label}"
            if coverage:
                line += f" — {coverage}"
            lines.append(line)
        lines.append("")

    coverage_rows = coverage_units or []
    if coverage_rows:
        lines.append("## Coverage Ledger")
        lines.append("")
        for unit in coverage_rows:
            dimensions = unit.get("dimensions") if isinstance(unit.get("dimensions"), dict) else {}
            dim_label = ", ".join(f"{k}={v}" for k, v in sorted(dimensions.items()))
            if not dim_label:
                dim_label = str(unit.get("dim_key") or "coverage unit")
            status = unit.get("status") or "unknown"
            line = f"- {dim_label}: {status}"
            unblocker = unit.get("unblocker")
            if isinstance(unblocker, str) and unblocker.strip():
                line += f" — {unblocker.strip()}"
            lines.append(line)
        lines.append("")

    gap_rows = confirmed_gaps or []
    if gap_rows:
        lines.append("## Confirmed Gaps")
        lines.append("")
        for gap in gap_rows:
            topic = gap.get("topic") or "Unresolved gap"
            summary = gap.get("failure_summary") or "Could not resolve from available sources."
            unblocker = gap.get("suggested_unblocker") or "Identify a source owner or custodian."
            lines.append(f"- **{topic}**: {summary} Unblocker: {unblocker}")
            attempts = gap.get("attempts")
            if isinstance(attempts, list):
                for attempt in attempts[:5]:
                    if not isinstance(attempt, dict):
                        continue
                    kind = attempt.get("task_kind") or "task"
                    query = attempt.get("query") or topic
                    reason = attempt.get("failure_reason") or "failed"
                    count = attempt.get("count") or 1
                    lines.append(f"  - {kind} `{query}`: {reason} ({count}x)")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _write_deterministic_fallback_output(
    job: Job,
    plan: Plan,
    *,
    primary_tier: str | None,
    fallback_tier: str,
    primary_exc: BaseException | None,
    fallback_exc: BaseException,
    top_n: int,
    final: bool,
    post_cap: bool = False,
) -> SynthesisOutput:
    """Write a no-LLM report after terminal non-budget synthesis failures."""
    # The traceback section in _render_failed_synthesis_md already wraps the
    # body in a ```text fence, so emit plain section markers here and let the
    # outer fence apply — nested fences would break markdown rendering.
    traceback_parts: list[str] = []
    if primary_exc is not None:
        traceback_parts.append(
            "--- Primary Tier Traceback ---\n"
            + "".join(
                traceback.format_exception(
                    type(primary_exc),
                    primary_exc,
                    primary_exc.__traceback__,
                )
            ).strip()
        )
    traceback_parts.append(
        "--- Fallback Tier Traceback ---\n"
        + "".join(
            traceback.format_exception(
                type(fallback_exc),
                fallback_exc,
                fallback_exc.__traceback__,
            )
        ).strip()
    )
    failed_version = write_synthesis_failed(
        job,
        "",
        model="synthesis_llm_failed",
        traceback_text="\n\n".join(traceback_parts),
    )
    failed_path = job.root / f"synthesis/{failed_version:04d}.failed.md"

    findings = _load_top_findings(job, top_n)
    sources = _load_sources_for(job, findings)
    confirmed_gaps = _compute_confirmed_gaps(job, plan)
    artifact_rows = _load_artifacts_for_context(job)
    coverage_rows = _load_coverage_for_context(job)
    content = _render_template_stub(
        goal=job.goal,
        findings=findings,
        sources=sources,
        heading="# Report (deterministic fallback)",
        intro_lines=[
            "The configured LLM synthesis tiers failed, so this report was",
            "rendered deterministically from persisted findings, sources,",
            "coverage, gaps, and table artifacts. No LLM wrote this report.",
        ],
        confirmed_gaps=confirmed_gaps,
        coverage_units=coverage_rows,
        artifacts=artifact_rows,
    )

    version = write_synthesis(
        job,
        content,
        model="deterministic_fallback",
        cost_usd=None,
    )
    report_path = write_report(job, content)
    emit(
        job,
        "INFO",
        "synth",
        "synthesis_deterministic_fallback_written",
        {
            "version": version,
            "report_path": str(report_path),
            "failed_path": str(failed_path),
            "primary_tier": primary_tier,
            "fallback_tier": fallback_tier,
            "findings_count": len(findings),
            "confirmed_gaps_count": len(confirmed_gaps),
            "has_artifacts": bool(artifact_rows),
            "has_coverage_ledger": bool(coverage_rows),
            "final": final,
            "post_cap": post_cap,
        },
    )
    return SynthesisOutput(
        version=version,
        content=content,
        model="deterministic_fallback",
        cost_usd=None,
        report_path=str(report_path),
        truncated=True,
    )


def _write_template_stub_output(job: Job) -> SynthesisOutput:
    """Build a markdown report from on-disk findings/sources — no LLM call.

    Falls back to :data:`_BUDGET_STUB_REPORT` only when there are zero
    findings, since rendering an empty bullet list isn't useful.
    """
    findings = _load_top_findings(job, FINAL_TOP_N)
    sources = _load_sources_for(job, findings)

    if not findings:
        content = _BUDGET_STUB_REPORT
    else:
        content = _render_template_stub(
            goal=job.goal,
            findings=findings,
            sources=sources,
        )

    version = write_synthesis(
        job,
        content,
        model="budget_capped_template",
        cost_usd=None,
    )
    report_path = write_report(job, content)
    emit(
        job,
        "INFO",
        "synth",
        "synthesis_written",
        {
            "version": version,
            "tier": "template_stub",
            "truncated": True,
            "report_path": str(report_path),
        },
    )
    return SynthesisOutput(
        version=version,
        content=content,
        model="budget_capped_template",
        cost_usd=None,
        report_path=str(report_path),
        truncated=True,
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def synthesize(job: Job, plan: Plan, *, router: Router) -> SynthesisOutput:
    """Run an in-loop synthesis pass on the cloud ``frontier`` tier.

    Reads the top :data:`TOP_N_FINDINGS` findings (by confidence), the
    sources they cite, the prior synthesis (if any), and the latest
    critique (if any). Persists a new synthesis version, rotates the prior
    ``report.md`` into ``report.history/`` and writes the fresh content.
    """
    if fragment_synth_enabled():
        return await synthesize_fragments(job, plan, router=router, final=False)
    _emit_synthesis_mode(job, mode="legacy", entrypoint="synthesize", final=False)
    return await _do_synthesis(
        job,
        plan,
        router=router,
        top_n=TOP_N_FINDINGS,
        final=False,
    )


async def final_synthesis(job: Job, plan: Plan, *, router: Router) -> SynthesisOutput:
    """End-of-job synthesis pass — larger window, ``final=True`` flag in context.

    Uses :data:`FINAL_TOP_N` instead of :data:`TOP_N_FINDINGS` so the final
    report can draw on the full investigation. The same budget fallback
    ladder applies (frontier → frontier_speed → stub).
    """
    if fragment_synth_enabled():
        return await synthesize_fragments(job, plan, router=router, final=True)
    _emit_synthesis_mode(job, mode="legacy", entrypoint="final_synthesis", final=True)
    return await _do_synthesis(
        job,
        plan,
        router=router,
        top_n=FINAL_TOP_N,
        final=True,
    )


async def final_synthesis_after_cap(
    job: Job,
    plan: Plan,
    *,
    router: Router,
) -> SynthesisOutput:
    """Final-pass synthesis triggered after the loop hit the per-job budget cap.

    The ``frontier`` tier is skipped: we already know the cap was tripped, so
    going straight to ``frontier_speed`` saves the precheck overhead and any
    misleading "fell back to fallback" event noise. If even ``frontier_speed``
    blows the precheck (truly $0 left), :func:`_write_template_stub_output`
    renders a report from on-disk findings without making any LLM call.
    """
    if fragment_synth_enabled():
        return await synthesize_fragments(job, plan, router=router, final=True)
    _emit_synthesis_mode(
        job,
        mode="legacy",
        entrypoint="final_synthesis_after_cap",
        final=True,
    )

    findings = _load_top_findings(job, FINAL_TOP_N)
    sources = _load_sources_for(job, findings)
    prior = _load_prior_synthesis(job)
    critique = _load_latest_critique(job)
    followup_recipes = _load_followup_recipes()
    paid_unblock_recipes = _load_paid_unblock_recipes()
    confirmed_gaps = _compute_confirmed_gaps(job, plan)
    current_hypotheses = _load_current_hypotheses(job, findings)
    artifact_rows = _load_artifacts_for_context(job)

    context = _build_context(
        goal=job.goal,
        plan=plan,
        findings=findings,
        sources=sources,
        prior=prior,
        critique=critique,
        followup_recipes=followup_recipes,
        paid_unblock_recipes=paid_unblock_recipes,
        confirmed_gaps=confirmed_gaps,
        current_hypotheses=current_hypotheses,
        artifacts=artifact_rows,
        final=True,
    )

    fallback_tier = "frontier_speed"
    try:
        content = await _run_synth(job, router, fallback_tier, context)
    except BudgetExceeded as exc:
        logger.warning("synth: budget exceeded on %s tier (post-cap): %s", fallback_tier, exc)
        emit(
            job,
            "WARN",
            "synth",
            "warning",
            {"stage": fallback_tier, "error": str(exc), "budget_capped": True},
        )
        return _write_template_stub_output(job)
    except Exception as exc:  # noqa: BLE001 — terminal retry exhaustion
        logger.warning("synth: %s tier failed after retries (post-cap): %s", fallback_tier, exc)
        _emit_synthesis_llm_failed(
            job,
            router,
            tier=fallback_tier,
            exc=exc,
            attempt_count=1,
            post_cap=True,
        )
        return _write_deterministic_fallback_output(
            job,
            plan,
            primary_tier=None,
            fallback_tier=fallback_tier,
            primary_exc=None,
            fallback_exc=exc,
            top_n=FINAL_TOP_N,
            final=True,
            post_cap=True,
        )

    cost = getattr(router.budget, "last_cost", None)
    cost_val: float | None = float(cost) if isinstance(cost, (int, float)) else None
    model_name = _model_name_for(router, fallback_tier)

    content_without_hypotheses, hypothesis_updates = _extract_hypothesis_updates(job, content)
    stripped_md, status_map = _extract_subgoal_status(
        job,
        content_without_hypotheses,
        subgoal_ids=[sg.id for sg in plan.subgoals],
    )
    stripped_md = _reconcile_sources(job, stripped_md, sources)

    version = write_synthesis(job, stripped_md, model=model_name, cost_usd=cost_val)
    synth_md = (job.root / f"synthesis/{version:04d}.md").read_text(encoding="utf-8")
    report_path = write_report(job, synth_md)

    if status_map:
        _apply_subgoal_status(job, plan, status_map)
    if hypothesis_updates:
        _apply_hypothesis_updates(job, plan, hypothesis_updates)

    emit(
        job,
        "INFO",
        "synth",
        "synthesis_written",
        {
            "version": version,
            "tier": fallback_tier,
            "truncated": False,
            "report_path": str(report_path),
            "post_cap": True,
        },
    )

    return SynthesisOutput(
        version=version,
        content=stripped_md,
        model=model_name,
        cost_usd=cost_val,
        report_path=str(report_path),
        truncated=False,
    )


__all__ = [
    "FINAL_TOP_N",
    "SynthesisOutput",
    "TOP_N_FINDINGS",
    "final_synthesis",
    "final_synthesis_after_cap",
    "synthesize",
]
