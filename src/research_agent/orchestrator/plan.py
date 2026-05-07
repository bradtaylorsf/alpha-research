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
import re
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai import Agent

from research_agent.observability.events import emit
from research_agent.prompts.loader import load_prompt
from research_agent.storage import db
from research_agent.storage.jobs import _atomic_write_text
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

ScopeClass = Literal["narrow", "medium", "broad", "comprehensive"]


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
    scope_class: ScopeClass | None = None

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
            "scope_class": plan.scope_class,
        },
    )


def _enqueue_plan_tasks(job: Job, plan: Plan) -> list[int]:
    """Persist ``plan.task_template`` into the tasks queue.

    Without this the loop would pull ``None`` immediately after a fresh
    plan and exit before doing any research. Deferred import — ``storage.tasks``
    imports ``TaskSpec`` from this module, so a top-level import would cycle.
    """
    from research_agent.storage.tasks import enqueue

    if not plan.task_template:
        return []
    return enqueue(job, list(plan.task_template), plan.version)


_YAML_FENCE_RE = re.compile(r"```(?:yaml|yml)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


class PlanParseError(RuntimeError):
    """Raised when the planner's YAML output can't be parsed into a Plan.

    The raw YAML is always written to ``jobs/<id>/plan/<v>.yaml`` before
    parsing is attempted, so the operator can inspect what the model
    actually emitted (in the error message we include the path).
    """


def _extract_yaml(raw: str) -> str:
    """Return the contents of the first ```yaml fenced block, or ``raw`` itself.

    Local models occasionally forget the fence, so we tolerate "the whole
    response is YAML" as a fallback rather than failing immediately.
    """
    match = _YAML_FENCE_RE.search(raw)
    if match:
        return match.group(1).strip()
    return raw.strip()


def _persist_raw_plan_yaml(job: Job, version: int, raw: str) -> str:
    """Write the planner's raw YAML to ``jobs/<id>/plan/<v>.yaml`` (return rel path).

    The write is atomic and happens before parse/validate so a malformed
    plan still leaves an artifact on disk for forensics + future learnings.
    """
    rel = f"plan/{version:04d}.yaml"
    _atomic_write_text(job.root / rel, raw if raw.endswith("\n") else raw + "\n")
    return rel


_PLAN_KEYS = set(Plan.model_fields.keys())
_SUBGOAL_KEYS = set(Subgoal.model_fields.keys())
_TASKSPEC_KEYS = set(TaskSpec.model_fields.keys())


def _strip_unknown_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Drop keys not declared on Plan / Subgoal / TaskSpec from a parsed dict.

    The Pydantic models use ``extra="forbid"`` because that's the right
    contract for code-internal callers (catches typos at module boundaries).
    But the planner LLM occasionally adds *helpful* extras — e.g. gemma
    once emitted a ``describe`` key alongside ``description`` on a subgoal.
    Strict validation killed the whole plan over a single ignored field.

    This helper prunes unknown keys at the LLM trust boundary so models
    stay strict for internal use while LLM output stays tolerant.
    """
    if not isinstance(data, dict):
        return data
    pruned: dict[str, Any] = {k: v for k, v in data.items() if k in _PLAN_KEYS}

    sgs = pruned.get("subgoals")
    if isinstance(sgs, list):
        pruned["subgoals"] = [
            {k: v for k, v in sg.items() if k in _SUBGOAL_KEYS}
            if isinstance(sg, dict) else sg
            for sg in sgs
        ]
    tasks = pruned.get("task_template")
    if isinstance(tasks, list):
        pruned["task_template"] = [
            {k: v for k, v in t.items() if k in _TASKSPEC_KEYS}
            if isinstance(t, dict) else t
            for t in tasks
        ]
    return pruned


def _parse_plan_yaml(raw: str, *, version: int, raw_path: str) -> Plan:
    """Parse + validate a YAML plan; force ``version`` on the result.

    ``raw_path`` is included in error messages so the operator can open
    the on-disk artifact when validation fails. Unknown keys emitted by
    the LLM are silently dropped via :func:`_strip_unknown_keys` so the
    parser tolerates planner drift without forfeiting strict validation
    for code-internal callers.
    """
    yaml_text = _extract_yaml(raw)
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise PlanParseError(
            f"planner YAML failed to parse ({raw_path}): {e}"
        ) from e
    if not isinstance(data, dict):
        raise PlanParseError(
            f"planner YAML root must be a mapping ({raw_path}); got {type(data).__name__}"
        )
    data["version"] = version
    data = _strip_unknown_keys(data)
    try:
        return Plan.model_validate(data)
    except ValidationError as e:
        raise PlanParseError(
            f"planner YAML failed Plan validation ({raw_path}): {e}"
        ) from e


async def _run_planner_for_yaml(
    job: Job,
    *,
    tier: str,
    router: Router,
    user_message: str,
) -> str:
    """Call the configured tier with output_type=str and return raw text.

    Local models choke on tool-call structured output for our nested
    Plan schema; YAML-on-disk is the resilient path.
    """
    rendered = load_prompt("planner", job=job, goal=job.goal)
    agent = Agent(router.model_for(tier), output_type=str, system_prompt=rendered)
    result = await router.call(tier, agent, user_message)
    output = result.output
    if not isinstance(output, str):
        output = str(output)
    return output


async def initial_plan(job: Job, *, router: Router) -> Plan:
    """Build the v1 plan for a fresh job.

    Asks the ``frontier`` tier for a YAML plan, writes the raw response to
    ``jobs/<id>/plan/0001.yaml``, parses + validates against :class:`Plan`,
    persists the validated structure via :func:`write_plan`, enqueues the
    task template, and emits ``plan_created``.
    """
    _assert_under_cap(job)
    raw = await _run_planner_for_yaml(
        job, tier="frontier", router=router, user_message=job.goal
    )
    raw_path = _persist_raw_plan_yaml(job, version=1, raw=raw)
    plan = _parse_plan_yaml(raw, version=1, raw_path=raw_path)
    write_plan(job, plan.model_dump())
    _enqueue_plan_tasks(job, plan)
    _emit_plan_created(job, plan, tier="frontier", kind="initial")
    return plan


async def tactical_replan(
    job: Job,
    plan: Plan,
    recent_results: list[dict[str, Any]],
    *,
    router: Router,
    findings: list[dict[str, Any]] | None = None,
    synthesis_md: str | None = None,
) -> Plan:
    """Run a small in-loop replan on the local ``general`` tier.

    The prior plan + recent task results are serialized into the run-prompt
    payload so the planner can adjust without a full cloud rewrite. Optional
    ``findings`` and ``synthesis_md`` carry the wider research state so a
    drain-driven replan (issue #117) can pivot on what's been learned, not
    just on what just ran. The returned plan's version is set to
    ``plan.version + 1`` and persisted.
    """
    _assert_under_cap(job)
    next_version = plan.version + 1
    payload: dict[str, Any] = {
        "prior_plan": plan.model_dump(),
        "recent_results": recent_results,
    }
    if findings is not None:
        payload["findings"] = findings
    if synthesis_md is not None:
        payload["synthesis_md"] = synthesis_md
    context = json.dumps(payload, sort_keys=True, default=str)
    raw = await _run_planner_for_yaml(
        job, tier="general", router=router, user_message=context
    )
    raw_path = _persist_raw_plan_yaml(job, version=next_version, raw=raw)
    new_plan = _parse_plan_yaml(raw, version=next_version, raw_path=raw_path)
    write_plan(job, new_plan.model_dump())
    _enqueue_plan_tasks(job, new_plan)
    _emit_plan_created(job, new_plan, tier="general", kind="tactical_replan")
    return new_plan


def _load_latest_plan(job: Job) -> Plan:
    """Read the highest-version ``plans`` row for ``job`` and return it as :class:`Plan`."""
    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM plans WHERE job_id = ? ORDER BY version DESC LIMIT 1",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise RuntimeError(f"no plan persisted for job {job.id!r}")
    return Plan.model_validate_json(row["payload_json"])


def update_subgoal_done(job: Job, status_map: dict[int, str]) -> Plan:
    """Apply a synthesizer-emitted ``subgoal_status`` map to the latest plan.

    For each subgoal whose id appears in ``status_map``, set ``done=True``
    when the status is ``confirmed`` or ``refuted`` and ``done=False`` when
    it is ``inconclusive``. Subgoals whose id is not in the map are left
    untouched. A new plan version is persisted under :data:`MAX_PLAN_VERSIONS`.

    When ``status_map`` would not actually flip any subgoal's ``done`` flag
    (the synthesizer reported the same statuses again on a later heuristic
    fire), this is a no-op: no version bump, no write, no emit. The synth
    heuristic fires every 25 tasks — bumping unconditionally would burn
    through the 200-version cap on any moderately long run and break the
    very goal_complete termination this module exists to enable.
    """
    plan = _load_latest_plan(job)

    closing = {"confirmed", "refuted"}
    prior_done: dict[int, bool] = {sg.id: sg.done for sg in plan.subgoals}
    new_done_by_id: dict[int, bool] = {}
    for sg in plan.subgoals:
        if sg.id in status_map:
            new_done_by_id[sg.id] = status_map[sg.id] in closing

    if not any(new_done_by_id[sid] != prior_done[sid] for sid in new_done_by_id):
        return plan

    _assert_under_cap(job)
    next_version = plan.version + 1
    new_subgoals: list[Subgoal] = [
        sg.model_copy(update={"done": new_done_by_id[sg.id]})
        if sg.id in new_done_by_id
        else sg
        for sg in plan.subgoals
    ]

    new_plan = plan.model_copy(update={"version": next_version, "subgoals": new_subgoals})
    write_plan(job, new_plan.model_dump())

    closed = [
        sg.id
        for sg in new_plan.subgoals
        if sg.id in status_map and sg.done and not prior_done.get(sg.id, False)
    ]
    reopened = [
        sg.id
        for sg in new_plan.subgoals
        if sg.id in status_map and not sg.done and prior_done.get(sg.id, False)
    ]
    inconclusive = [
        sg.id for sg in new_plan.subgoals if status_map.get(sg.id) == "inconclusive"
    ]

    emit(
        job,
        "INFO",
        "planner",
        "plan_subgoals_updated",
        {
            "version": new_plan.version,
            "closed": closed,
            "reopened": reopened,
            "inconclusive": inconclusive,
        },
    )
    return new_plan


def reopen_subgoals(job: Job, ids: list[int]) -> Plan:
    """Flip ``done=False`` for matching subgoal ids and persist a new plan version.

    Used by the critique pass when synthesis closed subgoals prematurely —
    the critic flags them and we reopen them so the loop keeps working.
    No-op (no version bump) when every targeted subgoal is already
    ``done=False``, mirroring :func:`update_subgoal_done`.
    """
    plan = _load_latest_plan(job)

    target_ids = set(ids)
    if not any(sg.done for sg in plan.subgoals if sg.id in target_ids):
        return plan

    _assert_under_cap(job)
    next_version = plan.version + 1
    new_subgoals = [
        sg.model_copy(update={"done": False}) if sg.id in target_ids else sg
        for sg in plan.subgoals
    ]
    new_plan = plan.model_copy(update={"version": next_version, "subgoals": new_subgoals})
    write_plan(job, new_plan.model_dump())

    reopened = [sg.id for sg in new_plan.subgoals if sg.id in target_ids]
    emit(
        job,
        "INFO",
        "planner",
        "plan_subgoals_reopened",
        {"version": new_plan.version, "reopened": reopened},
    )
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
    next_version = plan.version + 1
    context = json.dumps(
        {
            "prior_plan": plan.model_dump(),
            "critique": critique,
        },
        sort_keys=True,
        default=str,
    )
    raw = await _run_planner_for_yaml(
        job, tier="frontier", router=router, user_message=context
    )
    raw_path = _persist_raw_plan_yaml(job, version=next_version, raw=raw)
    new_plan = _parse_plan_yaml(raw, version=next_version, raw_path=raw_path)
    write_plan(job, new_plan.model_dump())
    _enqueue_plan_tasks(job, new_plan)
    _emit_plan_created(job, new_plan, tier="frontier", kind="cloud_replan")
    return new_plan


__all__ = [
    "MAX_PLAN_VERSIONS",
    "Plan",
    "PlanParseError",
    "PlanVersionCapExceeded",
    "ScopeClass",
    "Subgoal",
    "TaskKind",
    "TaskSpec",
    "cloud_replan",
    "initial_plan",
    "reopen_subgoals",
    "tactical_replan",
    "update_subgoal_done",
]
