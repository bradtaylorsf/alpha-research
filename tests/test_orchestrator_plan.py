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
    MAX_FINDINGS_FOR_REPLAN,
    MAX_PLAN_VERSIONS,
    MAX_RECENT_RESULTS_FOR_REPLAN,
    Plan,
    PlanVersionCapExceeded,
    ScopeClass,
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
ALL_SCOPE_CLASSES: tuple[str, ...] = get_args(ScopeClass)


# ---------------------------------------------------------------------------
# TaskKind enumeration
# ---------------------------------------------------------------------------


CONNECTOR_KIND_PREFIXES: tuple[str, ...] = (
    "congress",
    "fec",
    "edgar",
    "courtlistener",
    "fedregister",
    "gallica",
    "lda",
    "usaspending",
    "gdelt",
    "littlesis",
    "nonprofits",
    "opencorporates",
    "sanctions",
    "bbb",
    "licensing",
    "sos",
    "calaccess",
    "scholar",
    "linkedin",
    "commons",
    "iarchive",
    "trove",
    "wikidata",
    "wikisource",
    "openalex",
    "openlibrary",
    "persee",
    "bne",
)


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
        "cornerstone_query",
        "extract_findings",
        "summarize_source",
        "synthesize",
        "critique",
    }
    for prefix in CONNECTOR_KIND_PREFIXES:
        expected.add(f"{prefix}_search")
        expected.add(f"{prefix}_fetch")
    assert set(ALL_TASK_KINDS) == expected


@pytest.mark.parametrize("prefix", CONNECTOR_KIND_PREFIXES)
def test_task_spec_accepts_connector_search_kind(prefix: str) -> None:
    spec = TaskSpec(kind=f"{prefix}_search")  # type: ignore[arg-type]
    assert spec.kind == f"{prefix}_search"


@pytest.mark.parametrize("prefix", CONNECTOR_KIND_PREFIXES)
def test_task_spec_accepts_connector_fetch_kind(prefix: str) -> None:
    spec = TaskSpec(kind=f"{prefix}_fetch")  # type: ignore[arg-type]
    assert spec.kind == f"{prefix}_fetch"


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
# Plan.scope_class — issue #118 (scope-aware planning)
# ---------------------------------------------------------------------------


def test_scope_class_enumeration_matches_spec() -> None:
    assert set(ALL_SCOPE_CLASSES) == {
        "narrow",
        "medium",
        "broad",
        "comprehensive",
    }


def test_plan_scope_class_defaults_to_none() -> None:
    plan = _sample_plan()
    assert plan.scope_class is None


@pytest.mark.parametrize("scope", ALL_SCOPE_CLASSES)
def test_plan_accepts_each_scope_class(scope: str) -> None:
    plan = _sample_plan(scope_class=scope)
    assert plan.scope_class == scope


def test_plan_rejects_unknown_scope_class() -> None:
    with pytest.raises(ValidationError):
        _sample_plan(scope_class="huge")


def test_plan_scope_class_round_trips() -> None:
    plan = _sample_plan(scope_class="broad")
    rebuilt = Plan.model_validate(plan.model_dump())
    assert rebuilt.scope_class == "broad"
    assert rebuilt == plan


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
    # And the tactical run input embedded the prior plan + summarized
    # recent results (issue #176: full result_json no longer ships through).
    args = calls[1][2]
    assert args, "tactical_replan should pass run-input to router.call"
    payload = json.loads(args[0])
    sent = payload["recent_results"]
    assert len(sent) == 1
    assert sent[0]["task_id"] == 7
    assert sent[0]["kind"] == "web_search"
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


def test_plan_created_event_payload_includes_scope_class(
    job: Job,
    db_path: Path,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    """Issue #118: ``plan_created`` payloads must surface ``scope_class``."""
    router, _calls = router_with_spy
    _StubAgent.next_plan = _sample_plan(scope_class="broad")

    asyncio.run(initial_plan(job, router=router))

    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM events"
            " WHERE job_id = ? AND kind = 'plan_created'"
            " ORDER BY id ASC LIMIT 1",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    payload = json.loads(row["payload_json"])
    assert payload["scope_class"] == "broad"
    assert payload["version"] == 1
    assert payload["tier"] == "frontier"
    assert payload["kind"] == "initial"


def test_plan_created_event_payload_includes_scope_class_when_unset(
    job: Job,
    db_path: Path,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    """When the planner omits ``scope_class``, the event still carries the key (None)."""
    router, _calls = router_with_spy
    _StubAgent.next_plan = _sample_plan()  # default: scope_class=None

    asyncio.run(initial_plan(job, router=router))

    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM events"
            " WHERE job_id = ? AND kind = 'plan_created'"
            " ORDER BY id ASC LIMIT 1",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    payload = json.loads(row["payload_json"])
    assert "scope_class" in payload
    assert payload["scope_class"] is None


# ---------------------------------------------------------------------------
# tactical_replan payload bounding — issue #176
# ---------------------------------------------------------------------------


def _make_recent_results(n: int) -> list[dict[str, Any]]:
    """Build ``n`` synthetic completed-task entries shaped like loop output.

    Emits DESC order (task_id from ``n`` down to ``1``) to match the contract
    of ``_load_recent_task_results`` (issue #188).
    """
    return [
        {
            "task_id": i,
            "kind": "web_search",
            "payload": {"q": f"query-{i}"},
            "result": {
                "results": [
                    {"url": f"https://example.com/{i}/{j}", "title": f"hit-{i}-{j}"}
                    for j in range(5)
                ]
            },
        }
        for i in range(n, 0, -1)
    ]


def test_tactical_replan_truncates_recent_results_to_max(
    job: Job,
    db_path: Path,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    router, calls = router_with_spy
    _StubAgent.next_plan = _sample_plan()
    v1 = asyncio.run(initial_plan(job, router=router))

    _StubAgent.next_plan = _sample_plan(version=2)
    recent_results = _make_recent_results(100)
    asyncio.run(tactical_replan(job, v1, recent_results, router=router))

    args = calls[1][2]
    payload = json.loads(args[0])
    sent = payload["recent_results"]
    assert len(sent) == MAX_RECENT_RESULTS_FOR_REPLAN == 25
    # Newest 25 (DESC) must survive the slice — task_ids 100..76, not 1..25.
    assert [r["task_id"] for r in sent] == list(range(100, 75, -1))


def test_tactical_replan_emits_replan_truncated_event(
    job: Job,
    db_path: Path,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    router, _calls = router_with_spy
    _StubAgent.next_plan = _sample_plan()
    v1 = asyncio.run(initial_plan(job, router=router))

    _StubAgent.next_plan = _sample_plan(version=2)
    recent_results = _make_recent_results(100)
    asyncio.run(tactical_replan(job, v1, recent_results, router=router))

    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT level, payload_json FROM events"
            " WHERE job_id = ? AND kind = 'replan_truncated'"
            " ORDER BY id ASC",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["level"] == "WARN"
    payload = json.loads(rows[0]["payload_json"])
    assert payload["before"] == 100
    assert payload["after"] == 25
    assert payload["compressed"] is True


def test_tactical_replan_compresses_result_payloads(
    job: Job,
    db_path: Path,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    """A heavy ``result`` (1000 search hits) must collapse into a small summary."""
    router, calls = router_with_spy
    _StubAgent.next_plan = _sample_plan()
    v1 = asyncio.run(initial_plan(job, router=router))

    heavy_hits = [
        {"url": f"https://example.com/r/{j}", "title": f"hit-{j}"}
        for j in range(1000)
    ]
    recent_results = [
        {
            "task_id": 42,
            "kind": "web_search",
            "payload": {"q": "huge"},
            "result": {"results": heavy_hits, "follow_up_tasks": []},
        }
    ]

    _StubAgent.next_plan = _sample_plan(version=2)
    asyncio.run(tactical_replan(job, v1, recent_results, router=router))

    args = calls[1][2]
    payload = json.loads(args[0])
    sent = payload["recent_results"]
    assert len(sent) == 1
    entry = sent[0]
    # No raw `result` key — must be replaced by `summary`.
    assert "result" not in entry
    assert "summary" in entry
    assert entry["task_id"] == 42
    assert entry["kind"] == "web_search"
    assert entry["status"] == "ok"
    assert entry["summary"]["count"] == 1000
    # Top hits cap at 3 to keep the prompt small.
    assert len(entry["summary"]["top"]) == 3
    # Serialized payload should be tiny relative to the raw 1000-hit shape.
    raw_bytes = len(json.dumps(recent_results[0]))
    sent_bytes = len(json.dumps(entry))
    assert sent_bytes < raw_bytes / 10


def test_tactical_replan_does_not_emit_truncated_when_recent_empty(
    job: Job,
    db_path: Path,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    """Empty recent_results: skip the event so the log isn't noisy on cold starts."""
    router, _calls = router_with_spy
    _StubAgent.next_plan = _sample_plan()
    v1 = asyncio.run(initial_plan(job, router=router))

    _StubAgent.next_plan = _sample_plan(version=2)
    asyncio.run(tactical_replan(job, v1, [], router=router))

    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM events"
            " WHERE job_id = ? AND kind = 'replan_truncated'",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["c"] == 0


# ---------------------------------------------------------------------------
# tactical_replan findings drill-down — issue #179
# ---------------------------------------------------------------------------


def _make_findings(
    n: int,
    *,
    claim_template: str = "Finding {i} body text",
    starting_id: int = 1,
    source_ids_per_finding: int = 1,
) -> list[dict[str, Any]]:
    """Build ``n`` synthetic finding rows shaped like ``_load_all_findings``."""
    out: list[dict[str, Any]] = []
    for offset in range(n):
        i = starting_id + offset
        out.append(
            {
                "id": i,
                "claim": claim_template.format(i=i),
                "confidence": 0.7,
                "source_ids": [
                    f"src-{i}-{j}" for j in range(source_ids_per_finding)
                ],
                "tags": ["topic-a"],
            }
        )
    return out


def test_tactical_replan_includes_findings_in_payload(
    job: Job,
    db_path: Path,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    """Issue #179: ``tactical_replan`` must ship ``findings`` in the planner payload.

    Without findings in the payload, the planner has no way to drill into
    named claims and just re-emits umbrella queries.
    """
    router, calls = router_with_spy
    _StubAgent.next_plan = _sample_plan()
    v1 = asyncio.run(initial_plan(job, router=router))

    findings = _make_findings(
        5,
        claim_template="Schedule F implementation note {i}",
    )
    _StubAgent.next_plan = _sample_plan(version=2)
    asyncio.run(
        tactical_replan(job, v1, [], router=router, findings=findings),
    )

    args = calls[1][2]
    payload = json.loads(args[0])
    assert "findings" in payload
    sent = payload["findings"]
    assert len(sent) == 5
    for entry in sent:
        assert "id" in entry
        assert "claim" in entry
        assert "tags" in entry
        assert "source_id" in entry
    assert sent[0]["claim"] == "Schedule F implementation note 1"


def test_tactical_replan_drills_into_named_findings(
    job: Job,
    db_path: Path,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    """End-to-end wiring: a stub planner drilling into 5 named subjects must surface
    at least 3 of those names verbatim in the resulting plan's task queries.

    This exercises the orchestrator wiring (findings reach the planner; the
    planner's tasks land in the persisted plan). The actual prompt-driven
    drill-down behavior is exercised at runtime where a real LLM reads the
    drill-down rule in ``planner.md``.
    """
    router, _calls = router_with_spy
    _StubAgent.next_plan = _sample_plan()
    v1 = asyncio.run(initial_plan(job, router=router))

    named_subjects = [
        "Schedule F",
        "WOTUS rule",
        "mifepristone reversal",
        "IRS workforce",
        "FDA Title X",
    ]
    findings: list[dict[str, Any]] = []
    fid = 1
    for subject in named_subjects:
        for variant_idx in range(3):
            findings.append(
                {
                    "id": fid,
                    "claim": (
                        f"{subject} update {variant_idx}: implementation status "
                        "is partially supported by recent reporting."
                    ),
                    "confidence": 0.6,
                    "source_ids": [f"src-{fid}"],
                    "tags": [subject],
                }
            )
            fid += 1
    assert len(findings) == 15

    drilled_plan = Plan(
        version=2,
        objective="Drill into named findings.",
        scope_class="broad",
        subgoals=[
            Subgoal(id=1, description="Drill into named claims from prior findings."),
        ],
        task_template=[
            TaskSpec(
                kind="web_search",
                payload={
                    "query": "Schedule F implementation timeline OPM guidance",
                    "sub_question": "What is the Schedule F rollout timeline?",
                },
            ),
            TaskSpec(
                kind="web_search",
                payload={
                    "query": "site:federalregister.gov WOTUS rule comment period",
                    "sub_question": "Comment-period status of the WOTUS rule.",
                },
            ),
            TaskSpec(
                kind="news_search",
                payload={
                    "query": "mifepristone reversal court ruling",
                    "sub_question": "Recent court filings on the mifepristone reversal.",
                },
            ),
            TaskSpec(
                kind="web_search",
                payload={
                    "query": "IRS workforce downsizing 2026",
                    "sub_question": "IRS staffing cuts and downsizing actions.",
                },
            ),
        ],
        expected_iterations=2,
    )
    _StubAgent.next_plan = drilled_plan

    v2 = asyncio.run(
        tactical_replan(job, v1, [], router=router, findings=findings),
    )

    queries = [
        str(t.payload.get("query", "")) for t in v2.task_template
    ]
    sub_questions = [
        str(t.payload.get("sub_question", "")) for t in v2.task_template
    ]
    haystack = " || ".join(queries + sub_questions).lower()

    matched = [s for s in named_subjects if s.lower() in haystack]
    assert len(matched) >= 3, (
        f"expected at least 3 named subjects to appear verbatim in v2 task "
        f"queries/sub_questions; got {matched} from {named_subjects}"
    )


def test_tactical_replan_truncates_findings_to_max(
    job: Job,
    db_path: Path,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    """200 findings → payload caps at MAX_FINDINGS_FOR_REPLAN, event emitted."""
    router, calls = router_with_spy
    _StubAgent.next_plan = _sample_plan()
    v1 = asyncio.run(initial_plan(job, router=router))

    findings = _make_findings(200)
    _StubAgent.next_plan = _sample_plan(version=2)
    asyncio.run(
        tactical_replan(job, v1, [], router=router, findings=findings),
    )

    args = calls[1][2]
    payload = json.loads(args[0])
    sent = payload["findings"]
    assert len(sent) == MAX_FINDINGS_FOR_REPLAN == 60
    # Highest-ID findings (most recent) survive, re-ordered ascending.
    sent_ids = [e["id"] for e in sent]
    assert sent_ids == sorted(sent_ids)
    assert sent_ids[0] == 200 - MAX_FINDINGS_FOR_REPLAN + 1  # 141
    assert sent_ids[-1] == 200

    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT level, payload_json FROM events"
            " WHERE job_id = ? AND kind = 'findings_truncated'"
            " ORDER BY id ASC",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["level"] == "WARN"
    event_payload = json.loads(rows[0]["payload_json"])
    assert event_payload["before"] == 200
    assert event_payload["after"] == MAX_FINDINGS_FOR_REPLAN
    assert event_payload["compressed"] is True


def test_tactical_replan_compresses_finding_source_ids(
    job: Job,
    db_path: Path,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    """A finding with 50 source_ids ships as a single ``source_id``, not a list."""
    router, calls = router_with_spy
    _StubAgent.next_plan = _sample_plan()
    v1 = asyncio.run(initial_plan(job, router=router))

    findings = [
        {
            "id": 7,
            "claim": "Schedule F restructures civil service.",
            "confidence": 0.9,
            "source_ids": [f"src-{j}" for j in range(50)],
            "tags": ["civil-service"],
        }
    ]
    _StubAgent.next_plan = _sample_plan(version=2)
    asyncio.run(
        tactical_replan(job, v1, [], router=router, findings=findings),
    )

    args = calls[1][2]
    payload = json.loads(args[0])
    sent = payload["findings"]
    assert len(sent) == 1
    entry = sent[0]
    assert "source_ids" not in entry
    assert "source_id" in entry
    assert entry["source_id"] == "src-0"
    assert isinstance(entry["source_id"], str)


def test_tactical_replan_does_not_emit_findings_truncated_when_under_cap(
    job: Job,
    db_path: Path,
    router_with_spy: tuple[Router, list[Any]],
) -> None:
    """Under-cap findings: no truncation event, payload carries them all."""
    router, _calls = router_with_spy
    _StubAgent.next_plan = _sample_plan()
    v1 = asyncio.run(initial_plan(job, router=router))

    findings = _make_findings(MAX_FINDINGS_FOR_REPLAN)  # exactly at the cap
    _StubAgent.next_plan = _sample_plan(version=2)
    asyncio.run(
        tactical_replan(job, v1, [], router=router, findings=findings),
    )

    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM events"
            " WHERE job_id = ? AND kind = 'findings_truncated'",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["c"] == 0


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


# ---------------------------------------------------------------------------
# planner.md connector-routing guardrail (issue #174)
# ---------------------------------------------------------------------------


def test_planner_md_documents_connector_site_operators() -> None:
    """`prompts/planner.md` must teach the full connector roster (issue #174).

    Without this section, the planner emits plain `web_search` queries and
    bypasses every site:-routed connector — see the 2026-05-07 Project 2025
    overnight run that fired 8× plain web_search and 0× site:-scoped queries.
    """
    body = prompts_loader.load_prompt(
        "planner",
        goal="dummy goal — this test only inspects static prompt content",
        connector_skills_index="(none)",
        strategy_skills_index="(none)",
    )

    assert "Connector routing" in body, (
        "planner.md is missing the 'Connector routing' section that teaches "
        "the model when to use site: operators"
    )

    # One concrete site:-prefixed example per shipped connector. The
    # web_fetch host-dispatch is keyed on these domains; if the planner
    # never names them, the connectors never run.
    required_site_patterns = [
        "site:sec.gov",
        "site:courtlistener.com",
        "site:federalregister.gov",
        "site:projects.propublica.org/nonprofits",
        "site:fec.gov",
        "site:congress.gov",
        "site:lda.senate.gov",
        "site:usaspending.gov",
        "site:littlesis.org",
        "site:treasury.gov",
        "site:powersearch.sos.ca.gov",
        "site:cslb.ca.gov",
        "site:bizfileonline.sos.ca.gov",
        "site:bbb.org",
    ]
    missing = [pat for pat in required_site_patterns if pat not in body]
    assert not missing, (
        f"planner.md must include one example per connector domain — missing: {missing}"
    )

    # GDELT is the lone aggregator: no site: operator. The doc should
    # explicitly say so or the planner will assume site:gdelt-something
    # is the right move.
    assert "GDELT" in body, "planner.md should call out GDELT as the no-site: aggregator"
