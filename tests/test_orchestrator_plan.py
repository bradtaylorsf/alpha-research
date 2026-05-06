"""Tests for `research_agent.orchestrator.plan`."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from pydantic import ValidationError

from research_agent.llm.budgets import BudgetTracker
from research_agent.llm.router import Router, load_models_config
from research_agent.orchestrator import plan as plan_module
from research_agent.orchestrator.plan import (
    MAX_PLAN_VERSIONS,
    Plan,
    PlanVersionCapExceeded,
    Subgoal,
    TaskKind,
    TaskSpec,
    cloud_replan,
    initial_plan,
    tactical_replan,
)
from research_agent.prompts import loader as prompts_loader
from research_agent.storage import db
from research_agent.storage.jobs import Job

ALL_TASK_KINDS: tuple[str, ...] = get_args(TaskKind)


# ---------------------------------------------------------------------------
# TaskKind enumeration
# ---------------------------------------------------------------------------


def test_task_kind_covers_expected_set() -> None:
    expected = {
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
    }
    assert set(ALL_TASK_KINDS) == expected


@pytest.mark.parametrize("kind", ALL_TASK_KINDS)
def test_task_spec_accepts_each_enumerated_kind(kind: str) -> None:
    spec = TaskSpec(kind=kind)  # type: ignore[arg-type]
    assert spec.kind == kind


def test_task_spec_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        TaskSpec(kind="not_a_real_kind")  # type: ignore[arg-type]


def test_task_spec_defaults() -> None:
    spec = TaskSpec(kind="web_search")
    assert spec.payload == {}
    assert spec.priority == 0
    assert spec.depends_on == []


def test_task_spec_round_trip() -> None:
    spec = TaskSpec(
        kind="web_fetch",
        payload={"url": "https://example.com"},
        priority=5,
        depends_on=[1, 2],
    )
    dumped = spec.model_dump()
    again = TaskSpec.model_validate(dumped)
    assert again == spec


def test_task_spec_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(
            {"kind": "web_search", "unknown": "boom"},
        )


# ---------------------------------------------------------------------------
# Subgoal
# ---------------------------------------------------------------------------


def test_subgoal_requires_non_empty_description() -> None:
    with pytest.raises(ValidationError):
        Subgoal(id=1, description="")


def test_subgoal_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError):
        Subgoal.model_validate({"id": 1, "description": "do thing", "extra": True})


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def _sample_plan(**overrides) -> Plan:
    base = {
        "version": 1,
        "objective": "Investigate the target",
        "subgoals": [
            Subgoal(id=1, description="Gather background"),
            Subgoal(id=2, description="Cross-reference sources"),
        ],
        "task_template": [
            TaskSpec(kind="web_search", payload={"q": "target"}),
            TaskSpec(kind="arxiv_search", payload={"q": "target"}, depends_on=[0]),
        ],
        "expected_iterations": 3,
    }
    base.update(overrides)
    return Plan(**base)


def test_plan_round_trip_through_model_dump_and_validate() -> None:
    plan = _sample_plan()
    dumped = plan.model_dump()
    rebuilt = Plan.model_validate(dumped)
    assert rebuilt == plan


def test_plan_is_complete_false_when_subgoals_empty() -> None:
    plan = _sample_plan(subgoals=[])
    assert plan.is_complete() is False


def test_plan_is_complete_false_when_any_not_done() -> None:
    plan = _sample_plan(
        subgoals=[
            Subgoal(id=1, description="a", done=True),
            Subgoal(id=2, description="b", done=False),
        ]
    )
    assert plan.is_complete() is False


def test_plan_is_complete_true_when_all_done() -> None:
    plan = _sample_plan(
        subgoals=[
            Subgoal(id=1, description="a", done=True),
            Subgoal(id=2, description="b", done=True),
        ]
    )
    assert plan.is_complete() is True


def test_plan_rejects_version_below_one() -> None:
    with pytest.raises(ValidationError):
        _sample_plan(version=0)


def test_plan_rejects_empty_objective() -> None:
    with pytest.raises(ValidationError):
        _sample_plan(objective="")


def test_plan_rejects_expected_iterations_below_one() -> None:
    with pytest.raises(ValidationError):
        _sample_plan(expected_iterations=0)


def test_plan_forbids_extra_keys() -> None:
    payload = _sample_plan().model_dump()
    payload["surprise"] = "boom"
    with pytest.raises(ValidationError):
        Plan.model_validate(payload)


# ---------------------------------------------------------------------------
# Planner orchestration: initial_plan / tactical_replan / cloud_replan
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_MODELS_YAML = REPO_ROOT / "config" / "models.yaml"


class _StubUsage:
    """Mimics a Pydantic AI usage object for ``_extract_usage`` to consume."""

    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0


class _StubResult:
    """Mimics ``AgentRunResult`` — exposes ``output``/``usage()``/``finish_reason``.

    The planner now consumes ``output: str`` (raw YAML), not a structured
    :class:`Plan`; tests serialize a Plan fixture to YAML and hand the
    string back here.
    """

    def __init__(self, output: str) -> None:
        self.output = output
        self.finish_reason = "stop"

    def usage(self) -> _StubUsage:
        return _StubUsage()


def _plan_to_yaml(plan: Plan) -> str:
    """Render a Plan as the fenced YAML block the live model would emit."""
    body = yaml.safe_dump(plan.model_dump(), sort_keys=False)
    return f"```yaml\n{body}```"


class _StubAgent:
    """Stub stand-in for :class:`pydantic_ai.Agent`.

    Captures construction kwargs (so tests can verify ``output_type`` and the
    rendered ``system_prompt``) and returns a YAML serialization of the
    configured :class:`Plan` from :meth:`run`. Each test resets the
    class-level ``next_plan`` before triggering the planner.
    """

    instances: list[_StubAgent] = []
    next_plan: Plan | None = None

    def __init__(
        self,
        model: Any,
        *,
        output_type: Any = None,
        system_prompt: str | None = None,
        **extra: Any,
    ) -> None:
        self.model = model
        self.output_type = output_type
        self.system_prompt = system_prompt
        self.extra = extra
        self.run_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        _StubAgent.instances.append(self)

    async def run(self, *args: Any, **kwargs: Any) -> _StubResult:
        self.run_calls.append((args, kwargs))
        plan = _StubAgent.next_plan
        assert plan is not None, "test forgot to set _StubAgent.next_plan"
        return _StubResult(output=_plan_to_yaml(plan))


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    """Per-test reset of the prompt loader cache to avoid cross-test bleed."""
    prompts_loader.clear_cache()
    yield
    prompts_loader.clear_cache()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.sqlite"
    db.migrate(path=path).close()
    return path


@pytest.fixture
def jobs_root(tmp_path: Path) -> Path:
    return tmp_path / "jobs"


@pytest.fixture
def job(jobs_root: Path, db_path: Path) -> Job:
    return Job.create(
        {"goal": "Investigate planner module"},
        jobs_root=jobs_root,
        db_path=db_path,
    )


@pytest.fixture
def router_with_spy(
    monkeypatch: pytest.MonkeyPatch,
    job: Job,
    db_path: Path,
) -> tuple[Router, list[tuple[str, Any, tuple[Any, ...], dict[str, Any]]]]:
    """Build a Router whose ``call`` is replaced with a spy that drives the stub.

    The spy records ``(tier, agent, args, kwargs)`` on every call and returns
    whatever the stub agent produces — this avoids any HTTP traffic while
    still exercising the planner's tier-selection contract.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-planner")
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)

    cfg = load_models_config(SHIPPED_MODELS_YAML)
    budget = BudgetTracker(job.id, cap_usd=None, db_path=db_path)
    router = Router(cfg, budget, job=job, db_path=db_path)

    calls: list[tuple[str, Any, tuple[Any, ...], dict[str, Any]]] = []

    async def _spy_call(tier: str, agent: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append((tier, agent, args, kwargs))
        return await agent.run(*args, **kwargs)

    monkeypatch.setattr(router, "call", _spy_call)
    monkeypatch.setattr(plan_module, "Agent", _StubAgent)
    _StubAgent.instances = []
    _StubAgent.next_plan = None
    return router, calls


def _read_plan_rows(db_path: Path, job_id: str) -> list[dict[str, Any]]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT version, payload_json, created_at FROM plans"
            " WHERE job_id = ? ORDER BY version ASC",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _read_event_kinds(db_path: Path, job_id: str) -> list[str]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT kind FROM events WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    return [r["kind"] for r in rows]


def test_initial_plan_writes_v1_row_and_emits_event(
    job: Job,
    db_path: Path,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    router, calls = router_with_spy
    # Stub returns a plan claiming version=99; initial_plan must force it to 1.
    _StubAgent.next_plan = _sample_plan(version=99)

    result = asyncio.run(initial_plan(job, router=router))

    assert result.version == 1
    assert calls and calls[0][0] == "frontier"

    rows = _read_plan_rows(db_path, job.id)
    assert len(rows) == 1
    assert rows[0]["version"] == 1
    persisted = json.loads(rows[0]["payload_json"])
    assert persisted["version"] == 1
    assert persisted["objective"] == result.objective

    assert "plan_created" in _read_event_kinds(db_path, job.id)


def test_initial_plan_renders_planner_prompt_with_goal(
    job: Job,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    _router, _calls = router_with_spy
    _StubAgent.next_plan = _sample_plan()

    asyncio.run(initial_plan(job, router=_router))

    assert _StubAgent.instances, "Agent was never constructed"
    constructed = _StubAgent.instances[0]
    # YAML-on-disk path: planner now requests raw text from the model and
    # parses YAML ourselves, so output_type is str (not Plan).
    assert constructed.output_type is str
    # The planner prompt template contains the goal literal once rendered.
    assert constructed.system_prompt is not None
    assert job.goal in constructed.system_prompt


def test_tactical_replan_increments_version_and_uses_general_tier(
    job: Job,
    db_path: Path,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    router, calls = router_with_spy
    _StubAgent.next_plan = _sample_plan()
    v1 = asyncio.run(initial_plan(job, router=router))
    assert v1.version == 1

    # Stub returns whatever version it likes — tactical_replan must overwrite
    # to ``prior + 1`` regardless.
    _StubAgent.next_plan = _sample_plan(version=42)
    recent_results = [{"task_id": 7, "kind": "web_search", "status": "done"}]
    v2 = asyncio.run(
        tactical_replan(job, v1, recent_results, router=router),
    )

    assert v2.version == 2
    rows = _read_plan_rows(db_path, job.id)
    assert [r["version"] for r in rows] == [1, 2]

    # Spy captured the tier on the second call.
    assert [c[0] for c in calls] == ["frontier", "general"]
    # And the tactical run input embedded the prior plan + recent results.
    args = calls[1][2]
    assert args, "tactical_replan should pass run-input to router.call"
    payload = json.loads(args[0])
    assert payload["recent_results"] == recent_results
    assert payload["prior_plan"]["version"] == 1


def test_cloud_replan_increments_version_and_routes_through_frontier(
    job: Job,
    db_path: Path,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    router, calls = router_with_spy
    _StubAgent.next_plan = _sample_plan()
    v1 = asyncio.run(initial_plan(job, router=router))

    _StubAgent.next_plan = _sample_plan(version=7)
    critique = "Plan misses contradicting evidence; broaden subgoal 2."
    v2 = asyncio.run(cloud_replan(job, v1, critique, router=router))

    assert v2.version == 2
    assert [c[0] for c in calls] == ["frontier", "frontier"]

    args = calls[1][2]
    payload = json.loads(args[0])
    assert payload["critique"] == critique
    assert payload["prior_plan"]["version"] == 1

    rows = _read_plan_rows(db_path, job.id)
    assert [r["version"] for r in rows] == [1, 2]


def test_max_plan_versions_cap_raises_when_full(
    job: Job,
    db_path: Path,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    router, _calls = router_with_spy
    # Pre-populate 200 plan rows directly so the cap check fires before the
    # planner ever calls into the stub agent.
    now = int(time.time())
    conn = db.connect(db_path)
    try:
        with conn:
            for v in range(1, MAX_PLAN_VERSIONS + 1):
                conn.execute(
                    "INSERT INTO plans (job_id, version, payload_json, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (job.id, v, json.dumps({"version": v}), now),
                )
    finally:
        conn.close()

    _StubAgent.next_plan = _sample_plan()

    with pytest.raises(PlanVersionCapExceeded):
        asyncio.run(initial_plan(job, router=router))

    prior = _sample_plan(version=MAX_PLAN_VERSIONS)
    with pytest.raises(PlanVersionCapExceeded):
        asyncio.run(tactical_replan(job, prior, [], router=router))
    with pytest.raises(PlanVersionCapExceeded):
        asyncio.run(cloud_replan(job, prior, "x", router=router))


def test_persisted_plan_round_trips_via_db(
    job: Job,
    db_path: Path,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    """The stored ``payload_json`` parses back into the same Plan we returned."""
    router, _calls = router_with_spy
    stub = _sample_plan(version=99)
    _StubAgent.next_plan = stub

    returned = asyncio.run(initial_plan(job, router=router))

    rows = _read_plan_rows(db_path, job.id)
    assert len(rows) == 1
    rebuilt = Plan.model_validate(json.loads(rows[0]["payload_json"]))
    # Version is forced to 1; everything else matches the stub payload.
    assert rebuilt == returned
    expected = stub.model_copy(update={"version": 1})
    assert rebuilt == expected
