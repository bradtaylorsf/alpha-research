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
    write_synthesis_partial,
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
        body = (files("research_agent.prompts") / "followup_recipes.md").read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, OSError) as exc:
        if not _FOLLOWUP_RECIPES_WARN_LOGGED:
            logger.warning("synth: followup_recipes.md unavailable: %s", exc)
            _FOLLOWUP_RECIPES_WARN_LOGGED = True
        body = ""
    _FOLLOWUP_RECIPES = body
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


def _build_context(
    *,
    goal: str,
    plan: Plan,
    findings: list[dict[str, Any]],
    sources: dict[int, dict[str, Any]],
    prior: str | None,
    critique: str | None,
    followup_recipes: str,
    final: bool = False,
) -> str:
    payload: dict[str, Any] = {
        "goal": goal,
        "subgoals": [
            {"id": sg.id, "description": sg.description, "done": sg.done}
            for sg in plan.subgoals
        ],
        "findings": findings,
        "sources": {str(k): v for k, v in sources.items()},
        "prior_synthesis": prior,
        "critique": critique,
        "followup_recipes": followup_recipes,
    }
    if final:
        payload["final"] = True
    return json.dumps(payload, sort_keys=True, default=str)


_SUBGOAL_STATUS_FENCE_RE = re.compile(
    r"```[ \t]*json[ \t]*\n(.*?)\n```\s*$", re.DOTALL
)


def _extract_subgoal_status(
    job: Job,
    raw: str,
) -> tuple[str, dict[int, str] | None]:
    """Split a trailing fenced ``json`` block off the markdown report.

    Returns ``(stripped_md, status_map)``. ``status_map`` is ``None`` when
    the block is missing, has malformed JSON, or its payload doesn't carry
    a ``subgoal_status`` mapping. A WARN ``warning`` event is emitted in
    each of those tolerated-but-degraded cases.
    """
    match = _SUBGOAL_STATUS_FENCE_RE.search(raw)
    if match is None:
        emit(
            job,
            "WARN",
            "synth",
            "warning",
            {"stage": "subgoal_status", "error": "trailing JSON fence missing"},
        )
        return raw, None

    body = match.group(1).strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        emit(
            job,
            "WARN",
            "synth",
            "warning",
            {"stage": "subgoal_status", "error": f"json parse failed: {exc}"},
        )
        # Still strip the fence so it never lands in report.md.
        return raw[: match.start()].rstrip() + "\n", None

    if not isinstance(data, dict) or not isinstance(data.get("subgoal_status"), dict):
        emit(
            job,
            "WARN",
            "synth",
            "warning",
            {"stage": "subgoal_status", "error": "missing or non-dict subgoal_status"},
        )
        return raw[: match.start()].rstrip() + "\n", None

    status_map: dict[int, str] = {}
    for key, value in data["subgoal_status"].items():
        try:
            sid = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, str):
            status_map[sid] = value

    stripped = raw[: match.start()].rstrip() + "\n"
    return stripped, status_map


def _apply_subgoal_status(
    job: Job,
    plan: Plan,  # noqa: ARG001 — kept for clarity at call sites; helper reloads from DB
    status_map: dict[int, str],
) -> None:
    """Persist a synthesizer-emitted ``subgoal_status`` map onto the latest plan."""
    from research_agent.orchestrator import plan as _plan_mod

    _plan_mod.update_subgoal_done(job, status_map)


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

    context = _build_context(
        goal=job.goal,
        plan=plan,
        findings=findings,
        sources=sources,
        prior=prior,
        critique=critique,
        followup_recipes=followup_recipes,
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

    stripped_md, status_map = _extract_subgoal_status(job, content)

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
    file is still written so the next attempt can spot it as prior context.
    Returns a degraded :class:`SynthesisOutput` (``model='synthesis_failed'``,
    ``truncated=True``) so callers don't have to thread an exception type.
    """
    partial_version = write_synthesis_partial(job, partial_content, model="synthesis_failed")
    partial_path = job.root / f"synthesis/{partial_version:04d}.partial.md"
    emit(
        job,
        "ERROR",
        "synth",
        "synthesis_failed",
        {
            "tier": tier,
            "reason": str(exc),
            "attempt_count": attempt_count,
            "partial_path": str(partial_path),
        },
    )
    return SynthesisOutput(
        version=partial_version,
        content=partial_content,
        model="synthesis_failed",
        cost_usd=None,
        report_path=str(partial_path),
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

    context = _build_context(
        goal=job.goal,
        plan=plan,
        findings=findings,
        sources=sources,
        prior=prior,
        critique=critique,
        followup_recipes=followup_recipes,
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

    stripped_md, status_map = _extract_subgoal_status(job, content)

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
