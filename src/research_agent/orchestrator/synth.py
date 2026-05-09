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
from datetime import UTC, datetime
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent

from research_agent.llm.budgets import BudgetExceeded
from research_agent.observability.events import emit
from research_agent.prompts.loader import load_prompt
from research_agent.storage import db
from research_agent.storage.markdown import (
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


def _load_top_findings(job: Job, n: int) -> list[dict[str, Any]]:
    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, claim, confidence, source_ids, tags
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
        out.append(
            {
                "id": int(row["id"]),
                "claim": row["claim"],
                "confidence": float(row["confidence"]),
                "source_ids": json.loads(source_ids_raw) if source_ids_raw else [],
                "tags": json.loads(tags_raw) if tags_raw else [],
            }
        )
    return out


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
    final: bool = False,
) -> str:
    payload: dict[str, Any] = {
        "goal": goal,
        "scope_class": str(plan.scope_class) if plan.scope_class else None,
        "subgoals": [
            {"id": sg.id, "description": sg.description, "done": sg.done} for sg in plan.subgoals
        ],
        "findings": findings,
        "sources": {str(k): v for k, v in sources.items()},
        "prior_synthesis": prior,
        "critique": critique,
        "followup_recipes": followup_recipes,
        "paid_unblock_recipes": paid_unblock_recipes,
        "department_coverage": _compute_department_coverage(findings),
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


def _apply_subgoal_status(
    job: Job,
    plan: Plan,  # noqa: ARG001 — kept for clarity at call sites; helper reloads from DB
    status_map: dict[int, str],
) -> None:
    """Persist a synthesizer-emitted ``subgoal_status`` map onto the latest plan."""
    from research_agent.orchestrator import plan as _plan_mod

    _plan_mod.update_subgoal_done(job, status_map)


_CITATION_RE = re.compile(r"\[(\d+(?:,\s*\d+)*)\]")
_SOURCES_HEADING_RE = re.compile(r"^##\s+Sources\s*$", re.MULTILINE)
_NUMBERED_LINE_RE = re.compile(r"^(\d+)\.\s", re.MULTILINE)


def _format_source_line(sid: int, src: dict[str, Any]) -> str:
    """Render the canonical synthesizer Sources-section line shape (issue #207)."""
    url = src.get("url") or "(no url)"
    title = src.get("title") or "(untitled)"
    fetched_at = src.get("fetched_at")
    if fetched_at is None:
        date_str = "unknown"
    else:
        date_str = datetime.fromtimestamp(int(fetched_at), tz=UTC).strftime("%Y-%m-%d")
    return f'{sid}. {url} — "{title}" (retrieved {date_str})'


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
    headings = list(_SOURCES_HEADING_RE.finditer(md))
    if headings:
        last = headings[-1]
        body_text = md[: last.start()]
        sources_text = md[last.start() :]
    else:
        body_text = md
        sources_text = ""

    cited_in_body: list[int] = []
    seen: set[int] = set()
    for match in _CITATION_RE.finditer(body_text):
        for token in match.group(1).split(","):
            try:
                sid = int(token.strip())
            except ValueError:
                continue
            if sid not in seen:
                seen.add(sid)
                cited_in_body.append(sid)

    enumerated: set[int] = set()
    for match in _NUMBERED_LINE_RE.finditer(sources_text):
        try:
            enumerated.add(int(match.group(1)))
        except ValueError:
            continue

    missing = [sid for sid in cited_in_body if sid not in enumerated]
    if not missing:
        return md

    added: list[int] = []
    unresolved: list[int] = []
    new_lines: list[str] = []
    for sid in missing:
        src = sources_by_id.get(sid)
        if src is None:
            unresolved.append(sid)
            continue
        added.append(sid)
        new_lines.append(_format_source_line(sid, src))

    emit(
        job,
        "INFO",
        "synth",
        "source_list_reconciled",
        {
            "added": added,
            "unresolved": unresolved,
            "already_listed": len(enumerated),
            "cited_total": len(cited_in_body),
        },
    )

    if not new_lines:
        return md

    suffix = "\n".join(new_lines) + "\n"
    if md.endswith("\n"):
        return md + suffix
    return md + "\n" + suffix


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

    context = _build_context(
        goal=job.goal,
        plan=plan,
        findings=findings,
        sources=sources,
        prior=prior,
        critique=critique,
        followup_recipes=followup_recipes,
        paid_unblock_recipes=paid_unblock_recipes,
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
            return _write_failed_output(job, tier=fallback_tier, exc=exc2, attempt_count=2)
    except Exception as exc:  # noqa: BLE001 — terminal retry exhaustion
        logger.warning("synth: %s tier failed after retries: %s", primary_tier, exc)
        return _write_failed_output(job, tier=primary_tier, exc=exc, attempt_count=1)

    cost = getattr(router.budget, "last_cost", None)
    cost_val: float | None = float(cost) if isinstance(cost, (int, float)) else None
    model_name = _model_name_for(router, used_tier)

    stripped_md, status_map = _extract_subgoal_status(
        job,
        content,
        subgoal_ids=[sg.id for sg in plan.subgoals],
    )
    stripped_md = _reconcile_sources(job, stripped_md, sources)

    version = write_synthesis(job, stripped_md, model=model_name, cost_usd=cost_val)
    synth_md = (job.root / f"synthesis/{version:04d}.md").read_text(encoding="utf-8")
    report_path = write_report(job, synth_md)

    if status_map:
        _apply_subgoal_status(job, plan, status_map)

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
) -> str:
    """Render a no-LLM markdown report from on-disk findings + sources.

    Used when even ``frontier_speed`` precheck blows the cap: every byte
    here comes from the SQLite mirror, so the user always gets a readable
    report.md even with $0 left in the budget.
    """
    lines: list[str] = [
        "# Report (budget cap — template stub)",
        "",
        "Research budget cap was reached before any synthesis call could run.",
        "This report is a template-rendered summary of the findings already on",
        "disk; no LLM call was made.",
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

    return "\n".join(lines).rstrip() + "\n"


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
    findings = _load_top_findings(job, FINAL_TOP_N)
    sources = _load_sources_for(job, findings)
    prior = _load_prior_synthesis(job)
    critique = _load_latest_critique(job)
    followup_recipes = _load_followup_recipes()
    paid_unblock_recipes = _load_paid_unblock_recipes()

    context = _build_context(
        goal=job.goal,
        plan=plan,
        findings=findings,
        sources=sources,
        prior=prior,
        critique=critique,
        followup_recipes=followup_recipes,
        paid_unblock_recipes=paid_unblock_recipes,
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
        return _write_failed_output(job, tier=fallback_tier, exc=exc, attempt_count=1)

    cost = getattr(router.budget, "last_cost", None)
    cost_val: float | None = float(cost) if isinstance(cost, (int, float)) else None
    model_name = _model_name_for(router, fallback_tier)

    stripped_md, status_map = _extract_subgoal_status(
        job,
        content,
        subgoal_ids=[sg.id for sg in plan.subgoals],
    )
    stripped_md = _reconcile_sources(job, stripped_md, sources)

    version = write_synthesis(job, stripped_md, model=model_name, cost_usd=cost_val)
    synth_md = (job.root / f"synthesis/{version:04d}.md").read_text(encoding="utf-8")
    report_path = write_report(job, synth_md)

    if status_map:
        _apply_subgoal_status(job, plan, status_map)

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
