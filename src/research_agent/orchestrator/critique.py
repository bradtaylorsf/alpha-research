"""Critique module — gap analysis on a synthesis report.

Implements the critic pass from the implementation guide: cloud
``frontier_alt`` tier (a *different* model family than the synthesizer's
``frontier`` tier) so the synthesizer and critic disagree productively
instead of agreeing themselves into echo chambers.

The critic reads the top N findings (and the sources they cite), the
latest synthesis content, and any prior critique, packs them into a JSON
context payload, and asks the model to emit a structured
:class:`CritiqueOutput` (gaps, unsupported claims, suggested subgoals,
confidence concerns, and a ``should_replan`` boolean).

The output drives two things:

* persisted as ``critique/<v>.md`` + ``.json`` and a ``critiques`` DB row
* when ``should_replan`` is true the loop calls
  :func:`research_agent.orchestrator.plan.cloud_replan` with the critique
  attached so the planner can rewrite the next plan version

Budget handling mirrors :mod:`synth`: on :class:`BudgetExceeded` we log
WARN, emit a ``warning`` event, and return a stub :class:`CritiqueOutput`
(no DB row, ``should_replan=False``) so a flaky cap never blocks loop
progress — synth has a fallback ladder, critique just no-ops.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent

from research_agent.llm.budgets import BudgetExceeded
from research_agent.observability.events import emit
from research_agent.prompts.loader import load_prompt
from research_agent.storage import db
from research_agent.storage.markdown import write_critique

if TYPE_CHECKING:
    from research_agent.llm.router import Router
    from research_agent.orchestrator.plan import Plan
    from research_agent.storage.jobs import Job

logger = logging.getLogger(__name__)

TOP_N_FINDINGS = 50
DEFAULT_TIER = "frontier_alt"


class Gap(BaseModel):
    """A single gap the critic flagged in the synthesis."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    severity: Literal["block", "warn", "nit"]
    area: str | None = None


class CritiqueOutput(BaseModel):
    """Structured critique consumed by the loop + the planner.

    The ``version``/``model``/``cost_usd``/``md_path`` fields are filled in
    after the row is persisted so callers can render or log the result
    without re-querying the DB.
    """

    model_config = ConfigDict(extra="forbid")

    gaps: list[Gap] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    suggested_subgoals: list[str] = Field(default_factory=list)
    confidence_concerns: list[str] = Field(default_factory=list)
    should_replan: bool = False

    version: int = 0
    model: str = ""
    cost_usd: float | None = None
    md_path: str = ""


# ---------------------------------------------------------------------------
# Helpers — mirror synth.py loaders so behavior stays consistent
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


def _load_prior_critique(job: Job) -> str | None:
    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            "SELECT md_path FROM critiques WHERE job_id = ? ORDER BY version DESC LIMIT 1",
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


def _build_context(
    *,
    goal: str,
    findings: list[dict[str, Any]],
    sources: dict[int, dict[str, Any]],
    synthesis: str | None,
    prior_critique: str | None,
) -> str:
    payload: dict[str, Any] = {
        "goal": goal,
        "findings": findings,
        "sources": {str(k): v for k, v in sources.items()},
        "synthesis": synthesis,
        "prior_critique": prior_critique,
    }
    return json.dumps(payload, sort_keys=True, default=str)


def _model_name_for(router: Router, tier: str) -> str:
    spec = router.tiers.get(tier, {})
    name = spec.get("model")
    if not isinstance(name, str) or not name:
        return tier
    return name


def _render_critique_md(payload: CritiqueOutput) -> str:
    """Render a human-readable summary for ``critique/<v>.md``."""
    lines: list[str] = ["# Critique\n"]
    lines.append(f"**should_replan:** {'yes' if payload.should_replan else 'no'}\n")

    lines.append("\n## Gaps\n")
    if payload.gaps:
        for gap in payload.gaps:
            area = f" [{gap.area}]" if gap.area else ""
            lines.append(f"- **{gap.severity}**{area}: {gap.description}\n")
    else:
        lines.append("- (none)\n")

    lines.append("\n## Unsupported claims\n")
    if payload.unsupported_claims:
        for claim in payload.unsupported_claims:
            lines.append(f"- {claim}\n")
    else:
        lines.append("- (none)\n")

    lines.append("\n## Suggested subgoals\n")
    if payload.suggested_subgoals:
        for sg in payload.suggested_subgoals:
            lines.append(f"- {sg}\n")
    else:
        lines.append("- (none)\n")

    lines.append("\n## Confidence concerns\n")
    if payload.confidence_concerns:
        for cc in payload.confidence_concerns:
            lines.append(f"- {cc}\n")
    else:
        lines.append("- (none)\n")

    return "".join(lines)


def _stub_output() -> CritiqueOutput:
    """Return an empty critique used when the budget cap blocks the call."""
    return CritiqueOutput(
        gaps=[],
        unsupported_claims=[],
        suggested_subgoals=[],
        confidence_concerns=[],
        should_replan=False,
        version=0,
        model="budget_capped",
        cost_usd=None,
        md_path="",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def critique(
    job: Job,
    plan: Plan,
    latest_synthesis: str | None,
    *,
    router: Router,
    tier: str = DEFAULT_TIER,
) -> CritiqueOutput:
    """Run a critique pass on the cloud ``frontier_alt`` tier by default.

    The critic reads the top :data:`TOP_N_FINDINGS` findings (by confidence),
    the sources they cite, the latest synthesis, and the prior critique (if
    any). On budget exhaustion this returns a stub :class:`CritiqueOutput`
    with ``should_replan=False`` and emits a ``warning`` event — critique is
    advisory, not load-bearing.
    """
    findings = _load_top_findings(job, TOP_N_FINDINGS)
    sources = _load_sources_for(job, findings)
    prior = _load_prior_critique(job)

    context = _build_context(
        goal=job.goal,
        findings=findings,
        sources=sources,
        synthesis=latest_synthesis,
        prior_critique=prior,
    )

    rendered = load_prompt("critic", job=job, goal=job.goal)
    agent = Agent(
        router.model_for(tier),
        output_type=CritiqueOutput,
        system_prompt=rendered,
    )

    try:
        result = await router.call(tier, agent, context)
    except BudgetExceeded as exc:
        logger.warning("critique: budget exceeded on %s tier: %s", tier, exc)
        emit(
            job,
            "WARN",
            "critique",
            "warning",
            {"stage": tier, "error": str(exc), "budget_capped": True},
        )
        return _stub_output()

    output = result.output
    if not isinstance(output, CritiqueOutput):
        output = CritiqueOutput.model_validate(output)

    cost_raw = getattr(router.budget, "last_cost", None)
    cost_val: float | None = float(cost_raw) if isinstance(cost_raw, (int, float)) else None
    model_name = _model_name_for(router, tier)

    md_body = _render_critique_md(output)
    payload_dict = output.model_dump(exclude={"version", "model", "cost_usd", "md_path"})
    version = write_critique(
        job,
        payload=payload_dict,
        content=md_body,
        model=model_name,
        cost_usd=cost_val,
        should_replan=output.should_replan,
    )

    md_rel = f"critique/{version:04d}.md"
    enriched = output.model_copy(
        update={
            "version": version,
            "model": model_name,
            "cost_usd": cost_val,
            "md_path": md_rel,
        }
    )

    emit(
        job,
        "INFO",
        "critique",
        "critique_written",
        {
            "version": version,
            "tier": tier,
            "model": model_name,
            "should_replan": bool(output.should_replan),
            "gaps_count": len(output.gaps),
            "replan_triggered": False,
        },
    )

    return enriched


__all__ = [
    "DEFAULT_TIER",
    "TOP_N_FINDINGS",
    "CritiqueOutput",
    "Gap",
    "critique",
]
