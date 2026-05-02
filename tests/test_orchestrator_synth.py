"""Tests for ``research_agent.orchestrator.synth``."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from research_agent.llm.budgets import BudgetExceeded
from research_agent.orchestrator import synth as synth_module
from research_agent.orchestrator.plan import Plan, Subgoal, TaskSpec
from research_agent.orchestrator.synth import (
    FINAL_TOP_N,
    TOP_N_FINDINGS,
    SynthesisOutput,
    final_synthesis,
    synthesize,
)
from research_agent.prompts import loader as prompts_loader
from research_agent.storage import db
from research_agent.storage.jobs import Job
from research_agent.storage.markdown import write_finding, write_plan
from research_agent.storage.sources import write_source

# ---------------------------------------------------------------------------
# Stub Router / Budget
# ---------------------------------------------------------------------------


_DEFAULT_TIERS: dict[str, dict[str, Any]] = {
    "frontier": {"provider": "openrouter", "model": "anthropic/claude-opus-4-7"},
    "frontier_speed": {"provider": "openrouter", "model": "anthropic/claude-haiku-4-5"},
}


class _StubBudget:
    """Mimics ``BudgetTracker`` for cost-recording assertions."""

    def __init__(self, last_cost: float = 0.0042) -> None:
        self.last_cost = last_cost


class _StubAgent:
    """Stand-in for :class:`pydantic_ai.Agent` — captures construction args.

    Synth code constructs an ``Agent`` then hands it to ``router.call``;
    the stub router below ignores the agent entirely, so this class only
    needs to be constructible.
    """

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
    """Stub Router compatible with :func:`synthesize`'s call surface.

    ``side_effect`` is a dict mapping tier → list of exceptions / Nones
    consumed in order on each call to that tier. Once the list is empty
    a successful call returns ``content`` via ``result.output``.
    """

    def __init__(
        self,
        *,
        content: str = "# Synthesis report\n\nMarkdown content here.\n",
        side_effect: dict[str, list[Exception | None]] | None = None,
        tiers: dict[str, dict[str, Any]] | None = None,
        budget: _StubBudget | None = None,
    ) -> None:
        self.content = content
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
        return SimpleNamespace(output=self.content)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace pydantic_ai.Agent inside synth with a stub for every test."""
    _StubAgent.instances = []
    monkeypatch.setattr(synth_module, "Agent", _StubAgent)


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
        {"goal": "Investigate Widget Co synthesis"},
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
    """Seed one source + one finding per confidence value. Returns (source_ids, finding_ids)."""
    source_ids: list[int] = []
    finding_ids: list[int] = []
    for i, conf in enumerate(confidences):
        sid = _seed_source(job, f"https://example.com/{i}", f"content body {i}")
        source_ids.append(sid)
        fid = write_finding(job, f"{base_claim} {i}", conf, [sid])
        finding_ids.append(fid)
    return source_ids, finding_ids


def _read_synthesis_rows(db_path: Path, job_id: str) -> list[dict[str, Any]]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT version, md_path, model, cost_usd FROM syntheses"
            " WHERE job_id = ? ORDER BY version ASC",
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


def test_synthesize_writes_synthesis_and_report(job: Job, db_path: Path, plan: Plan) -> None:
    _seed_findings(job, [0.9, 0.5])
    router = _StubRouter()

    out = asyncio.run(synthesize(job, plan, router=router))

    assert isinstance(out, SynthesisOutput)
    assert out.truncated is False
    assert out.version == 1
    assert out.cost_usd == pytest.approx(router.budget.last_cost)
    assert out.model == "anthropic/claude-opus-4-7"

    rows = _read_synthesis_rows(db_path, job.id)
    assert len(rows) == 1
    assert rows[0]["version"] == 1
    assert rows[0]["md_path"] == "synthesis/0001.md"
    assert rows[0]["model"] == "anthropic/claude-opus-4-7"

    synth_md = (job.root / "synthesis/0001.md").read_text(encoding="utf-8")
    assert "Synthesis report" in synth_md

    report = job.root / "report.md"
    assert report.exists()
    assert report.read_text(encoding="utf-8") == synth_md
    assert out.report_path == str(report)


def test_synthesize_rotates_prior_report(job: Job, db_path: Path, plan: Plan) -> None:
    _seed_findings(job, [0.7])
    sentinel = "# Older report\n\nThis was here first.\n"
    (job.root / "report.md").write_text(sentinel, encoding="utf-8")

    router = _StubRouter(content="# New synthesis\n\nFresh content.\n")
    asyncio.run(synthesize(job, plan, router=router))

    history = job.root / "report.history"
    assert history.exists()
    archived = list(history.glob("*.md"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == sentinel

    new_report = (job.root / "report.md").read_text(encoding="utf-8")
    assert "New synthesis" in new_report
    assert sentinel.strip() not in new_report


def test_synthesize_top_n_ordering(job: Job, db_path: Path, plan: Plan) -> None:
    confidences = [0.1, 0.9, 0.4, 0.7, 0.5]
    _seed_findings(job, confidences)
    router = _StubRouter()

    asyncio.run(synthesize(job, plan, router=router))

    assert len(router.calls) == 1
    tier, args, _kwargs = router.calls[0]
    assert tier == "frontier"
    assert args, "synthesize must pass the context payload as a positional arg"
    payload = json.loads(args[0])
    seen_confidences = [f["confidence"] for f in payload["findings"]]
    assert seen_confidences == sorted(confidences, reverse=True)


def test_synthesize_budget_exceeded_falls_back_to_frontier_speed(
    job: Job, db_path: Path, plan: Plan
) -> None:
    _seed_findings(job, [0.6])
    router = _StubRouter(
        side_effect={
            "frontier": [BudgetExceeded(job.id, spent=10.0, cap=5.0)],
        },
    )

    out = asyncio.run(synthesize(job, plan, router=router))

    assert out.truncated is False
    assert out.model == "anthropic/claude-haiku-4-5"
    assert [c[0] for c in router.calls] == ["frontier", "frontier_speed"]

    rows = _read_synthesis_rows(db_path, job.id)
    assert rows[0]["model"] == "anthropic/claude-haiku-4-5"

    events = _read_event_rows(db_path, job.id)
    warns = [e for e in events if e["kind"] == "warning"]
    assert warns, "fallback path should record a WARN event"
    assert any(e["level"] == "WARN" for e in warns)
    assert any("frontier" in e["payload_json"] for e in warns)


def test_synthesize_budget_exceeded_twice_writes_stub(job: Job, db_path: Path, plan: Plan) -> None:
    _seed_findings(job, [0.6])
    router = _StubRouter(
        side_effect={
            "frontier": [BudgetExceeded(job.id, spent=10.0, cap=5.0)],
            "frontier_speed": [BudgetExceeded(job.id, spent=11.0, cap=5.0)],
        },
    )

    out = asyncio.run(synthesize(job, plan, router=router))

    assert out.truncated is True
    assert out.model == "budget_capped"
    assert out.cost_usd is None

    rows = _read_synthesis_rows(db_path, job.id)
    assert len(rows) == 1
    assert rows[0]["model"] == "budget_capped"
    assert rows[0]["cost_usd"] is None

    report = (job.root / "report.md").read_text(encoding="utf-8")
    assert "Report (truncated)" in report
    assert "budget cap was reached" in report.lower()

    events = _read_event_rows(db_path, job.id)
    warns = [e for e in events if e["kind"] == "warning"]
    assert len(warns) == 2
    payloads = [json.loads(e["payload_json"]) for e in warns]
    assert any(p.get("budget_capped") is True for p in payloads)


def test_final_synthesis_uses_larger_top_n_and_final_flag(
    job: Job, db_path: Path, plan: Plan
) -> None:
    # Seed more than TOP_N_FINDINGS so we can prove the larger window is used.
    n_seed = TOP_N_FINDINGS + 5
    _seed_findings(job, [0.5] * n_seed)
    router = _StubRouter()

    out = asyncio.run(final_synthesis(job, plan, router=router))

    assert out.truncated is False
    assert len(router.calls) == 1
    tier, args, _ = router.calls[0]
    assert tier == "frontier"
    payload = json.loads(args[0])
    assert payload.get("final") is True
    # Should have at least more than the in-loop window — capped at FINAL_TOP_N.
    assert len(payload["findings"]) == n_seed
    assert len(payload["findings"]) <= FINAL_TOP_N


def test_synthesize_emits_synthesis_written_event(job: Job, db_path: Path, plan: Plan) -> None:
    _seed_findings(job, [0.8])
    router = _StubRouter()

    out = asyncio.run(synthesize(job, plan, router=router))

    events = _read_event_rows(db_path, job.id)
    written = [e for e in events if e["kind"] == "synthesis_written"]
    assert len(written) == 1
    payload = json.loads(written[0]["payload_json"])
    assert payload["version"] == out.version
    assert payload["tier"] == "frontier"
    assert payload["truncated"] is False
    assert payload["report_path"] == out.report_path
