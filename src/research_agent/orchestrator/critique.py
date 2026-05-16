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
from research_agent.storage.markdown import (
    latest_fragment_critique,
    latest_fragments,
    write_critique,
    write_fragment_critique,
)

if TYPE_CHECKING:
    from research_agent.llm.router import Router
    from research_agent.orchestrator.plan import Plan
    from research_agent.storage.jobs import Job

logger = logging.getLogger(__name__)

TOP_N_FINDINGS = 50
DEFAULT_TIER = "frontier_alt"
FRAGMENT_CONFIDENCE_SKIP_THRESHOLD = 0.85


class Gap(BaseModel):
    """A single gap the critic flagged in the synthesis."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    severity: Literal["block", "warn", "nit"]
    area: str | None = None


class PaidOpportunity(BaseModel):
    """A paid resource that would close a specific evidenced gap.

    ``service`` and ``cost_range`` come verbatim from the
    ``paid_unblock_recipes`` catalog (e.g., "LinkedIn Premium",
    "$60–$150/mo"); ``gap`` ties the recommendation back to a concrete
    finding/subject; ``tier`` distinguishes "the paid resource is the
    only realistic path" (``high``) from "a public alternative may
    suffice" (``low``).
    """

    model_config = ConfigDict(extra="forbid")

    service: str = Field(min_length=1)
    cost_range: str = Field(min_length=1)
    gap: str = Field(min_length=1)
    tier: Literal["high", "low"]


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
    premature_subgoals: list[int] = Field(default_factory=list)
    paid_opportunities: list[PaidOpportunity] = Field(default_factory=list)
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
    paid_unblock_recipes: str,
) -> str:
    payload: dict[str, Any] = {
        "goal": goal,
        "findings": findings,
        "sources": {str(k): v for k, v in sources.items()},
        "synthesis": synthesis,
        "prior_critique": prior_critique,
        "paid_unblock_recipes": paid_unblock_recipes,
    }
    return json.dumps(payload, sort_keys=True, default=str)


def _model_name_for(router: Router, tier: str) -> str:
    spec = router.tiers.get(tier, {})
    name = spec.get("model")
    if not isinstance(name, str) or not name:
        return tier
    return name


def _actual_call_tier_model(router: Router, requested_tier: str) -> tuple[str, str]:
    """Return the tier/model that produced the last router result when available."""
    metadata = getattr(router, "last_call_metadata", None)
    if isinstance(metadata, dict):
        tier = metadata.get("tier")
        model = metadata.get("model")
        if isinstance(tier, str) and tier and isinstance(model, str) and model:
            return tier, model
    return requested_tier, _model_name_for(router, requested_tier)


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

    lines.append("\n## Premature subgoal closures\n")
    if payload.premature_subgoals:
        for sid in payload.premature_subgoals:
            lines.append(f"- subgoal {sid}\n")
    else:
        lines.append("- (none)\n")

    lines.append("\n## Paid resource opportunities\n")
    if payload.paid_opportunities:
        for opp in payload.paid_opportunities:
            lines.append(
                f"- **{opp.tier}**: {opp.service} ({opp.cost_range}) — {opp.gap}\n"
            )
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
        paid_opportunities=[],
        should_replan=False,
        version=0,
        model="budget_capped",
        cost_usd=None,
        md_path="",
    )


def _fragment_issue_confidence(output: CritiqueOutput) -> float:
    """Map a critique result to a coarse confidence score for skip decisions."""
    if output.should_replan or any(gap.severity == "block" for gap in output.gaps):
        return 0.4
    if (
        output.gaps
        or output.unsupported_claims
        or output.suggested_subgoals
        or output.confidence_concerns
        or output.premature_subgoals
    ):
        return 0.7
    return 0.95


def _fragment_status(output: CritiqueOutput) -> str:
    if output.should_replan:
        return "replan"
    if (
        output.gaps
        or output.unsupported_claims
        or output.suggested_subgoals
        or output.confidence_concerns
        or output.premature_subgoals
    ):
        return "issues"
    return "ok"


def _render_fragment_critique_md(
    section_id: str,
    fragment_version: int,
    payload: CritiqueOutput,
) -> str:
    return (
        f"# Fragment Critique: {section_id}\n\n"
        f"**fragment_version:** {fragment_version}\n\n"
        + _render_critique_md(payload)
    )


def _merge_fragment_outputs(outputs: list[CritiqueOutput]) -> CritiqueOutput:
    merged = CritiqueOutput()
    for output in outputs:
        merged.gaps.extend(output.gaps)
        merged.unsupported_claims.extend(output.unsupported_claims)
        merged.suggested_subgoals.extend(output.suggested_subgoals)
        merged.confidence_concerns.extend(output.confidence_concerns)
        merged.premature_subgoals.extend(output.premature_subgoals)
        merged.paid_opportunities.extend(output.paid_opportunities)
        merged.should_replan = merged.should_replan or output.should_replan
    return merged


def _should_skip_fragment_critique(
    *,
    fragment: dict[str, Any],
    prior: dict[str, Any] | None,
    stale_sections: set[str],
) -> tuple[bool, str]:
    section_id = str(fragment["section_id"])
    if prior is None:
        return False, "missing_prior_critique"
    if section_id in stale_sections:
        return False, "stale_fragment"
    if int(prior["fragment_version"]) != int(fragment["version"]):
        return False, "fragment_version_changed"

    fragment_confidence = fragment.get("confidence")
    if isinstance(fragment_confidence, (int, float)) and not isinstance(
        fragment_confidence, bool
    ):
        if float(fragment_confidence) < FRAGMENT_CONFIDENCE_SKIP_THRESHOLD:
            return False, "low_fragment_confidence"

    prior_confidence = prior.get("confidence")
    if isinstance(prior_confidence, (int, float)) and not isinstance(prior_confidence, bool):
        if float(prior_confidence) < FRAGMENT_CONFIDENCE_SKIP_THRESHOLD:
            return False, "low_prior_critique_confidence"

    if prior.get("should_replan"):
        return False, "prior_requested_replan"
    return True, "unchanged_high_confidence"


def _build_fragment_critique_context(
    job: Job,
    section_id: str,
    plan: Plan,
    *,
    fragment: dict[str, Any],
) -> str:
    """Build bounded context for critiquing one report fragment."""
    from research_agent.orchestrator.synth import (
        _build_fragment_context,
        _load_paid_unblock_recipes,
    )

    context, _findings, _sources = _build_fragment_context(job, section_id, plan)
    payload = json.loads(context)
    prior = latest_fragment_critique(job, section_id)
    payload["mode"] = "fragment_critique"
    payload["fragment"] = {
        "section_id": section_id,
        "version": int(fragment["version"]),
        "content": fragment["content"],
        "confidence": fragment.get("confidence"),
        "status": fragment.get("status"),
    }
    payload["prior_fragment_critique"] = (
        {
            "version": int(prior["version"]),
            "fragment_version": int(prior["fragment_version"]),
            "status": prior.get("status"),
            "confidence": prior.get("confidence"),
            "should_replan": bool(prior.get("should_replan")),
            "payload": prior.get("payload") or {},
        }
        if prior is not None
        else None
    )
    payload["paid_unblock_recipes"] = _load_paid_unblock_recipes()
    payload.pop("prior_fragment", None)
    return json.dumps(payload, sort_keys=True, default=str)


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
    # Reuse synth's loader so the catalog cache + missing-file behavior
    # stays in one place.
    from research_agent.orchestrator.synth import _load_paid_unblock_recipes

    paid_unblock_recipes = _load_paid_unblock_recipes()

    context = _build_context(
        goal=job.goal,
        findings=findings,
        sources=sources,
        synthesis=latest_synthesis,
        prior_critique=prior,
        paid_unblock_recipes=paid_unblock_recipes,
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
    actual_tier, model_name = _actual_call_tier_model(router, tier)

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
            "tier": actual_tier,
            "requested_tier": tier,
            "model": model_name,
            "should_replan": bool(output.should_replan),
            "gaps_count": len(output.gaps),
            "replan_triggered": False,
        },
    )

    if output.premature_subgoals:
        from research_agent.orchestrator import plan as _plan_mod

        _plan_mod.reopen_subgoals(job, list(output.premature_subgoals))
        emit(
            job,
            "INFO",
            "critique",
            "subgoals_reopened",
            {
                "critique_version": version,
                "subgoal_ids": list(output.premature_subgoals),
            },
        )

    return enriched


async def critique_fragments(
    job: Job,
    plan: Plan,
    *,
    router: Router,
    tier: str = DEFAULT_TIER,
) -> CritiqueOutput:
    """Critique changed or stale report fragments with bounded context."""
    from research_agent.orchestrator.synth import _select_stale_fragments

    fragments = latest_fragments(job)
    if not fragments:
        emit(
            job,
            "INFO",
            "critique",
            "fragment_critique_skipped",
            {"reason": "no_fragments"},
        )
        return CritiqueOutput(model="fragment_critique_noop")

    stale_sections = set(_select_stale_fragments(job, plan))
    rendered = load_prompt("critic", job=job, goal=job.goal)
    outputs: list[CritiqueOutput] = []
    total_cost = 0.0
    saw_cost = False
    persisted_paths: list[str] = []
    actual_model = _model_name_for(router, tier)
    actual_tier = tier

    for section_id, fragment in fragments.items():
        prior = latest_fragment_critique(job, section_id)
        skip, reason = _should_skip_fragment_critique(
            fragment=fragment,
            prior=prior,
            stale_sections=stale_sections,
        )
        if skip:
            emit(
                job,
                "INFO",
                "critique",
                "fragment_critique_skipped",
                {
                    "section_id": section_id,
                    "fragment_version": int(fragment["version"]),
                    "critique_version": int(prior["version"]) if prior else None,
                    "reason": reason,
                },
            )
            continue

        context = _build_fragment_critique_context(
            job,
            section_id,
            plan,
            fragment=fragment,
        )
        agent = Agent(
            router.model_for(tier),
            output_type=CritiqueOutput,
            system_prompt=rendered,
        )
        try:
            result = await router.call(tier, agent, context)
        except BudgetExceeded as exc:
            logger.warning(
                "fragment critique: budget exceeded on %s tier for %s: %s",
                tier,
                section_id,
                exc,
            )
            emit(
                job,
                "WARN",
                "critique",
                "warning",
                {
                    "stage": f"fragment:{section_id}",
                    "tier": tier,
                    "error": str(exc),
                    "budget_capped": True,
                },
            )
            continue

        output = result.output
        if not isinstance(output, CritiqueOutput):
            output = CritiqueOutput.model_validate(output)

        cost_raw = getattr(router.budget, "last_cost", None)
        cost_val: float | None = (
            float(cost_raw) if isinstance(cost_raw, (int, float)) else None
        )
        if cost_val is not None:
            total_cost += cost_val
            saw_cost = True
        actual_tier, actual_model = _actual_call_tier_model(router, tier)
        confidence = _fragment_issue_confidence(output)
        status = _fragment_status(output)
        payload_dict = output.model_dump(
            exclude={"version", "model", "cost_usd", "md_path"}
        )
        version = write_fragment_critique(
            job,
            section_id,
            int(fragment["version"]),
            payload=payload_dict,
            content=_render_fragment_critique_md(
                section_id,
                int(fragment["version"]),
                output,
            ),
            model=actual_model,
            cost_usd=cost_val,
            status=status,
            confidence=confidence,
            should_replan=output.should_replan,
        )
        md_rel = f"critique/fragments/{section_id}/{version:04d}.md"
        persisted_paths.append(md_rel)
        enriched = output.model_copy(
            update={
                "version": version,
                "model": actual_model,
                "cost_usd": cost_val,
                "md_path": md_rel,
            }
        )
        outputs.append(enriched)
        emit(
            job,
            "INFO",
            "critique",
            "fragment_critique_written",
            {
                "section_id": section_id,
                "fragment_version": int(fragment["version"]),
                "version": version,
                "tier": actual_tier,
                "requested_tier": tier,
                "model": actual_model,
                "status": status,
                "confidence": confidence,
                "should_replan": bool(output.should_replan),
                "gaps_count": len(output.gaps),
            },
        )

    if not outputs:
        return CritiqueOutput(model="fragment_critique_noop")

    merged = _merge_fragment_outputs(outputs)
    aggregate_cost = total_cost if saw_cost else None
    aggregate_payload = merged.model_dump(
        exclude={"version", "model", "cost_usd", "md_path"}
    )
    aggregate_version = write_critique(
        job,
        payload=aggregate_payload,
        content="# Fragment Critique Summary\n\n" + _render_critique_md(merged),
        model="fragment_critique_aggregate",
        cost_usd=aggregate_cost,
        should_replan=merged.should_replan,
    )
    aggregate_md_rel = f"critique/{aggregate_version:04d}.md"
    enriched_aggregate = merged.model_copy(
        update={
            "version": aggregate_version,
            "model": "fragment_critique_aggregate",
            "cost_usd": aggregate_cost,
            "md_path": aggregate_md_rel,
        }
    )

    emit(
        job,
        "INFO",
        "critique",
        "critique_written",
        {
            "version": aggregate_version,
            "tier": actual_tier,
            "requested_tier": tier,
            "model": actual_model,
            "mode": "fragments",
            "fragment_paths": persisted_paths,
            "should_replan": bool(merged.should_replan),
            "gaps_count": len(merged.gaps),
            "replan_triggered": False,
        },
    )

    if merged.premature_subgoals:
        from research_agent.orchestrator import plan as _plan_mod

        subgoal_ids = list(dict.fromkeys(merged.premature_subgoals))
        _plan_mod.reopen_subgoals(job, subgoal_ids)
        emit(
            job,
            "INFO",
            "critique",
            "subgoals_reopened",
            {
                "critique_version": aggregate_version,
                "subgoal_ids": subgoal_ids,
            },
        )

    return enriched_aggregate


__all__ = [
    "DEFAULT_TIER",
    "FRAGMENT_CONFIDENCE_SKIP_THRESHOLD",
    "TOP_N_FINDINGS",
    "CritiqueOutput",
    "Gap",
    "PaidOpportunity",
    "critique",
    "critique_fragments",
]
