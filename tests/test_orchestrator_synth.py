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


def test_synthesize_retry_exhaustion_writes_partial_and_emits_failed(
    job: Job, db_path: Path, plan: Plan
) -> None:
    """Terminal retry exhaustion → partial md + ``synthesis_failed`` ERROR event."""
    _seed_findings(job, [0.6])
    router = _StubRouter(
        side_effect={
            "frontier": [RuntimeError("openrouter: connection reset after 6 retries")],
        },
    )

    out = asyncio.run(synthesize(job, plan, router=router))

    # SynthesisOutput marks the failure shape so the loop can distinguish it.
    assert out.truncated is True
    assert out.model == "synthesis_failed"
    assert out.cost_usd is None

    # Partial md exists at the synthesis/<v>.partial.md path (no DB row written).
    partial = job.root / f"synthesis/{out.version:04d}.partial.md"
    assert partial.exists()
    # Non-streaming call path — the partial is empty but its presence lets
    # the next attempt know a prior failure happened.
    assert partial.read_text(encoding="utf-8") == ""

    rows = _read_synthesis_rows(db_path, job.id)
    assert rows == [], "partial writes must not insert a syntheses row"

    events = _read_event_rows(db_path, job.id)
    failed = [e for e in events if e["kind"] == "synthesis_failed"]
    assert len(failed) == 1
    assert failed[0]["level"] == "ERROR"
    payload = json.loads(failed[0]["payload_json"])
    assert payload["tier"] == "frontier"
    assert "connection reset" in payload["reason"]
    assert payload["attempt_count"] == 1
    assert payload["partial_path"].endswith(".partial.md")


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


# ---------------------------------------------------------------------------
# Template-stub renderer (issue #39 — post-cap fallback with no LLM call)
# ---------------------------------------------------------------------------


def test_write_template_stub_output_renders_findings_and_sources(job: Job, db_path: Path) -> None:
    """With findings on disk, stub renders header + grouped claims + sources."""
    _seed_findings(job, [0.95, 0.6, 0.2])
    out = synth_module._write_template_stub_output(job)

    assert out.truncated is True
    assert out.model == "budget_capped_template"
    assert out.cost_usd is None

    report = (job.root / "report.md").read_text(encoding="utf-8")
    assert "template stub" in report.lower()
    assert job.goal in report
    assert "High confidence" in report
    assert "Medium confidence" in report
    assert "Low confidence" in report
    assert "claim 0" in report
    assert "claim 1" in report
    assert "claim 2" in report
    # Sources block lists at least one of the seeded sources.
    assert "https://example.com/0" in report

    rows = _read_synthesis_rows(db_path, job.id)
    assert len(rows) == 1
    assert rows[0]["model"] == "budget_capped_template"
    assert rows[0]["cost_usd"] is None

    events = _read_event_rows(db_path, job.id)
    written = [e for e in events if e["kind"] == "synthesis_written"]
    assert len(written) == 1
    payload = json.loads(written[0]["payload_json"])
    assert payload["tier"] == "template_stub"
    assert payload["truncated"] is True


def test_write_template_stub_output_empty_findings_falls_back_to_constant(
    job: Job, db_path: Path
) -> None:
    """No findings → fall back to the original ``_BUDGET_STUB_REPORT`` string."""
    out = synth_module._write_template_stub_output(job)

    assert out.truncated is True
    assert out.model == "budget_capped_template"
    report = (job.root / "report.md").read_text(encoding="utf-8")
    assert "Report (truncated)" in report
    assert "budget cap was reached" in report.lower()


def test_final_synthesis_after_cap_skips_frontier_tier(job: Job, db_path: Path, plan: Plan) -> None:
    """Post-cap helper must call only ``frontier_speed`` — not the primary tier."""
    _seed_findings(job, [0.7])
    router = _StubRouter()

    out = asyncio.run(synth_module.final_synthesis_after_cap(job, plan, router=router))
    assert out.truncated is False
    assert [c[0] for c in router.calls] == ["frontier_speed"]
    assert out.model == "anthropic/claude-haiku-4-5"


def test_final_synthesis_after_cap_falls_back_to_template_when_speed_capped(
    job: Job, db_path: Path, plan: Plan
) -> None:
    _seed_findings(job, [0.95, 0.4])
    router = _StubRouter(
        side_effect={
            "frontier_speed": [synth_module.BudgetExceeded(job.id, spent=11.0, cap=5.0)],
        },
    )

    out = asyncio.run(synth_module.final_synthesis_after_cap(job, plan, router=router))

    assert out.truncated is True
    assert out.model == "budget_capped_template"
    report = (job.root / "report.md").read_text(encoding="utf-8")
    assert "template stub" in report.lower()
    assert "claim 0" in report


# ---------------------------------------------------------------------------
# Subgoal-status extraction (issue #119)
# ---------------------------------------------------------------------------


def _read_latest_plan_subgoals(db_path: Path, job_id: str) -> list[dict[str, Any]]:
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM plans WHERE job_id = ? ORDER BY version DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return list(json.loads(row["payload_json"])["subgoals"])


def _read_latest_plan_version(db_path: Path, job_id: str) -> int:
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM plans WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row["v"])


@pytest.fixture
def multi_subgoal_plan(job: Job) -> Plan:
    p = Plan(
        version=1,
        objective="Investigate Widget Co",
        subgoals=[
            Subgoal(id=1, description="background", done=False),
            Subgoal(id=2, description="finances", done=False),
            Subgoal(id=3, description="connections", done=False),
        ],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=3,
    )
    write_plan(job, p.model_dump())
    return p


def test_synthesize_extracts_subgoal_status_and_strips_fence(
    job: Job, db_path: Path, multi_subgoal_plan: Plan
) -> None:
    """Trailing JSON fence closes subgoals 1 & 2 and is stripped from report.md."""
    _seed_findings(job, [0.9, 0.5])
    body = (
        "# Investigation Report\n\n"
        "## Executive Summary\n- finding [1]\n\n"
        "## Sources\n1. https://example.com/0 — \"Title\"\n"
    )
    fence = (
        '\n```json\n'
        '{"subgoal_status": {"1": "confirmed", "2": "refuted", "3": "confirmed"}}'
        '\n```\n'
    )
    router = _StubRouter(content=body + fence)

    asyncio.run(synthesize(job, multi_subgoal_plan, router=router))

    report = (job.root / "report.md").read_text(encoding="utf-8")
    assert "subgoal_status" not in report
    assert "```json" not in report
    assert "Investigation Report" in report

    synth_md = (job.root / "synthesis/0001.md").read_text(encoding="utf-8")
    assert "subgoal_status" not in synth_md

    subgoals = _read_latest_plan_subgoals(db_path, job.id)
    by_id = {sg["id"]: sg for sg in subgoals}
    assert by_id[1]["done"] is True
    assert by_id[2]["done"] is True
    assert by_id[3]["done"] is True

    events = _read_event_rows(db_path, job.id)
    updated = [e for e in events if e["kind"] == "plan_subgoals_updated"]
    assert len(updated) == 1
    payload = json.loads(updated[0]["payload_json"])
    assert sorted(payload["closed"]) == [1, 2, 3]


def test_synthesize_inconclusive_status_keeps_subgoal_open(
    job: Job, db_path: Path, multi_subgoal_plan: Plan
) -> None:
    """An ``inconclusive`` status leaves the subgoal ``done=False``."""
    _seed_findings(job, [0.7])
    body = "# Report\n\n## Sources\n1. https://x — \"t\"\n"
    fence = (
        '\n```json\n'
        '{"subgoal_status": {"1": "confirmed", "2": "inconclusive", "3": "refuted"}}'
        '\n```\n'
    )
    router = _StubRouter(content=body + fence)

    asyncio.run(synthesize(job, multi_subgoal_plan, router=router))

    subgoals = _read_latest_plan_subgoals(db_path, job.id)
    by_id = {sg["id"]: sg for sg in subgoals}
    assert by_id[1]["done"] is True
    assert by_id[2]["done"] is False
    assert by_id[3]["done"] is True

    events = _read_event_rows(db_path, job.id)
    updated = [e for e in events if e["kind"] == "plan_subgoals_updated"]
    assert len(updated) == 1
    payload = json.loads(updated[0]["payload_json"])
    assert payload["inconclusive"] == [2]


def test_synthesize_missing_fence_emits_warning_and_skips_plan_bump(
    job: Job, db_path: Path, multi_subgoal_plan: Plan
) -> None:
    """No trailing JSON fence: warning emitted, report still written, plan unchanged."""
    _seed_findings(job, [0.6])
    router = _StubRouter(content="# Report\n\nNo fence at all.\n")

    asyncio.run(synthesize(job, multi_subgoal_plan, router=router))

    assert (job.root / "report.md").exists()
    assert _read_latest_plan_version(db_path, job.id) == multi_subgoal_plan.version

    events = _read_event_rows(db_path, job.id)
    warns = [e for e in events if e["kind"] == "warning"]
    payloads = [json.loads(e["payload_json"]) for e in warns]
    assert any(p.get("stage") == "subgoal_status" for p in payloads)

    updated = [e for e in events if e["kind"] == "plan_subgoals_updated"]
    assert updated == []


def test_synthesize_malformed_fence_emits_warning_and_skips_plan_bump(
    job: Job, db_path: Path, multi_subgoal_plan: Plan
) -> None:
    """A trailing JSON fence with invalid JSON is tolerated: warn + no plan bump."""
    _seed_findings(job, [0.6])
    body = "# Report\n\n## Sources\n1. https://x — \"t\"\n"
    fence = '\n```json\n{not valid json\n```\n'
    router = _StubRouter(content=body + fence)

    asyncio.run(synthesize(job, multi_subgoal_plan, router=router))

    # Fence still gets stripped so it doesn't pollute report.md.
    report = (job.root / "report.md").read_text(encoding="utf-8")
    assert "not valid json" not in report
    assert "```json" not in report

    assert _read_latest_plan_version(db_path, job.id) == multi_subgoal_plan.version

    events = _read_event_rows(db_path, job.id)
    warns = [e for e in events if e["kind"] == "warning"]
    payloads = [json.loads(e["payload_json"]) for e in warns]
    assert any(p.get("stage") == "subgoal_status" for p in payloads)


def test_synthesize_passes_subgoals_in_context(
    job: Job, db_path: Path, multi_subgoal_plan: Plan
) -> None:
    """The context payload sent to the model includes ``subgoals``."""
    _seed_findings(job, [0.6])
    router = _StubRouter()

    asyncio.run(synthesize(job, multi_subgoal_plan, router=router))

    assert router.calls
    _tier, args, _kwargs = router.calls[0]
    payload = json.loads(args[0])
    assert "subgoals" in payload
    assert sorted(s["id"] for s in payload["subgoals"]) == [1, 2, 3]
    assert all(s["done"] is False for s in payload["subgoals"])


def test_synthesize_repeat_status_skips_plan_version_bump(
    job: Job, db_path: Path, multi_subgoal_plan: Plan
) -> None:
    """Repeated identical status maps must NOT bump the plan version.

    The synth heuristic fires every 25 tasks; on a 10K-task run it would
    fire 400 times. If every fire bumped the plan version we'd burn
    through MAX_PLAN_VERSIONS (200) long before the plan actually changed.
    """
    _seed_findings(job, [0.7])
    body = "# Report\n\n## Sources\n1. https://x — \"t\"\n"
    fence = (
        '\n```json\n'
        '{"subgoal_status": {"1": "confirmed", "2": "inconclusive", "3": "confirmed"}}'
        '\n```\n'
    )
    router = _StubRouter(content=body + fence)

    asyncio.run(synthesize(job, multi_subgoal_plan, router=router))
    version_after_first = _read_latest_plan_version(db_path, job.id)
    assert version_after_first == multi_subgoal_plan.version + 1

    # Second pass with the SAME status_map: no actual subgoal flip happens,
    # so no new plan version should be persisted.
    asyncio.run(synthesize(job, multi_subgoal_plan, router=router))
    version_after_second = _read_latest_plan_version(db_path, job.id)
    assert version_after_second == version_after_first

    events = _read_event_rows(db_path, job.id)
    updated = [e for e in events if e["kind"] == "plan_subgoals_updated"]
    assert len(updated) == 1


# ---------------------------------------------------------------------------
# Recommended Human Follow-Ups (issue #112)
# ---------------------------------------------------------------------------


def test_followup_recipes_in_context(job: Job, db_path: Path, plan: Plan) -> None:
    """The context payload sent to the synthesizer carries the recipe catalog."""
    _seed_findings(job, [0.7])
    router = _StubRouter()

    asyncio.run(synthesize(job, plan, router=router))

    assert router.calls
    _tier, args, _kwargs = router.calls[0]
    payload = json.loads(args[0])
    recipes = payload.get("followup_recipes")
    assert isinstance(recipes, str) and recipes, "recipes must be a non-empty string"
    # Spot-check a few specific channels the catalog must surface.
    assert "SEC TCR" in recipes or "Tip, Complaint, and Referral" in recipes
    assert "HHS-OIG Hotline" in recipes
    assert "licensing board" in recipes


def test_synthesizer_prompt_requires_followups_section() -> None:
    """The synthesizer prompt must instruct the model to emit the new section."""
    body = prompts_loader.load_prompt("synthesizer", goal="x")
    assert "Recommended Human Follow-Ups" in body
    assert "Adversarial fact-check targets" in body
    assert "FOIA candidates" in body
    assert "Whistleblower" in body


def test_critic_prompt_flags_missing_followups() -> None:
    """The critic prompt must audit the follow-ups section for completeness."""
    body = prompts_loader.load_prompt("critic")
    assert "follow-ups" in body
    assert "fact-check" in body


def test_build_context_exposes_goal_and_licensing_board_guidance(
    job: Job, db_path: Path
) -> None:
    """SBI Builders fixture-style structural check.

    Feeds a goal naming a subject + agency through ``_build_context`` and
    asserts the rendered JSON exposes both the goal and the recipe text
    that should drive an Adversarial fact-check target (the spokesperson)
    and a FOIA candidate (the licensing board's disciplinary file).
    """
    sbi_plan = Plan(
        version=1,
        objective="Investigate SBI Builders",
        subgoals=[Subgoal(id=1, description="background", done=False)],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=1,
    )
    findings = [
        {
            "id": 1,
            "claim": "SBI Builders is licensed by the State Contractors Board",
            "confidence": 0.9,
            "source_ids": [1],
            "tags": [],
        }
    ]
    sources = {
        1: {
            "id": 1,
            "url": "https://example.com/sbi",
            "title": "SBI Builders licensing record",
            "fetched_at": 0,
            "archive_url": None,
        }
    }

    context_json = synth_module._build_context(
        goal="investigate SBI Builders",
        plan=sbi_plan,
        findings=findings,
        sources=sources,
        prior=None,
        critique=None,
        followup_recipes=synth_module._load_followup_recipes(),
        final=False,
    )
    payload = json.loads(context_json)

    assert payload["goal"] == "investigate SBI Builders"
    recipes = payload["followup_recipes"]
    assert "licensing board" in recipes
    assert "spokesperson" in recipes or "press contact" in recipes
    assert "FOIA" in recipes


def test_synthesize_accepts_fence_with_space_before_json_lang(
    job: Job, db_path: Path, multi_subgoal_plan: Plan
) -> None:
    """The fence regex tolerates ``` ``` json``` (with whitespace).

    The synthesizer.md prompt's worked example renders the trailing fence
    with a space (``` ``` json``) so the outer documentation fence stays
    closeable. Models occasionally mimic the example over the instruction;
    parsing must accept either form.
    """
    _seed_findings(job, [0.7])
    body = "# Report\n\n## Sources\n1. https://x — \"t\"\n"
    fence = (
        '\n``` json\n'
        '{"subgoal_status": {"1": "confirmed", "2": "confirmed", "3": "confirmed"}}'
        '\n```\n'
    )
    router = _StubRouter(content=body + fence)

    asyncio.run(synthesize(job, multi_subgoal_plan, router=router))

    report = (job.root / "report.md").read_text(encoding="utf-8")
    assert "subgoal_status" not in report

    subgoals = _read_latest_plan_subgoals(db_path, job.id)
    assert all(sg["done"] is True for sg in subgoals)
