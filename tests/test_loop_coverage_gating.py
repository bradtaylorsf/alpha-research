"""Loop completion gating for enumeration coverage."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from research_agent.orchestrator.loop import run_loop
from research_agent.orchestrator.plan import Plan, Subgoal
from research_agent.storage import coverage, db
from research_agent.storage.jobs import Job
from research_agent.storage.markdown import write_finding, write_plan
from research_agent.storage.sources import write_source


@pytest.mark.asyncio
async def test_enumeration_job_with_findings_and_pending_coverage_is_not_complete(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "index.sqlite"
    db.migrate(path=db_path).close()
    job = Job.create(
        {
            "goal": "Build every 2026 House and Senate candidate roster",
            "domain": "political",
            "enumeration": {"required": True},
        },
        jobs_root=tmp_path / "jobs",
        db_path=db_path,
        today=date(2026, 5, 15),
    )
    plan = Plan(
        version=1,
        objective="Build a complete roster",
        subgoals=[Subgoal(id=1, description="Extract candidate rows", done=True)],
        task_template=[],
        expected_iterations=1,
        scope_class="broad",
    )
    write_plan(job, plan.model_dump())
    coverage.declare_coverage(
        job,
        [
            {"state": "CA", "chamber": "House", "source_type": "fec-filed"},
            {"state": "TX", "chamber": "House", "source_type": "fec-filed"},
            {"state": "FL", "chamber": "Senate", "source_type": "fec-filed"},
            {"state": "NY", "chamber": "House", "source_type": "fec-filed"},
            {"state": "PA", "chamber": "House", "source_type": "fec-filed"},
        ],
    )
    for state in ("CA", "TX", "FL"):
        chamber = "Senate" if state == "FL" else "House"
        coverage.set_matching_units(
            job,
            {"state": state, "chamber": chamber, "source_type": "fec-filed"},
            "complete",
            attempt={"task_kind": "fec_candidates_search", "status": "done"},
        )
    source_id = write_source(
        job,
        url="https://example.test/candidates",
        title="Candidate fixture",
        raw_content="Three states have candidate rows.",
        kind="web",
    )
    write_finding(
        job,
        claim="Three states have candidate rows.",
        confidence=0.9,
        source_ids=[source_id],
        tags=["candidate-roster"],
    )

    result = await run_loop(
        job,
        router=None,
        plan=plan,
        handlers={},
        max_tasks=0,
        retry_waits=(0,),
    )

    assert result["completed"] is False
    assert result["completion_reason"] != "goal_complete"
    pending = coverage.blocking_units(job)
    assert {unit.dimensions["state"] for unit in pending} == {"NY", "PA"}


def test_confirmed_gap_coverage_feeds_confirmed_gaps_section(tmp_path: Path) -> None:
    from research_agent.orchestrator import synth as synth_module

    db_path = tmp_path / "index.sqlite"
    db.migrate(path=db_path).close()
    job = Job.create(
        {
            "goal": "Build every 2026 House and Senate candidate roster",
            "domain": "political",
            "enumeration": {"required": True},
        },
        jobs_root=tmp_path / "jobs",
        db_path=db_path,
        today=date(2026, 5, 15),
    )
    plan = Plan(
        version=1,
        objective="Build a complete roster",
        subgoals=[Subgoal(id=1, description="Extract candidate rows", done=True)],
        task_template=[],
        expected_iterations=1,
    )
    write_plan(job, plan.model_dump())
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

    gaps: list[dict[str, Any]] = synth_module._compute_confirmed_gaps(job, plan)

    assert gaps[0]["topic"] == unit.dim_key
    assert "Maryland SBE" in gaps[0]["suggested_unblocker"]
    assert gaps[0]["attempts"][0]["failure_reason"] == "2026 list not yet public"
