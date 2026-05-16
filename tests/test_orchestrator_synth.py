"""Tests for ``research_agent.orchestrator.synth``."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from research_agent.llm.budgets import BudgetExceeded
from research_agent.observability.events import emit
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
from research_agent.storage import artifacts, coverage, db, hypotheses
from research_agent.storage.jobs import Job
from research_agent.storage.markdown import (
    assemble_report,
    latest_fragment,
    write_finding,
    write_fragment,
    write_plan,
)
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
    monkeypatch.delenv("RESEARCH_FRAGMENT_SYNTH", raising=False)
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


def test_normalize_fragment_tags_accepts_ids_and_titles() -> None:
    assert synth_module.normalize_fragment_tags(
        ["timeline", "Working Hypotheses", "stakeholder_map"]
    ) == [
        "timeline",
        "hypotheses",
        "stakeholder-map",
    ]


def test_normalize_fragment_tags_warns_and_falls_back(
    job: Job,
    db_path: Path,
) -> None:
    out = synth_module.normalize_fragment_tags(["not-a-section"], job=job)

    assert out == ["open-questions"]
    events = _read_event_rows(db_path, job.id)
    event = next(e for e in events if e["kind"] == "finding_fragment_classification_miss")
    assert event["level"] == "WARN"
    payload = json.loads(event["payload_json"])
    assert payload["unknown_fragment_ids"] == ["not-a-section"]
    assert payload["fallback_fragment_ids"] == ["open-questions"]


def test_normalize_fragment_tags_empty_uses_fallback(job: Job, db_path: Path) -> None:
    assert synth_module.normalize_fragment_tags([], job=job) == ["open-questions"]

    events = _read_event_rows(db_path, job.id)
    payload = json.loads(events[-1]["payload_json"])
    assert payload["fallback_fragment_ids"] == ["open-questions"]
    assert payload["raw_count"] == 0


def test_load_top_findings_exposes_target_fragments(job: Job) -> None:
    write_finding(
        job,
        "Tagged claim",
        0.9,
        [1],
        target_fragments=["timeline", "connections"],
    )

    findings = synth_module._load_top_findings(job, 5)

    assert findings[0]["target_fragments"] == ["timeline", "connections"]


def test_load_top_findings_legacy_null_fragments(job: Job) -> None:
    conn = db.connect(job.db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO findings (
                    job_id, md_path, claim, confidence, source_ids, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job.id, "findings/legacy.md", "Legacy claim", 0.7, "[]", 1),
            )
    finally:
        conn.close()

    findings = synth_module._load_top_findings(job, 5)

    assert findings[0]["target_fragments"] == []


def test_select_stale_fragments_uses_tags_and_dependencies(job: Job, plan: Plan) -> None:
    write_finding(
        job,
        "Connections claim",
        0.8,
        [1],
        target_fragments=["connections"],
    )

    assert synth_module._select_stale_fragments(job, plan) == (
        "stakeholder-map",
        "connections",
    )


def test_select_stale_fragments_does_not_include_unrelated_sections(
    job: Job,
    plan: Plan,
) -> None:
    write_finding(job, "Timeline claim", 0.8, [1], target_fragments=["timeline"])

    assert synth_module._select_stale_fragments(job, plan) == ("timeline",)


def test_build_fragment_context_is_section_bounded(job: Job, plan: Plan) -> None:
    timeline_source = _seed_source(job, "https://example.com/timeline", "timeline body")
    other_source = _seed_source(job, "https://example.com/other", "other body")
    write_finding(
        job,
        "Timeline finding",
        0.9,
        [timeline_source],
        target_fragments=["timeline"],
    )
    write_finding(
        job,
        "Connections finding",
        0.9,
        [other_source],
        target_fragments=["connections"],
    )
    write_fragment(job, "timeline", "## Timeline\n\nPrior body.", source_finding_ids=[])

    context, relevant_findings, sources = synth_module._build_fragment_context(
        job,
        "timeline",
        plan,
    )
    payload = json.loads(context)

    assert [f["claim"] for f in relevant_findings] == ["Timeline finding"]
    assert [f["claim"] for f in payload["findings"]] == ["Timeline finding"]
    assert set(payload["sources"]) == {str(timeline_source)}
    assert set(sources) == {timeline_source}
    assert "other" not in context
    assert payload["prior_fragment"]["content"] == "## Timeline\n\nPrior body.\n"


def test_build_fragment_context_includes_dependency_fragments(job: Job, plan: Plan) -> None:
    write_fragment(
        job,
        "stakeholder-map",
        "## Stakeholder Map\n\nPrior stakeholders.",
        source_finding_ids=[],
    )

    context, _findings, _sources = synth_module._build_fragment_context(
        job,
        "connections",
        plan,
    )
    payload = json.loads(context)

    assert "stakeholder-map" in payload["dependency_fragments"]
    assert "Prior stakeholders" in payload["dependency_fragments"]["stakeholder-map"]["content"]


def test_assemble_report_uses_registry_order(job: Job) -> None:
    write_fragment(job, "timeline", "## Timeline\n\n- Later section.", source_finding_ids=[])
    write_fragment(
        job,
        "executive-summary",
        "## Executive Summary\n\n- First section.",
        source_finding_ids=[],
    )

    assembled = assemble_report(job)

    assert assembled == (
        "## Executive Summary\n\n- First section.\n\n"
        "## Timeline\n\n- Later section.\n"
    )


def test_assemble_report_uses_dependency_safe_synthesis_order(
    job: Job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_agent.orchestrator import fragments as fragments_module

    write_fragment(
        job,
        "executive-summary",
        "## Executive Summary\n\n- Summary.",
        source_finding_ids=[],
    )
    write_fragment(job, "timeline", "## Timeline\n\n- Timeline.", source_finding_ids=[])

    monkeypatch.setattr(
        fragments_module,
        "synthesis_order",
        lambda _subset=None: ("timeline", "executive-summary"),
    )

    assert assemble_report(job) == (
        "## Timeline\n\n- Timeline.\n\n"
        "## Executive Summary\n\n- Summary.\n"
    )


def test_assemble_report_skips_missing_sections(job: Job) -> None:
    write_fragment(job, "timeline", "## Timeline\n\n- Only section.", source_finding_ids=[])

    assembled = assemble_report(job)

    assert "## Timeline" in assembled
    assert "Working Hypotheses" not in assembled
    assert "Stakeholder Map" not in assembled


def test_assemble_report_empty_placeholder(job: Job) -> None:
    assert assemble_report(job) == "# Report\n\n_No report fragments are available yet._\n"


def test_assemble_report_can_reconcile_bottom_sources(job: Job) -> None:
    sid = _seed_source(job, "https://example.com/source", "source body")
    write_fragment(
        job,
        "executive-summary",
        f"## Executive Summary\n\n- Cited claim [{sid}].",
        source_finding_ids=[],
    )
    write_fragment(job, "sources", "## Sources\n", source_finding_ids=[])

    assembled = assemble_report(
        job,
        sources_by_id={
            sid: {
                "url": "https://example.com/source",
                "title": "Example source",
                "fetched_at": 0,
            }
        },
    )

    assert f"{sid}. https://example.com/source" in assembled
    assert assembled.endswith('(retrieved 1970-01-01)\n')


def test_synthesize_fragments_writes_updates_and_events(
    job: Job,
    db_path: Path,
    plan: Plan,
) -> None:
    sid = _seed_source(job, "https://example.com/timeline", "timeline body")
    fid = write_finding(job, "Timeline claim", 0.9, [sid], target_fragments=["timeline"])
    router = _StubRouter(content="## Timeline\n\n- Timeline claim [1].\n")

    out = asyncio.run(synth_module.synthesize_fragments(job, plan, router=router))

    assert out.model == "fragment_assembly"
    assert [call[0] for call in router.calls] == ["frontier"]
    assert _StubAgent.instances[-1].system_prompt is not None
    assert "section-fragment synthesizer" in _StubAgent.instances[-1].system_prompt
    fragment = latest_fragment(job, "timeline")
    assert fragment is not None
    assert fragment["content"] == "## Timeline\n\n- Timeline claim [1].\n"
    assert fragment["source_finding_ids"] == [fid]
    assert fragment["cited_source_ids"] == [sid]
    report = job.root / "report.md"
    assert report.exists()
    assert out.report_path == str(report)
    assert report.read_text(encoding="utf-8") == assemble_report(job)
    rows = _read_synthesis_rows(db_path, job.id)
    assert rows[0]["model"] == "fragment_assembly"
    assert (job.root / rows[0]["md_path"]).read_text(encoding="utf-8") == report.read_text(
        encoding="utf-8"
    )

    events = _read_event_rows(db_path, job.id)
    event = next(e for e in events if e["kind"] == "fragment_update")
    payload = json.loads(event["payload_json"])
    assert payload["section_id"] == "timeline"
    assert payload["version"] == 1
    assert payload["finding_ids"] == [fid]
    assert payload["tier"] == "frontier"
    assert payload["model"] == "anthropic/claude-opus-4-7"
    assert payload["cost_usd"] == pytest.approx(router.budget.last_cost)
    written = next(e for e in events if e["kind"] == "synthesis_written")
    written_payload = json.loads(written["payload_json"])
    assert written_payload["mode"] == "fragments"
    assert written_payload["fragment_versions"] == {"timeline": 1}


def test_synthesize_fragments_rotates_prior_report(
    job: Job,
    plan: Plan,
) -> None:
    (job.root / "report.md").write_text("# Prior\n\nold body\n", encoding="utf-8")
    write_finding(job, "Timeline claim", 0.9, [1], target_fragments=["timeline"])
    router = _StubRouter(content="## Timeline\n\n- New section.\n")

    asyncio.run(synth_module.synthesize_fragments(job, plan, router=router))

    history = list((job.root / "report.history").glob("*.md"))
    assert len(history) == 1
    assert history[0].read_text(encoding="utf-8") == "# Prior\n\nold body\n"
    assert (job.root / "report.md").read_text(encoding="utf-8") == assemble_report(job)


def test_synthesize_fragments_budget_failure_keeps_prior_fragment(
    job: Job,
    db_path: Path,
    plan: Plan,
) -> None:
    write_finding(job, "Timeline claim", 0.9, [1], target_fragments=["timeline"])
    write_fragment(job, "timeline", "## Timeline\n\nPrior.", source_finding_ids=[])
    router = _StubRouter(
        side_effect={
            "frontier": [BudgetExceeded(job.id, spent=10.0, cap=5.0)],
        },
    )

    out = asyncio.run(synth_module.synthesize_fragments(job, plan, router=router))

    assert out.model == "fragment_assembly"
    fragment = latest_fragment(job, "timeline")
    assert fragment is not None
    assert fragment["version"] == 1
    assert fragment["content"] == "## Timeline\n\nPrior.\n"
    assert (job.root / "report.md").read_text(encoding="utf-8") == assemble_report(job)
    rows = _read_event_rows(db_path, job.id)
    assert not any(e["kind"] == "fragment_update" for e in rows)
    warning_payloads = [
        json.loads(e["payload_json"]) for e in rows if e["kind"] == "warning"
    ]
    assert any(p.get("stage") == "fragment:timeline" for p in warning_payloads)


def test_synthesize_fragments_nonbudget_failure_keeps_prior_fragment(
    job: Job,
    db_path: Path,
    plan: Plan,
) -> None:
    write_finding(job, "Timeline claim", 0.9, [1], target_fragments=["timeline"])
    write_fragment(job, "timeline", "## Timeline\n\nPrior.", source_finding_ids=[])
    router = _StubRouter(side_effect={"frontier": [RuntimeError("provider failed")]})

    asyncio.run(synth_module.synthesize_fragments(job, plan, router=router))

    fragment = latest_fragment(job, "timeline")
    assert fragment is not None
    assert fragment["version"] == 1
    assert fragment["content"] == "## Timeline\n\nPrior.\n"
    rows = _read_event_rows(db_path, job.id)
    assert not any(e["kind"] == "fragment_update" for e in rows)
    failed = [e for e in rows if e["kind"] == "synthesis_llm_failed"]
    assert failed


def test_synthesize_fragments_second_pass_without_new_findings_is_noop(
    job: Job,
    db_path: Path,
    plan: Plan,
) -> None:
    """Cost scales with delta: an unchanged section is not re-synthesized."""
    sid = _seed_source(job, "https://example.com/timeline", "timeline body")
    write_finding(job, "Timeline claim", 0.9, [sid], target_fragments=["timeline"])
    router = _StubRouter(content="## Timeline\n\n- Timeline claim [1].\n")

    first = asyncio.run(synth_module.synthesize_fragments(job, plan, router=router))
    assert [call[0] for call in router.calls] == ["frontier"]
    assert latest_fragment(job, "timeline")["version"] == 1

    # No new evidence landed — the second pass must select nothing and
    # leave the prior fragment version untouched (no extra LLM calls).
    second = asyncio.run(synth_module.synthesize_fragments(job, plan, router=router))

    assert [call[0] for call in router.calls] == ["frontier"]
    assert second.model == "fragment_assembly"
    assert latest_fragment(job, "timeline")["version"] == 1
    assert (job.root / "report.md").read_text(encoding="utf-8") == second.content
    assert second.content == assemble_report(job)
    fragment_updates = [
        e for e in _read_event_rows(db_path, job.id) if e["kind"] == "fragment_update"
    ]
    assert len(fragment_updates) == 1
    assert first.model == "fragment_assembly"


def test_synthesize_fragment_mode_env_branches_from_legacy(
    job: Job,
    db_path: Path,
    plan: Plan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_finding(job, "Timeline claim", 0.9, [1], target_fragments=["timeline"])
    router = _StubRouter(content="## Timeline\n\n- Timeline claim.\n")
    monkeypatch.setenv("RESEARCH_FRAGMENT_SYNTH", "1")

    out = asyncio.run(synthesize(job, plan, router=router))

    assert out.report_path == str(job.root / "report.md")
    assert latest_fragment(job, "timeline") is not None
    assert (job.root / "report.md").read_text(encoding="utf-8") == assemble_report(job)
    events = _read_event_rows(db_path, job.id)
    mode_event = next(e for e in events if e["kind"] == "synthesis_mode")
    payload = json.loads(mode_event["payload_json"])
    assert payload["mode"] == "fragments"
    assert payload["entrypoint"] == "synthesize_fragments"


def test_synthesize_emits_legacy_mode_event(
    job: Job,
    db_path: Path,
    plan: Plan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RESEARCH_FRAGMENT_SYNTH", raising=False)
    _seed_findings(job, [0.9])
    router = _StubRouter()

    asyncio.run(synthesize(job, plan, router=router))

    events = _read_event_rows(db_path, job.id)
    mode_event = next(e for e in events if e["kind"] == "synthesis_mode")
    payload = json.loads(mode_event["payload_json"])
    assert payload == {"mode": "legacy", "entrypoint": "synthesize", "final": False}


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


def test_synthesize_primary_nonbudget_failure_uses_fallback_tier(
    job: Job, db_path: Path, plan: Plan
) -> None:
    """HTTP 400-style primary failure tries ``frontier_speed`` before degrading."""
    _seed_findings(job, [0.6])
    router = _StubRouter(
        content="# Fallback synthesis\n\nRecovered from the primary model.\n",
        side_effect={
            "frontier": [
                RuntimeError(
                    "HTTP 400 Bad Request: provider rejected reasoning/channel text. "
                    "prompt=" + ("do not log prompt body " * 80)
                )
            ],
        },
    )

    out = asyncio.run(synthesize(job, plan, router=router))

    assert out.truncated is False
    assert out.model == "anthropic/claude-haiku-4-5"
    assert [call[0] for call in router.calls] == ["frontier", "frontier_speed"]
    assert not list((job.root / "synthesis").glob("*.failed.md"))
    assert "Fallback synthesis" in (job.root / "report.md").read_text(encoding="utf-8")

    events = _read_event_rows(db_path, job.id)
    llm_failed = [e for e in events if e["kind"] == "synthesis_llm_failed"]
    fallback = [e for e in events if e["kind"] == "synthesis_fallback_tier_used"]
    deterministic = [
        e for e in events if e["kind"] == "synthesis_deterministic_fallback_written"
    ]
    assert len(llm_failed) == 1
    assert len(fallback) == 1
    assert deterministic == []
    payload = json.loads(llm_failed[0]["payload_json"])
    assert payload["tier"] == "frontier"
    assert payload["provider"] == "openrouter"
    assert payload["model"] == "anthropic/claude-opus-4-7"
    assert payload["provider_format_specific"] is True
    assert "HTTP 400" in payload["diagnostic_snippet"]
    assert len(payload["diagnostic_snippet"]) <= 500
    assert "do not log prompt body" not in payload["diagnostic_snippet"]


def test_synthesize_both_nonbudget_failures_write_deterministic_report(
    job: Job, db_path: Path, plan: Plan
) -> None:
    """Both LLM tiers fail → failed trace artifact plus deterministic report.md."""
    _seed_findings(job, [0.85], base_claim="candidate roster finding")
    artifacts.write_table_artifact(
        job,
        "candidates",
        schema=artifacts.CANDIDATE_ROSTER_SCHEMA,
        rows=[
            {
                "state": "CA",
                "chamber": "House",
                "district_or_seat": "1",
                "candidate_name": "Jane Doe",
                "party": "Democratic",
                "candidate_status": "filed",
                "confidence": "high",
                "official_campaign_website": "https://jane.example",
                "source_url": "https://example.com/jane",
            }
        ],
        source_coverage="CA House fixture",
    )
    [unit] = coverage.declare_coverage(
        job,
        [{"state": "MD", "chamber": "House", "source_type": "state-ballot-qualified"}],
    )
    coverage.upsert_unit_status(
        job,
        unit.dim_key,
        "confirmed_gap",
        attempt={
            "task_kind": "state_election_search",
            "status": "failed",
            "reason": "2026 list not yet public",
        },
        unblocker="Check the Maryland SBE site after filings close",
    )
    router = _StubRouter(
        side_effect={
            "frontier": [RuntimeError("HTTP 400 Bad Request: provider rejected channel")],
            "frontier_speed": [RuntimeError("fallback parser failed")],
        },
    )

    out = asyncio.run(synthesize(job, plan, router=router))

    assert out.truncated is True
    assert out.model == "deterministic_fallback"
    assert out.report_path == str(job.root / "report.md")
    assert [call[0] for call in router.calls] == ["frontier", "frontier_speed"]

    failed_path = job.root / f"synthesis/{out.version:04d}.failed.md"
    assert failed_path.exists()
    failed_md = failed_path.read_text(encoding="utf-8")
    assert "## Partial Output" in failed_md
    assert "_No partial output captured._" in failed_md
    assert "## Traceback" in failed_md
    assert "Primary Tier Traceback" in failed_md
    assert "Fallback Tier Traceback" in failed_md
    assert "RuntimeError: HTTP 400 Bad Request" in failed_md
    assert "RuntimeError: fallback parser failed" in failed_md

    rows = _read_synthesis_rows(db_path, job.id)
    assert len(rows) == 1
    assert rows[0]["model"] == "deterministic_fallback"

    report = (job.root / "report.md").read_text(encoding="utf-8")
    assert "Report (deterministic fallback)" in report
    assert "## Findings" in report
    assert "candidate roster finding 0" in report
    assert "## Artifacts" in report
    assert "[CSV](artifacts/candidates.csv)" in report
    assert "## Coverage Ledger" in report
    assert "confirmed_gap" in report
    assert "## Confirmed Gaps" in report
    assert "Maryland SBE" in report

    events = _read_event_rows(db_path, job.id)
    assert [e["kind"] for e in events].count("synthesis_llm_failed") == 2
    assert [e["kind"] for e in events].count("synthesis_fallback_tier_used") == 1
    assert [e["kind"] for e in events].count("synthesis_deterministic_fallback_written") == 1
    assert [e["kind"] for e in events].count("synthesis_failed") == 0
    deterministic = [
        e for e in events if e["kind"] == "synthesis_deterministic_fallback_written"
    ][0]
    payload = json.loads(deterministic["payload_json"])
    assert payload["report_path"] == str(job.root / "report.md")
    assert payload["failed_path"].endswith(".failed.md")
    assert payload["primary_tier"] == "frontier"
    assert payload["fallback_tier"] == "frontier_speed"
    assert payload["has_artifacts"] is True
    assert payload["has_coverage_ledger"] is True


def test_synthesize_both_nonbudget_failures_with_no_rows_still_writes_report(
    job: Job, db_path: Path, plan: Plan
) -> None:
    router = _StubRouter(
        side_effect={
            "frontier": [RuntimeError("primary model failed")],
            "frontier_speed": [RuntimeError("fallback model failed")],
        },
    )

    out = asyncio.run(synthesize(job, plan, router=router))

    assert out.truncated is True
    assert out.model == "deterministic_fallback"
    assert out.report_path == str(job.root / "report.md")
    assert (job.root / f"synthesis/{out.version:04d}.failed.md").exists()
    report = (job.root / "report.md").read_text(encoding="utf-8")
    assert "Report (deterministic fallback)" in report
    assert "_No findings recorded" in report
    assert "_No sources cited" in report

    events = _read_event_rows(db_path, job.id)
    assert [e["kind"] for e in events].count("synthesis_deterministic_fallback_written") == 1


def test_failed_synthesis_artifact_includes_partial_content_and_traceback(
    job: Job, db_path: Path
) -> None:
    try:
        raise RuntimeError("handler crashed mid-write")
    except RuntimeError as exc:
        out = synth_module._write_failed_output(
            job,
            tier="frontier",
            exc=exc,
            attempt_count=1,
            partial_content="# Partial report\n\nDraft body",
        )

    failed_path = job.root / f"synthesis/{out.version:04d}.failed.md"
    failed_md = failed_path.read_text(encoding="utf-8")
    assert "# Partial report" in failed_md
    assert "Draft body" in failed_md
    assert "RuntimeError: handler crashed mid-write" in failed_md
    assert list((job.root / "synthesis").glob("*.partial.md")) == []

    rows = _read_synthesis_rows(db_path, job.id)
    assert rows == []


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


def test_final_synthesis_after_cap_nonbudget_failure_writes_deterministic_report(
    job: Job, db_path: Path, plan: Plan
) -> None:
    _seed_findings(job, [0.95])
    router = _StubRouter(
        side_effect={
            "frontier_speed": [RuntimeError("post-cap provider parse failure")],
        },
    )

    out = asyncio.run(synth_module.final_synthesis_after_cap(job, plan, router=router))

    assert out.truncated is True
    assert out.model == "deterministic_fallback"
    assert [c[0] for c in router.calls] == ["frontier_speed"]
    report = (job.root / "report.md").read_text(encoding="utf-8")
    assert "Report (deterministic fallback)" in report
    assert "claim 0" in report
    assert (job.root / f"synthesis/{out.version:04d}.failed.md").exists()

    events = _read_event_rows(db_path, job.id)
    failed = [e for e in events if e["kind"] == "synthesis_llm_failed"]
    assert len(failed) == 1
    assert json.loads(failed[0]["payload_json"])["post_cap"] is True
    deterministic = [
        e for e in events if e["kind"] == "synthesis_deterministic_fallback_written"
    ]
    assert len(deterministic) == 1
    payload = json.loads(deterministic[0]["payload_json"])
    assert payload["primary_tier"] is None
    assert payload["fallback_tier"] == "frontier_speed"
    assert payload["post_cap"] is True


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
        '## Sources\n1. https://example.com/0 — "Title"\n'
    )
    fence = (
        '\n```json\n{"subgoal_status": {"1": "confirmed", "2": "refuted", "3": "confirmed"}}\n```\n'
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


def test_synthesize_extracts_raw_trailing_subgoal_status_json(
    job: Job, db_path: Path, multi_subgoal_plan: Plan
) -> None:
    """A raw trailing JSON object is parsed and stripped even without a fence."""
    _seed_findings(job, [0.9, 0.5])
    body = (
        "# Investigation Report\n\n"
        "## Executive Summary\n- finding [1]\n\n"
        '## Sources\n1. https://example.com/0 — "Title"\n'
    )
    raw_json = '\n{"closed": [1], "reopened": [2], "inconclusive": [3]}\n'
    router = _StubRouter(content=body + raw_json)

    asyncio.run(synthesize(job, multi_subgoal_plan, router=router))

    report = (job.root / "report.md").read_text(encoding="utf-8")
    assert '"closed"' not in report
    assert "Investigation Report" in report

    subgoals = _read_latest_plan_subgoals(db_path, job.id)
    by_id = {sg["id"]: sg for sg in subgoals}
    assert by_id[1]["done"] is True
    assert by_id[2]["done"] is False
    assert by_id[3]["done"] is False

    events = _read_event_rows(db_path, job.id)
    updated = [e for e in events if e["kind"] == "plan_subgoals_updated"]
    assert len(updated) == 1
    payload = json.loads(updated[0]["payload_json"])
    assert payload["closed"] == [1]
    assert payload["inconclusive"] == [2, 3]


def test_synthesize_extracts_subgoal_status_section_json(
    job: Job, db_path: Path, multi_subgoal_plan: Plan
) -> None:
    """A final ``## Subgoal Status`` section can carry the JSON payload."""
    _seed_findings(job, [0.9])
    content = (
        "# Investigation Report\n\n"
        "## Executive Summary\n- finding [1]\n\n"
        '## Sources\n1. https://example.com/0 — "Title"\n\n'
        "## Subgoal Status\n\n"
        "```json\n"
        '{"subgoal_status": {"1": "confirmed", "2": "inconclusive", "3": "refuted"}}'
        "\n```\n"
    )
    router = _StubRouter(content=content)

    asyncio.run(synthesize(job, multi_subgoal_plan, router=router))

    report = (job.root / "report.md").read_text(encoding="utf-8")
    assert "## Subgoal Status" not in report
    assert "subgoal_status" not in report

    subgoals = _read_latest_plan_subgoals(db_path, job.id)
    by_id = {sg["id"]: sg for sg in subgoals}
    assert by_id[1]["done"] is True
    assert by_id[2]["done"] is False
    assert by_id[3]["done"] is True


def test_synthesize_extracts_subgoal_status_from_prose(
    job: Job, db_path: Path, multi_subgoal_plan: Plan
) -> None:
    """Prose-only status lines are an observable fallback."""
    _seed_findings(job, [0.9])
    content = (
        "# Investigation Report\n\n"
        "## Hypotheses\n\n"
        "Subgoal 1: Confirmed after checking filings [1].\n"
        "Subgoal 2: Inconclusive because records are missing.\n"
        "H3: Refuted by the permit history [1].\n"
        "H99: Confirmed, but this is not in the active plan.\n\n"
        '## Sources\n1. https://example.com/0 — "Title"\n'
    )
    router = _StubRouter(content=content)

    asyncio.run(synthesize(job, multi_subgoal_plan, router=router))

    subgoals = _read_latest_plan_subgoals(db_path, job.id)
    by_id = {sg["id"]: sg for sg in subgoals}
    assert by_id[1]["done"] is True
    assert by_id[2]["done"] is False
    assert by_id[3]["done"] is True

    events = _read_event_rows(db_path, job.id)
    prose_events = [e for e in events if e["kind"] == "synth_status_from_prose"]
    assert len(prose_events) == 1
    payload = json.loads(prose_events[0]["payload_json"])
    assert payload["status"] == {"1": "confirmed", "2": "inconclusive", "3": "refuted"}
    assert [e for e in events if e["kind"] == "synth_status_missing"] == []


def test_synthesize_inconclusive_status_keeps_subgoal_open(
    job: Job, db_path: Path, multi_subgoal_plan: Plan
) -> None:
    """An ``inconclusive`` status leaves the subgoal ``done=False``."""
    _seed_findings(job, [0.7])
    body = '# Report\n\n## Sources\n1. https://x — "t"\n'
    fence = (
        "\n```json\n"
        '{"subgoal_status": {"1": "confirmed", "2": "inconclusive", "3": "refuted"}}'
        "\n```\n"
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
    """No structured or prose status: warning emitted, report written, plan unchanged."""
    _seed_findings(job, [0.6])
    router = _StubRouter(content="# Report\n\nNo fence at all.\n")

    asyncio.run(synthesize(job, multi_subgoal_plan, router=router))

    assert (job.root / "report.md").exists()
    assert _read_latest_plan_version(db_path, job.id) == multi_subgoal_plan.version

    events = _read_event_rows(db_path, job.id)
    missing = [e for e in events if e["kind"] == "synth_status_missing"]
    assert len(missing) == 1
    assert missing[0]["level"] == "WARN"

    updated = [e for e in events if e["kind"] == "plan_subgoals_updated"]
    assert updated == []


def test_synthesize_malformed_fence_emits_warning_and_skips_plan_bump(
    job: Job, db_path: Path, multi_subgoal_plan: Plan
) -> None:
    """A trailing JSON fence with invalid JSON is tolerated: warn + no plan bump."""
    _seed_findings(job, [0.6])
    body = '# Report\n\n## Sources\n1. https://x — "t"\n'
    fence = "\n```json\n{not valid json\n```\n"
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
    body = '# Report\n\n## Sources\n1. https://x — "t"\n'
    fence = (
        "\n```json\n"
        '{"subgoal_status": {"1": "confirmed", "2": "inconclusive", "3": "confirmed"}}'
        "\n```\n"
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
# Confirmed Gaps (issue #258)
# ---------------------------------------------------------------------------


def _insert_failed_task(
    job: Job,
    *,
    kind: str,
    payload: dict[str, Any],
    error: str,
    plan_version: int = 1,
) -> None:
    conn = db.connect(job.db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO tasks"
                " (job_id, plan_version, kind, payload_json, status, error, retry_count)"
                " VALUES (?, ?, ?, ?, 'failed', ?, 1)",
                (job.id, plan_version, kind, json.dumps(payload, sort_keys=True), error),
            )
    finally:
        conn.close()


def test_compute_confirmed_gaps_from_failed_tasks_plan_and_low_yield(
    job: Job, db_path: Path
) -> None:
    gapped_plan = Plan(
        version=1,
        objective="Investigate Alameda restroom delay",
        subgoals=[
            Subgoal(
                id=1,
                description="Identify the prime contractor name",
                done=False,
                gap_reason="contract award file unavailable without city clerk records",
            )
        ],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=2,
    )
    write_plan(job, gapped_plan.model_dump())
    payload = {
        "query": "site:alamedaca.gov shoreline restroom contractor",
        "sub_question": "Identify the prime contractor name",
        "subgoal_id": 1,
    }
    for _ in range(3):
        _insert_failed_task(
            job,
            kind="web_search",
            payload=payload,
            error="0 results",
        )
    emit(job, "INFO", "planner", "plan_subgoals_updated", {"inconclusive": [1]})
    stem = synth_module._query_stem_for_payload(payload)
    (job.root / "synthesis" / "low_yield.json").write_text(
        json.dumps(
            [
                {
                    "kind": "web_search",
                    "query_stem": stem,
                    "count": 3,
                    "suggested_unblocker": (
                        "FOIA the City of Alameda Clerk for the prime contract award file"
                    ),
                }
            ]
        ),
        encoding="utf-8",
    )

    gaps = synth_module._compute_confirmed_gaps(job, gapped_plan)

    assert len(gaps) == 1
    gap = gaps[0]
    assert gap["topic"] == "Identify the prime contractor name"
    assert "city clerk records" in gap["failure_summary"]
    assert "FOIA the City of Alameda Clerk for the prime contract award file" in gap[
        "suggested_unblocker"
    ]
    assert gap["attempts"] == [
        {
            "task_kind": "web_search",
            "query": "site:alamedaca.gov shoreline restroom contractor",
            "failure_reason": "0 results from web_search",
            "count": 3,
        }
    ]


def test_compute_confirmed_gaps_empty_without_failure_signals(job: Job, plan: Plan) -> None:
    assert synth_module._compute_confirmed_gaps(job, plan) == []


def test_build_context_includes_confirmed_gaps(job: Job, db_path: Path, plan: Plan) -> None:
    context_json = synth_module._build_context(
        goal=job.goal,
        plan=plan,
        findings=[],
        sources={},
        prior=None,
        critique=None,
        followup_recipes="",
        paid_unblock_recipes="",
        confirmed_gaps=[
            {
                "topic": "prime contractor name",
                "attempts": [],
                "failure_summary": "No public source identified.",
                "suggested_unblocker": "FOIA the City of Alameda Clerk",
            }
        ],
        final=False,
    )
    payload = json.loads(context_json)
    assert payload["confirmed_gaps"][0]["topic"] == "prime contractor name"
    assert payload["subgoals"][0]["gap_reason"] is None


def test_synthesize_preserves_confirmed_gaps_section_and_strips_status_fence(
    job: Job, db_path: Path, multi_subgoal_plan: Plan
) -> None:
    _seed_findings(job, [0.9])
    content = (
        "# Investigation Report\n\n"
        "## Executive Summary\n\n- finding [1].\n\n"
        "## Confirmed Gaps\n\n"
        "- **Prime contractor name** — Tried city records. Unblocker: "
        "FOIA the City of Alameda Clerk\n"
        "  - Attempted `web_search` for \"site:alamedaca.gov contractor\": "
        "0 results (count: 3).\n\n"
        "## Sources\n"
        '1. https://example.com/0 — "Title" (retrieved 2026-05-06)\n'
        "\n```json\n"
        '{"subgoal_status": {"1": "inconclusive", "2": "inconclusive", "3": "inconclusive"}}'
        "\n```\n"
    )
    router = _StubRouter(content=content)

    asyncio.run(synthesize(job, multi_subgoal_plan, router=router))

    report = (job.root / "report.md").read_text(encoding="utf-8")
    assert "## Confirmed Gaps" in report
    assert "FOIA the City of Alameda Clerk" in report
    assert "subgoal_status" not in report


# ---------------------------------------------------------------------------
# Working hypotheses (issue #261)
# ---------------------------------------------------------------------------


def test_synthesize_extracts_hypothesis_updates_and_strips_fence(
    job: Job, db_path: Path, multi_subgoal_plan: Plan
) -> None:
    _source_ids, finding_ids = _seed_findings(job, [0.9])
    content = (
        "# Investigation Report\n\n"
        "## Executive Summary\n\n- finding [1].\n\n"
        "## Working Hypotheses\n\n"
        "### H-new: Contractor underbid\n"
        "**Status:** Open\n"
        "**Confidence:** 0.65\n"
        "- Supporting: finding [1].\n\n"
        "## Sources\n"
        '1. https://example.com/0 — "Title" (retrieved 2026-05-06)\n'
        "\n```json\n"
        '{"subgoal_status": {"1": "inconclusive", "2": "inconclusive", "3": "inconclusive"}}'
        "\n```\n"
        "```json\n"
        '{"hypothesis_updates": ['
        '{"statement": "The contractor underbid and is slow-walking change orders.", '
        '"confidence": 0.65, '
        f'"supports": [{finding_ids[0]}], '
        '"refutes": [], "status": "open"}]}'
        "\n```\n"
    )
    router = _StubRouter(content=content)

    asyncio.run(synthesize(job, multi_subgoal_plan, router=router))

    report = (job.root / "report.md").read_text(encoding="utf-8")
    assert "## Working Hypotheses" in report
    assert "hypothesis_updates" not in report
    assert "subgoal_status" not in report

    rows = hypotheses.list_hypotheses(job)
    assert len(rows) == 1
    assert rows[0]["statement"] == "The contractor underbid and is slow-walking change orders."
    assert rows[0]["confidence"] == pytest.approx(0.65)
    assert rows[0]["supports"] == [finding_ids[0]]
    assert rows[0]["status"] == "open"

    events = _read_event_rows(db_path, job.id)
    updated = [e for e in events if e["kind"] == "hypothesis_updated"]
    assert len(updated) == 1
    payload = json.loads(updated[0]["payload_json"])
    assert payload["id"] == rows[0]["id"]
    assert payload["status"] == "open"


def test_synthesize_context_includes_current_hypotheses(job: Job, plan: Plan) -> None:
    _source_ids, finding_ids = _seed_findings(job, [0.8])
    hid = hypotheses.upsert_hypothesis(
        job,
        plan_version=1,
        statement="Permitting friction is the primary delay driver.",
        confidence=0.55,
        supports=[finding_ids[0]],
        refutes=[],
        status="open",
    )
    router = _StubRouter()

    asyncio.run(synthesize(job, plan, router=router))

    _tier, args, _kwargs = router.calls[0]
    payload = json.loads(args[0])
    assert payload["current_hypotheses"][0]["id"] == hid
    assert payload["current_hypotheses"][0]["statement"] == (
        "Permitting friction is the primary delay driver."
    )
    assert payload["current_hypotheses"][0]["supports"] == [finding_ids[0]]
    assert payload["current_hypotheses"][0]["supporting_findings"][0]["id"] == finding_ids[0]


def test_synthesize_context_includes_generated_artifacts(job: Job, plan: Plan) -> None:
    artifacts.write_table_artifact(
        job,
        "candidates",
        schema=artifacts.CANDIDATE_ROSTER_SCHEMA,
        rows=[
            {
                "state": "CA",
                "chamber": "House",
                "candidate_name": "Jane Doe",
                "source_url": "https://example.com/jane",
            }
        ],
    )
    router = _StubRouter()

    asyncio.run(synthesize(job, plan, router=router))

    _tier, args, _kwargs = router.calls[0]
    payload = json.loads(args[0])
    assert payload["artifacts"] == [
        {
            "name": "candidates",
            "schema_version": 1,
            "row_count": 1,
            "generated_at_epoch": payload["artifacts"][0]["generated_at_epoch"],
            "source_coverage": "",
            "csv_path": "artifacts/candidates.csv",
        }
    ]


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


def test_build_context_exposes_goal_and_licensing_board_guidance(job: Job, db_path: Path) -> None:
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
        paid_unblock_recipes=synth_module._load_paid_unblock_recipes(),
        final=False,
    )
    payload = json.loads(context_json)

    assert payload["goal"] == "investigate SBI Builders"
    recipes = payload["followup_recipes"]
    assert "licensing board" in recipes
    assert "spokesperson" in recipes or "press contact" in recipes
    assert "FOIA" in recipes

    # SBI Builders acceptance criterion: paid catalog must surface
    # LinkedIn (for company employees) and a regional / trade-press
    # subscription that an investigation of a regional builder would
    # plausibly need.
    paid = payload["paid_unblock_recipes"]
    assert isinstance(paid, str) and paid
    assert "LinkedIn" in paid
    assert "ENR" in paid or "Crain" in paid or "trade press" in paid.lower()


# ---------------------------------------------------------------------------
# Paid Resources That Would Unblock This Investigation (issue #113)
# ---------------------------------------------------------------------------


def test_paid_unblock_recipes_in_context(job: Job, db_path: Path, plan: Plan) -> None:
    """The synthesizer's context payload carries the paid-unblock catalog."""
    _seed_findings(job, [0.7])
    router = _StubRouter()

    asyncio.run(synthesize(job, plan, router=router))

    assert router.calls
    _tier, args, _kwargs = router.calls[0]
    payload = json.loads(args[0])
    paid = payload.get("paid_unblock_recipes")
    assert isinstance(paid, str) and paid
    # Spot-check several specific services from the seeded catalog so a
    # regression that drops one is caught.
    assert "LinkedIn" in paid
    assert "PACER" in paid
    assert "Westlaw" in paid


def test_synthesizer_prompt_requires_paid_resources_section() -> None:
    """The synthesizer prompt must instruct the model to emit the new section."""
    body = prompts_loader.load_prompt("synthesizer", goal="x")
    assert "Paid Resources That Would Unblock This Investigation" in body
    assert "High value" in body
    assert "Lower value" in body
    assert "because" in body
    # The section must be conditional on the critique's flagged opportunities.
    assert "paid_opportunities" in body


# ---------------------------------------------------------------------------
# Scope-aware closure (issue #159)
# ---------------------------------------------------------------------------


@pytest.fixture
def broad_scope_plan(job: Job) -> Plan:
    p = Plan(
        version=1,
        objective="Project 2025 implementation tracker",
        subgoals=[
            Subgoal(id=1, description="Identify core policy pillars", done=False),
            Subgoal(id=2, description="Map policies to federal departments", done=False),
            Subgoal(id=3, description="Collect legal challenges and pushback", done=False),
            Subgoal(id=4, description="Track public statements", done=False),
        ],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=10,
        scope_class="broad",
    )
    write_plan(job, p.model_dump())
    return p


def test_synthesize_broad_scope_context_includes_scope_class(
    job: Job, db_path: Path, broad_scope_plan: Plan
) -> None:
    """The synthesizer's context payload exposes the plan's scope_class.

    Without this, the prompt's scope-aware closure rules can't fire — the
    synthesizer would default to its old decisive behavior on broad goals
    and prematurely terminate overnight runs.
    """
    _seed_findings(job, [0.7])
    router = _StubRouter()

    asyncio.run(synthesize(job, broad_scope_plan, router=router))

    assert router.calls
    _tier, args, _kwargs = router.calls[0]
    payload = json.loads(args[0])
    assert payload.get("scope_class") == "broad"


def test_build_context_narrow_scope_renders_string(job: Job) -> None:
    narrow_plan = Plan(
        version=1,
        objective="x",
        subgoals=[Subgoal(id=1, description="x", done=False)],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=1,
        scope_class="narrow",
    )
    context_json = synth_module._build_context(
        goal="x",
        plan=narrow_plan,
        findings=[],
        sources={},
        prior=None,
        critique=None,
        followup_recipes="",
        paid_unblock_recipes="",
        final=False,
    )
    payload = json.loads(context_json)
    assert payload["scope_class"] == "narrow"


def test_build_context_missing_scope_class_renders_null(job: Job) -> None:
    plan = Plan(
        version=1,
        objective="x",
        subgoals=[Subgoal(id=1, description="x", done=False)],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=1,
    )
    context_json = synth_module._build_context(
        goal="x",
        plan=plan,
        findings=[],
        sources={},
        prior=None,
        critique=None,
        followup_recipes="",
        paid_unblock_recipes="",
        final=False,
    )
    payload = json.loads(context_json)
    assert payload["scope_class"] is None


def test_synthesize_broad_scope_corpus_remains_inconclusive(
    job: Job, db_path: Path, broad_scope_plan: Plan
) -> None:
    """Broad-scope subgoals reported as inconclusive remain done=False end-to-end.

    Wiring check: a synthesizer response declaring 3 of 4 broad-scope
    subgoals inconclusive must persist into the plan with those subgoals
    still open, so drain-replan can keep firing instead of terminating.
    """
    # Seed a 45-task-style corpus so the test mirrors the failure repro.
    _seed_findings(job, [0.7] * 45)

    body = '# Report\n\n## Sources\n1. https://x — "t"\n'
    fence = (
        "\n```json\n"
        '{"subgoal_status": {'
        '"1": "confirmed",'
        '"2": "inconclusive",'
        '"3": "inconclusive",'
        '"4": "inconclusive"'
        "}}"
        "\n```\n"
    )
    router = _StubRouter(content=body + fence)

    asyncio.run(synthesize(job, broad_scope_plan, router=router))

    subgoals = _read_latest_plan_subgoals(db_path, job.id)
    by_id = {sg["id"]: sg for sg in subgoals}
    # Inconclusive majority is preserved: 3 of 4 stay open.
    open_ids = [sid for sid, sg in by_id.items() if not sg["done"]]
    assert sorted(open_ids) == [2, 3, 4]
    assert by_id[1]["done"] is True

    events = _read_event_rows(db_path, job.id)
    updated = [e for e in events if e["kind"] == "plan_subgoals_updated"]
    assert len(updated) == 1
    payload = json.loads(updated[0]["payload_json"])
    assert sorted(payload["inconclusive"]) == [2, 3, 4]
    assert payload["closed"] == [1]


def test_synthesizer_prompt_has_scope_aware_closure_rules() -> None:
    """The synthesizer prompt must instruct the model to apply scope-aware gating."""
    body = prompts_loader.load_prompt("synthesizer", goal="x")
    assert "scope_class" in body
    assert "Scope-aware closure rules" in body
    # All three gates must be named so the model knows what to check.
    assert "5 distinct" in body
    assert "2 specific" in body
    assert "3 distinct" in body


def test_synthesizer_prompt_ends_with_status_trailer_instruction() -> None:
    """Keep the machine-readable trailer requirement at the end for recency."""
    body = prompts_loader.load_prompt("synthesizer", goal="x")
    assert "Final mandatory subgoal-status trailer" in body
    assert "MUST be emitted on every synthesis pass" in body
    assert body.rstrip().endswith("text after the closing fence.")


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
    body = '# Report\n\n## Sources\n1. https://x — "t"\n'
    fence = (
        "\n``` json\n"
        '{"subgoal_status": {"1": "confirmed", "2": "confirmed", "3": "confirmed"}}'
        "\n```\n"
    )
    router = _StubRouter(content=body + fence)

    asyncio.run(synthesize(job, multi_subgoal_plan, router=router))

    report = (job.root / "report.md").read_text(encoding="utf-8")
    assert "subgoal_status" not in report

    subgoals = _read_latest_plan_subgoals(db_path, job.id)
    assert all(sg["done"] is True for sg in subgoals)


# ---------------------------------------------------------------------------
# Sources-section reconciliation (issue #207)
# ---------------------------------------------------------------------------


def _seed_sources_with_finding(job: Job, n: int) -> list[int]:
    """Seed ``n`` sources + one finding citing all of them.

    Returns the list of source IDs (ascending). The single finding
    guarantees ``synth._load_sources_for`` picks up every seeded source so
    the reconciliation helper can resolve any of them by ID.
    """
    sids = [_seed_source(job, f"https://example.com/url-{i}", f"content {i}") for i in range(n)]
    write_finding(job, "claim citing many sources", 0.9, sids)
    return sids


def test_synthesize_reconciles_dropped_inline_citations(
    job: Job, db_path: Path, plan: Plan
) -> None:
    """A model output that cites ``[1][2][3, 5]`` but enumerates only ``1.`` and
    ``2.`` must end up with reconciled entries for ``3.`` and ``5.``.
    """
    sids = _seed_sources_with_finding(job, 5)
    # IDs are autoincrement on a fresh DB → sids == [1, 2, 3, 4, 5].
    assert sids == [1, 2, 3, 4, 5]

    body = (
        "# Investigation Report\n\n"
        "## Executive Summary\n"
        "- Finding A [1].\n"
        "- Finding B [2][3, 5].\n\n"
        "## Sources\n\n"
        '1. https://example.com/url-0 — "T0" (retrieved 2026-05-06)\n'
        '2. https://example.com/url-1 — "T1" (retrieved 2026-05-06)\n'
    )
    router = _StubRouter(content=body)

    asyncio.run(synthesize(job, plan, router=router))

    report = (job.root / "report.md").read_text(encoding="utf-8")
    # The model's enumerated lines survive verbatim.
    assert re.search(r"^1\. https://example\.com/url-0 — ", report, re.MULTILINE)
    assert re.search(r"^2\. https://example\.com/url-1 — ", report, re.MULTILINE)
    # Reconciliation appended canonical lines for the dropped IDs (3 and 5).
    assert re.search(
        r'^3\. https://example\.com/url-2 — "[^"]+" \(retrieved \d{4}-\d{2}-\d{2}\)',
        report,
        re.MULTILINE,
    )
    assert re.search(
        r'^5\. https://example\.com/url-4 — "[^"]+" \(retrieved \d{4}-\d{2}-\d{2}\)',
        report,
        re.MULTILINE,
    )
    # The persisted synthesis version must also include the reconciled lines.
    synth_md = (job.root / "synthesis/0001.md").read_text(encoding="utf-8")
    assert re.search(r"^3\. https://example\.com/url-2 — ", synth_md, re.MULTILINE)
    assert re.search(r"^5\. https://example\.com/url-4 — ", synth_md, re.MULTILINE)


def test_synthesize_emits_source_list_reconciled_event(job: Job, db_path: Path, plan: Plan) -> None:
    """A drop event records exactly the IDs the helper appended."""
    sids = _seed_sources_with_finding(job, 5)
    assert sids == [1, 2, 3, 4, 5]

    body = (
        "# Report\n\n"
        "- a [1].\n"
        "- b [2][3, 5].\n\n"
        "## Sources\n\n"
        '1. https://example.com/url-0 — "T0" (retrieved 2026-05-06)\n'
        '2. https://example.com/url-1 — "T1" (retrieved 2026-05-06)\n'
    )
    router = _StubRouter(content=body)

    asyncio.run(synthesize(job, plan, router=router))

    events = _read_event_rows(db_path, job.id)
    reconciled = [e for e in events if e["kind"] == "source_list_reconciled"]
    assert len(reconciled) == 1
    assert reconciled[0]["level"] == "INFO"
    payload = json.loads(reconciled[0]["payload_json"])
    assert payload["added"] == [3, 5]
    assert payload["unresolved"] == []
    assert payload["already_listed"] == 2
    assert payload["cited_total"] == 4


def test_synthesize_no_reconciliation_when_sources_already_complete(
    job: Job, db_path: Path, plan: Plan
) -> None:
    """When the model enumerated every cited ID, no event fires and the body
    is byte-for-byte unchanged below the heading.
    """
    sids = _seed_sources_with_finding(job, 3)
    assert sids == [1, 2, 3]

    body = (
        "# Report\n\n"
        "- a [1].\n"
        "- b [2][3].\n\n"
        "## Sources\n\n"
        '1. https://example.com/url-0 — "T0" (retrieved 2026-05-06)\n'
        '2. https://example.com/url-1 — "T1" (retrieved 2026-05-06)\n'
        '3. https://example.com/url-2 — "T2" (retrieved 2026-05-06)\n'
    )
    router = _StubRouter(content=body)

    asyncio.run(synthesize(job, plan, router=router))

    events = _read_event_rows(db_path, job.id)
    reconciled = [e for e in events if e["kind"] == "source_list_reconciled"]
    assert reconciled == []

    report = (job.root / "report.md").read_text(encoding="utf-8")
    # Sources section should contain exactly the three model-emitted lines —
    # no duplicates from a stray reconciliation pass.
    assert report.count("https://example.com/url-0") == 1
    assert report.count("https://example.com/url-1") == 1
    assert report.count("https://example.com/url-2") == 1


def test_synthesize_unresolved_inline_citation_logs_without_appending(
    job: Job, db_path: Path, plan: Plan
) -> None:
    """A body cite for an ID that doesn't exist in the source dict logs as
    ``unresolved`` and does NOT inject a bogus ``999.`` line.
    """
    sids = _seed_sources_with_finding(job, 2)
    assert sids == [1, 2]

    body = (
        "# Report\n\n"
        "- a [1].\n"
        "- b [999].\n\n"
        "## Sources\n\n"
        '1. https://example.com/url-0 — "T0" (retrieved 2026-05-06)\n'
    )
    router = _StubRouter(content=body)

    asyncio.run(synthesize(job, plan, router=router))

    report = (job.root / "report.md").read_text(encoding="utf-8")
    # Reconciliation must not invent a 999. line.
    assert not re.search(r"^999\. ", report, re.MULTILINE)

    events = _read_event_rows(db_path, job.id)
    reconciled = [e for e in events if e["kind"] == "source_list_reconciled"]
    assert len(reconciled) == 1
    payload = json.loads(reconciled[0]["payload_json"])
    assert payload["added"] == []
    assert payload["unresolved"] == [999]


def test_synthesizer_prompt_requires_sources_union_rule() -> None:
    """The synthesizer prompt must instruct the model to emit the union of
    inline-cited IDs in the Sources section (issue #207)."""
    body = prompts_loader.load_prompt("synthesizer", goal="x")
    # The new rule names the union explicitly so the model knows partial
    # lists are not acceptable.
    assert "union" in body.lower()
    assert "Sources" in body


# ---------------------------------------------------------------------------
# Departmental Policy Tracker (issue #208)
# ---------------------------------------------------------------------------


def test_compute_department_coverage_aliases_and_ranking() -> None:
    """Aliases collapse to canonicals; result is ranked high→low by count."""
    findings: list[dict[str, Any]] = [
        # DOJ via 'DOJ' and 'Justice Department' — counts once for the finding.
        {"claim": "DOJ filed suit. The Justice Department announced changes."},
        # DOJ via 'Justice' alias.
        {"claim": "Justice issued new guidance to prosecutors."},
        # HHS via FDA + Health and Human Services — counts once.
        {"claim": "HHS and FDA approved the policy across Health and Human Services."},
        # HHS via Health Department.
        {"claim": "The Department of Health changed Medicare rules."},
        # DOD: 'Department of Defense' + 'Pentagon' + 'DOD'.
        {"claim": "DOD reorganization at the Pentagon was confirmed by Department of Defense."},
        # DHS.
        {"claim": "Department of Homeland Security raised the alert level."},
        # OPM via 'Office of Personnel Management' + 'Personnel'.
        {"claim": "OPM rolled out new federal Personnel rules."},
        # Education via 'Department of Education'.
        {"claim": "Department of Education announced new Title IX guidance."},
        # No federal department mention.
        {"claim": "A private company released a quarterly report."},
    ]

    coverage = synth_module._compute_department_coverage(findings)
    by_dept = {item["department"]: item["count"] for item in coverage}

    # Aliases collapsed correctly (DOJ counts == 2, HHS == 2, others == 1 each).
    assert by_dept["DOJ"] == 2
    assert by_dept["HHS"] == 2
    assert by_dept["DOD"] == 1
    assert by_dept["DHS"] == 1
    assert by_dept["OPM"] == 1
    assert by_dept["Education"] == 1

    # No false-positive entries from the unrelated finding.
    assert "Treasury" not in by_dept
    assert "EPA" not in by_dept

    # Ranked high→low by count; tied departments use canonical name as
    # stable tiebreaker (alphabetical: DOJ before HHS).
    counts = [item["count"] for item in coverage]
    assert counts == sorted(counts, reverse=True)
    top_two = [item["department"] for item in coverage[:2]]
    assert top_two == ["DOJ", "HHS"]


def test_compute_department_coverage_empty_findings_returns_empty_list() -> None:
    """No findings → empty list (the prompt uses this to omit the section)."""
    assert synth_module._compute_department_coverage([]) == []


def test_compute_department_coverage_handles_missing_or_non_string_claims() -> None:
    """Findings with missing/empty/non-string claim are skipped without erroring."""
    findings: list[dict[str, Any]] = [
        {"claim": "DOJ acted."},
        {"claim": ""},
        {"claim": None},
        {},  # no 'claim' key at all
    ]
    coverage = synth_module._compute_department_coverage(findings)
    assert coverage == [{"department": "DOJ", "count": 1}]


def test_build_context_exposes_department_coverage(job: Job) -> None:
    """``_build_context`` renders ``department_coverage`` as an ordered list
    with the expected canonical/count pairs for a multi-department fixture."""
    plan_obj = Plan(
        version=1,
        objective="Project 2025 implementation tracker",
        subgoals=[Subgoal(id=1, description="map departments", done=False)],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=1,
    )
    findings: list[dict[str, Any]] = [
        {"id": 1, "claim": "DOJ filed three lawsuits.", "confidence": 0.9, "source_ids": []},
        {"id": 2, "claim": "Justice Department reorganized.", "confidence": 0.8, "source_ids": []},
        {"id": 3, "claim": "DOJ briefed Congress.", "confidence": 0.7, "source_ids": []},
        {"id": 4, "claim": "HHS announced new rules.", "confidence": 0.6, "source_ids": []},
        {"id": 5, "claim": "FDA approved a vaccine.", "confidence": 0.5, "source_ids": []},
        {"id": 6, "claim": "EPA cut regulations.", "confidence": 0.4, "source_ids": []},
    ]

    context_json = synth_module._build_context(
        goal="x",
        plan=plan_obj,
        findings=findings,
        sources={},
        prior=None,
        critique=None,
        followup_recipes="",
        paid_unblock_recipes="",
        final=False,
    )
    payload = json.loads(context_json)

    coverage = payload["department_coverage"]
    assert isinstance(coverage, list)
    # Order is high→low by count: DOJ(3), HHS(2), EPA(1).
    assert coverage == [
        {"department": "DOJ", "count": 3},
        {"department": "HHS", "count": 2},
        {"department": "EPA", "count": 1},
    ]


def test_build_context_empty_findings_emits_empty_department_coverage(job: Job) -> None:
    """Key is always present; an empty list signals the prompt to omit the section."""
    plan_obj = Plan(
        version=1,
        objective="x",
        subgoals=[Subgoal(id=1, description="x", done=False)],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=1,
    )
    context_json = synth_module._build_context(
        goal="x",
        plan=plan_obj,
        findings=[],
        sources={},
        prior=None,
        critique=None,
        followup_recipes="",
        paid_unblock_recipes="",
        final=False,
    )
    payload = json.loads(context_json)
    assert payload["department_coverage"] == []


def test_synthesizer_prompt_requires_departmental_policy_tracker() -> None:
    """The synthesizer prompt must drive the tracker off ``department_coverage``
    rather than a fixed 4–5-section template (issue #208)."""
    body = prompts_loader.load_prompt("synthesizer", goal="x")
    # The structural-hint input is documented.
    assert "department_coverage" in body
    # The new section is named explicitly.
    assert "Departmental Policy Tracker" in body
    # Ranking is by count, not by template.
    assert "high→low" in body or "high→low" in body
    # The ≥1-finding inclusion rule is present (or the equivalent "do not
    # omit" instruction).
    assert "do not omit" in body.lower() or "every entry" in body.lower()
    # The ≥3-findings → subsection threshold is named.
    assert "≥3" in body or "≥3" in body
