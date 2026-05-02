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
import sqlite3
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
    findings: list[dict[str, Any]],
    sources: dict[int, dict[str, Any]],
    prior: str | None,
    critique: str | None,
    final: bool = False,
) -> str:
    payload: dict[str, Any] = {
        "goal": goal,
        "findings": findings,
        "sources": {str(k): v for k, v in sources.items()},
        "prior_synthesis": prior,
        "critique": critique,
    }
    if final:
        payload["final"] = True
    return json.dumps(payload, sort_keys=True, default=str)


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

    context = _build_context(
        goal=job.goal,
        findings=findings,
        sources=sources,
        prior=prior,
        critique=critique,
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

    version = write_synthesis(job, content, model=model_name, cost_usd=cost_val)
    synth_md = (job.root / f"synthesis/{version:04d}.md").read_text(encoding="utf-8")
    report_path = write_report(job, synth_md)

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
        content=content,
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


__all__ = [
    "FINAL_TOP_N",
    "SynthesisOutput",
    "TOP_N_FINDINGS",
    "final_synthesis",
    "synthesize",
]
