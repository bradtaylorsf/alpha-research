"""Tests for ``research_agent.orchestrator.critique``."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from research_agent.llm.budgets import BudgetExceeded
from research_agent.orchestrator import loop as loop_module
from research_agent.orchestrator.critique import (
    DEFAULT_TIER,
    CritiqueOutput,
    Gap,
    critique,
)
from research_agent.orchestrator.plan import Plan, Subgoal, TaskSpec
from research_agent.prompts import loader as prompts_loader
from research_agent.storage import db
from research_agent.storage.jobs import Job
from research_agent.storage.markdown import write_finding, write_plan, write_synthesis
from research_agent.storage.sources import write_source

# `from research_agent.orchestrator import critique` would resolve to the
# re-exported function; grab the actual submodule via importlib so tests
# can monkeypatch attributes on it.
critique_module = importlib.import_module("research_agent.orchestrator.critique")

# ---------------------------------------------------------------------------
# Stub Router / Budget / Agent
# ---------------------------------------------------------------------------


_DEFAULT_TIERS: dict[str, dict[str, Any]] = {
    "frontier": {"provider": "openrouter", "model": "anthropic/claude-opus-4-7"},
    "frontier_alt": {"provider": "openrouter", "model": "moonshotai/kimi-k2"},
    "frontier_speed": {"provider": "openrouter", "model": "anthropic/claude-haiku-4-5"},
}


class _StubBudget:
    def __init__(self, last_cost: float = 0.0091) -> None:
        self.last_cost = last_cost


class _StubAgent:
    instances: list[_StubAgent] = []

    def __init__(
        self,
        model: Any,
        *,
        output_type: Any = None,
        system_prompt: str | None = None,
    ) -> None:
        self.model = model
        self.output_type = output_type
        self.system_prompt = system_prompt
        _StubAgent.instances.append(self)


class _StubRouter:
    """Stub Router compatible with :func:`critique`'s call surface."""

    def __init__(
        self,
        *,
        output: CritiqueOutput | None = None,
        side_effect: dict[str, list[Exception | None]] | None = None,
        tiers: dict[str, dict[str, Any]] | None = None,
        budget: _StubBudget | None = None,
    ) -> None:
        self.output = output or CritiqueOutput(
            gaps=[Gap(description="missing analysis of X", severity="warn")],
            unsupported_claims=["claim about Y has no source"],
            suggested_subgoals=["dig into X"],
            confidence_concerns=["low coverage of Z"],
            should_replan=False,
        )
        self.side_effect: dict[str, list[Exception | None]] = side_effect or {}
        self.tiers: dict[str, dict[str, Any]] = tiers or dict(_DEFAULT_TIERS)
        self.budget = budget or _StubBudget()
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def model_for(self, tier: str) -> Any:
        return SimpleNamespace(tier=tier)

    async def call(self, tier: str, agent: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((tier, args, kwargs))
        effects = self.side_effect.get(tier) or []
        if effects:
            err = effects.pop(0)
            if err is not None:
                raise err
        return SimpleNamespace(output=self.output)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    _StubAgent.instances = []
    monkeypatch.setattr(critique_module, "Agent", _StubAgent)


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
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
        {"goal": "Investigate Widget Co critique"},
        jobs_root=jobs_root,
        db_path=db_path,
    )


@pytest.fixture
def plan(job: Job) -> Plan:
    p = Plan(
        version=1,
        objective="Investigate the target",
        subgoals=[Subgoal(id=1, description="Gather", done=False)],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=3,
    )
    write_plan(job, p.model_dump())
    return p


def _seed_source(job: Job, url: str, content: str) -> int:
    return write_source(
        job,
        url=url,
        title=f"Title {url}",
        raw_content=content,
        kind="web",
    )


def _seed_findings(
    job: Job,
    confidences: list[float],
    *,
    base_claim: str = "claim",
) -> tuple[list[int], list[int]]:
    source_ids: list[int] = []
    finding_ids: list[int] = []
    for i, conf in enumerate(confidences):
        sid = _seed_source(job, f"https://example.com/{i}", f"content body {i}")
        source_ids.append(sid)
        fid = write_finding(job, f"{base_claim} {i}", conf, [sid])
        finding_ids.append(fid)
    return source_ids, finding_ids


def _read_critique_rows(db_path: Path, job_id: str) -> list[dict[str, Any]]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT version, md_path, model, cost_usd, should_replan, payload_json"
            " FROM critiques WHERE job_id = ? ORDER BY version ASC",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _read_event_rows(db_path: Path, job_id: str) -> list[dict[str, Any]]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT level, kind, payload_json FROM events WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_critique_writes_md_json_and_db_row(job: Job, db_path: Path, plan: Plan) -> None:
    _seed_findings(job, [0.9, 0.5])
    router = _StubRouter()

    out = asyncio.run(critique(job, plan, "# Synthesis\n\nDraft.\n", router=router))

    assert isinstance(out, CritiqueOutput)
    assert out.version == 1
    assert out.model == "moonshotai/kimi-k2"
    assert out.cost_usd == pytest.approx(router.budget.last_cost)
    assert out.md_path == "critique/0001.md"
    assert out.should_replan is False
    assert out.gaps and out.gaps[0].severity == "warn"

    md = (job.root / "critique/0001.md").read_text(encoding="utf-8")
    assert "# Critique" in md
    assert "missing analysis of X" in md

    sidecar = json.loads((job.root / "critique/0001.json").read_text(encoding="utf-8"))
    assert sidecar["version"] == 1
    assert sidecar["model"] == "moonshotai/kimi-k2"
    assert sidecar["payload"]["should_replan"] is False
    assert sidecar["payload"]["gaps"][0]["severity"] == "warn"

    rows = _read_critique_rows(db_path, job.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["version"] == 1
    assert row["md_path"] == "critique/0001.md"
    assert row["model"] == "moonshotai/kimi-k2"
    assert row["cost_usd"] == pytest.approx(router.budget.last_cost)
    assert row["should_replan"] == 0
    payload = json.loads(row["payload_json"])
    assert payload["gaps"][0]["description"] == "missing analysis of X"


def test_critique_uses_frontier_alt_tier_by_default(job: Job, db_path: Path, plan: Plan) -> None:
    _seed_findings(job, [0.7])
    router = _StubRouter()

    asyncio.run(critique(job, plan, "# Synthesis\n", router=router))

    assert router.calls, "critique must invoke router.call"
    assert router.calls[0][0] == DEFAULT_TIER == "frontier_alt"


def test_critique_file_rotation_writes_0002_on_second_call(
    job: Job, db_path: Path, plan: Plan
) -> None:
    _seed_findings(job, [0.7])
    router = _StubRouter()

    first = asyncio.run(critique(job, plan, "# Synth v1\n", router=router))
    second = asyncio.run(critique(job, plan, "# Synth v2\n", router=router))

    assert first.version == 1
    assert second.version == 2
    assert second.md_path == "critique/0002.md"

    assert (job.root / "critique/0001.md").exists()
    assert (job.root / "critique/0002.md").exists()
    assert (job.root / "critique/0001.json").exists()
    assert (job.root / "critique/0002.json").exists()

    rows = _read_critique_rows(db_path, job.id)
    versions = [r["version"] for r in rows]
    assert versions == [1, 2]


def test_critique_should_replan_persists_truthy(job: Job, db_path: Path, plan: Plan) -> None:
    _seed_findings(job, [0.6])
    output = CritiqueOutput(
        gaps=[Gap(description="big gap", severity="block", area="evidence")],
        unsupported_claims=[],
        suggested_subgoals=["new subgoal"],
        confidence_concerns=[],
        should_replan=True,
    )
    router = _StubRouter(output=output)

    out = asyncio.run(critique(job, plan, "# Synth\n", router=router))

    assert out.should_replan is True
    rows = _read_critique_rows(db_path, job.id)
    assert rows[0]["should_replan"] == 1

    events = _read_event_rows(db_path, job.id)
    written = [e for e in events if e["kind"] == "critique_written"]
    assert len(written) == 1
    payload = json.loads(written[0]["payload_json"])
    assert payload["should_replan"] is True
    assert payload["tier"] == "frontier_alt"
    assert payload["model"] == "moonshotai/kimi-k2"
    assert payload["gaps_count"] == 1


def test_critique_budget_exceeded_returns_stub_no_db_row(
    job: Job, db_path: Path, plan: Plan
) -> None:
    _seed_findings(job, [0.6])
    router = _StubRouter(
        side_effect={
            "frontier_alt": [BudgetExceeded(job.id, spent=10.0, cap=5.0)],
        },
    )

    out = asyncio.run(critique(job, plan, "# Synth\n", router=router))

    assert out.should_replan is False
    assert out.version == 0
    assert out.model == "budget_capped"
    assert out.cost_usd is None
    assert out.gaps == []

    # No critique row should be persisted on the budget-capped path.
    rows = _read_critique_rows(db_path, job.id)
    assert rows == []

    # No critique md/json files written either.
    assert not (job.root / "critique/0001.md").exists()
    assert not (job.root / "critique/0001.json").exists()

    events = _read_event_rows(db_path, job.id)
    warns = [e for e in events if e["kind"] == "warning"]
    assert warns, "budget-capped critique should emit a WARN event"
    assert any(e["level"] == "WARN" for e in warns)
    payloads = [json.loads(e["payload_json"]) for e in warns]
    assert any(p.get("budget_capped") is True for p in payloads)


def test_critique_top_n_findings_passed_to_model(job: Job, db_path: Path, plan: Plan) -> None:
    _seed_findings(job, [0.1, 0.9, 0.5])
    router = _StubRouter()

    asyncio.run(critique(job, plan, "# Synth\n", router=router))

    tier, args, _kwargs = router.calls[0]
    assert tier == "frontier_alt"
    assert args, "context payload must be passed positionally"
    payload = json.loads(args[0])
    confs = [f["confidence"] for f in payload["findings"]]
    assert confs == sorted(confs, reverse=True)
    assert payload["synthesis"] == "# Synth\n"
    assert payload["goal"] == job.goal


# ---------------------------------------------------------------------------
# Loop integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_critique_handler_triggers_cloud_replan(
    monkeypatch: pytest.MonkeyPatch,
    job: Job,
    db_path: Path,
    plan: Plan,
) -> None:
    """When ``critique.should_replan`` is True the loop calls ``cloud_replan``."""

    write_synthesis(
        job,
        "# Existing synthesis\n\nDraft body.\n",
        model="anthropic/claude-opus-4-7",
        cost_usd=0.001,
    )
    _seed_findings(job, [0.7])

    captured: dict[str, Any] = {}
    new_plan = plan.model_copy(update={"version": plan.version + 1})

    async def fake_critique(
        job_arg: Job,
        plan_arg: Plan,
        latest_synth: str | None,
        *,
        router: Any,
        tier: str = "frontier_alt",
    ) -> CritiqueOutput:
        captured["latest_synth"] = latest_synth
        captured["plan_version_in"] = plan_arg.version
        # Persist a real critique row + file so the loop can read it back.
        from research_agent.storage.markdown import write_critique as _write_critique

        payload = {
            "gaps": [{"description": "g", "severity": "block", "area": None}],
            "unsupported_claims": [],
            "suggested_subgoals": ["new"],
            "confidence_concerns": [],
            "should_replan": True,
        }
        version = _write_critique(
            job_arg,
            payload=payload,
            content="# Critique\n\nNeeds a replan.\n",
            model="moonshotai/kimi-k2",
            cost_usd=0.0042,
            should_replan=True,
        )
        return CritiqueOutput(
            gaps=[Gap(description="g", severity="block")],
            unsupported_claims=[],
            suggested_subgoals=["new"],
            confidence_concerns=[],
            should_replan=True,
            version=version,
            model="moonshotai/kimi-k2",
            cost_usd=0.0042,
            md_path=f"critique/{version:04d}.md",
        )

    async def fake_cloud_replan(
        job_arg: Job,
        plan_arg: Plan,
        critique_md: str,
        *,
        router: Any,
    ) -> Plan:
        captured["replan_called"] = True
        captured["critique_md"] = critique_md
        captured["plan_version_for_replan"] = plan_arg.version
        return new_plan

    from research_agent.orchestrator import plan as plan_module

    monkeypatch.setattr(critique_module, "critique", fake_critique)
    monkeypatch.setattr(plan_module, "cloud_replan", fake_cloud_replan)

    handlers = loop_module.default_handlers(router=object())
    critique_handler = handlers["critique"]

    result = await critique_handler(
        job, {"id": -1, "kind": "critique", "payload": {}, "plan_version": plan.version}
    )

    assert captured.get("replan_called") is True
    assert "Needs a replan" in captured["critique_md"]
    assert captured["plan_version_in"] == plan.version
    assert captured["plan_version_for_replan"] == plan.version
    assert captured["latest_synth"] == "# Existing synthesis\n\nDraft body.\n"

    assert result is not None
    assert result["should_replan"] is True

    events = _read_event_rows(db_path, job.id)
    triggered = [e for e in events if e["kind"] == "replan_triggered"]
    assert len(triggered) == 1
    payload = json.loads(triggered[0]["payload_json"])
    assert payload["from_version"] == plan.version
    assert payload["critique_version"] == 1
