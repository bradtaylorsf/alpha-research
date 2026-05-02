"""Plan + Task Pydantic models and planner orchestration.

The ``Plan`` is the versioned, structured document the planner agent emits
each iteration of the research loop. It captures the objective, the list of
subgoals (which drive completion), a template of tasks to enqueue, and the
expected number of loop iterations.

A ``TaskSpec`` is the typed shape that the planner emits and that
:mod:`research_agent.storage.tasks` persists into the ``tasks`` queue. It is
deliberately a *spec* — not the queue row itself — because the row also
carries lifecycle state (``status``, ``retry_count``, timestamps, the
parent task pointer) that the planner does not own.

All models use ``extra='forbid'`` so a typo in a planner prompt surfaces as
a validation error at the boundary rather than silently dropping a field.

This module also exposes the three planner entry points: :func:`initial_plan`
(cloud / ``frontier`` tier — first plan from intake), :func:`tactical_replan`
(local / ``general`` tier — small in-loop adjustments), and
:func:`cloud_replan` (cloud / ``frontier`` tier — big rewrites driven by a
critique). Each persists the new plan via :func:`write_plan` and emits a
``plan_created`` event. A hard cap of :data:`MAX_PLAN_VERSIONS` versions per
job (per implementation guide §6.3 anti-infinite-loop) guards against runaway
replanning.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent

from research_agent.observability.events import emit
from research_agent.prompts.loader import load_prompt
from research_agent.storage import db
from research_agent.storage.markdown import write_plan

if TYPE_CHECKING:
    from research_agent.llm.router import Router
    from research_agent.storage.jobs import Job

TaskKind = Literal[
    "web_search",
    "web_fetch",
    "arxiv_search",
    "arxiv_fetch",
    "github_search",
    "github_fetch",
    "news_search",
    "reddit_search",
    "local_corpus_query",
    "extract_findings",
    "summarize_source",
    "synthesize",
    "critique",
]


class Subgoal(BaseModel):
    """A single subgoal within a Plan. ``done=True`` retires it from the loop."""

    model_config = ConfigDict(extra="forbid")

    id: int
    description: str = Field(min_length=1)
    done: bool = False


class TaskSpec(BaseModel):
    """A planner-emitted task to enqueue.

    ``depends_on`` references other ``TaskSpec`` entries by their *index*
    inside the same ``Plan.task_template`` list — this is intentionally
    decoupled from DB rowids so a plan can be validated before any rows
    exist.
    """

    model_config = ConfigDict(extra="forbid")

    kind: TaskKind
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    depends_on: list[int] = Field(default_factory=list)


class Plan(BaseModel):
    """A versioned planning document.

    ``is_complete()`` is the single source of truth for "should the loop
    stop?" — it returns True only when at least one subgoal exists and all
    are marked done. An empty ``subgoals`` list returns False so a planner
    that emits no subgoals never accidentally terminates the loop.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    objective: str = Field(min_length=1)
    subgoals: list[Subgoal]
    task_template: list[TaskSpec]
    expected_iterations: int = Field(ge=1)

    def is_complete(self) -> bool:
        if not self.subgoals:
            return False
        return all(sg.done for sg in self.subgoals)


MAX_PLAN_VERSIONS = 200


class PlanVersionCapExceeded(RuntimeError):
    """Raised when a job has hit the §6.3 hard cap of plan versions.

    The cap exists to short-circuit a planner that is stuck rewriting itself
    instead of making progress — without it a tactical-replan loop could
    silently burn local-tier time forever.
    """


def _plan_count(job: Job) -> int:
    """Count persisted plan rows for ``job`` via the cross-job index."""
    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM plans WHERE job_id = ?",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["c"]) if row is not None else 0


def _assert_under_cap(job: Job) -> None:
    if _plan_count(job) >= MAX_PLAN_VERSIONS:
        raise PlanVersionCapExceeded(
            f"plan version cap of {MAX_PLAN_VERSIONS} reached for job {job.id!r}"
        )


def _emit_plan_created(job: Job, plan: Plan, *, tier: str, kind: str) -> None:
    emit(
        job,
        "INFO",
        "planner",
        "plan_created",
        {
            "version": plan.version,
            "tier": tier,
            "kind": kind,
            "subgoals": len(plan.subgoals),
            "tasks": len(plan.task_template),
        },
    )


async def initial_plan(job: Job, *, router: Router) -> Plan:
    """Build the v1 plan for a fresh job using the cloud ``frontier`` tier.

    Renders the ``planner.md`` system prompt with the job goal, runs a
    Pydantic AI :class:`Agent` with ``output_type=Plan`` against the frontier
    model, forces ``version = 1`` on the structured output, persists via
    :func:`write_plan`, and emits ``plan_created``.
    """
    _assert_under_cap(job)
    rendered = load_prompt("planner", job=job, goal=job.goal)
    agent = Agent(router.model_for("frontier"), output_type=Plan, system_prompt=rendered)
    result = await router.call("frontier", agent, job.goal)
    plan: Plan = result.output
    plan = plan.model_copy(update={"version": 1})
    write_plan(job, plan.model_dump())
    _emit_plan_created(job, plan, tier="frontier", kind="initial")
    return plan


async def tactical_replan(
    job: Job,
    plan: Plan,
    recent_results: list[dict[str, Any]],
    *,
    router: Router,
) -> Plan:
    """Run a small in-loop replan on the local ``general`` tier.

    The prior plan + recent task results are serialized into the run-prompt
    payload so the planner can adjust without a full cloud rewrite. The
    returned plan's version is set to ``plan.version + 1`` and persisted.
    """
    _assert_under_cap(job)
    rendered = load_prompt("planner", job=job, goal=job.goal)
    agent = Agent(router.model_for("general"), output_type=Plan, system_prompt=rendered)
    context = json.dumps(
        {
            "prior_plan": plan.model_dump(),
            "recent_results": recent_results,
        },
        sort_keys=True,
        default=str,
    )
    result = await router.call("general", agent, context)
    new_plan: Plan = result.output
    new_plan = new_plan.model_copy(update={"version": plan.version + 1})
    write_plan(job, new_plan.model_dump())
    _emit_plan_created(job, new_plan, tier="general", kind="tactical_replan")
    return new_plan


async def cloud_replan(
    job: Job,
    plan: Plan,
    critique: str,
    *,
    router: Router,
) -> Plan:
    """Run a major plan rewrite on the cloud ``frontier`` tier.

    Used when a critique flags structural gaps that a local tactical replan
    can't address. Increments the plan version, persists, emits.
    """
    _assert_under_cap(job)
    rendered = load_prompt("planner", job=job, goal=job.goal)
    agent = Agent(router.model_for("frontier"), output_type=Plan, system_prompt=rendered)
    context = json.dumps(
        {
            "prior_plan": plan.model_dump(),
            "critique": critique,
        },
        sort_keys=True,
        default=str,
    )
    result = await router.call("frontier", agent, context)
    new_plan: Plan = result.output
    new_plan = new_plan.model_copy(update={"version": plan.version + 1})
    write_plan(job, new_plan.model_dump())
    _emit_plan_created(job, new_plan, tier="frontier", kind="cloud_replan")
    return new_plan


__all__ = [
    "MAX_PLAN_VERSIONS",
    "Plan",
    "PlanVersionCapExceeded",
    "Subgoal",
    "TaskKind",
    "TaskSpec",
    "cloud_replan",
    "initial_plan",
    "tactical_replan",
]
