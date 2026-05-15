"""Tests for the enumeration coverage ledger."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from research_agent.storage import coverage, db
from research_agent.storage.jobs import Job


def _job(tmp_path: Path) -> Job:
    db_path = tmp_path / "index.sqlite"
    db.migrate(path=db_path).close()
    return Job.create(
        {
            "goal": "Enumerate every candidate",
            "domain": "political",
            "enumeration": {"required": True},
        },
        jobs_root=tmp_path / "jobs",
        db_path=db_path,
        today=date(2026, 5, 15),
    )


def test_declare_coverage_writes_sqlite_and_sidecar(tmp_path: Path) -> None:
    job = _job(tmp_path)

    units = coverage.declare_coverage(
        job,
        [
            {
                "state": "CA",
                "chamber": "House",
                "district_or_seat": "01",
                "source_type": "fec-filed",
            },
            {
                "state": "TX",
                "chamber": "Senate",
                "district_or_seat": "Class I",
                "source_type": "state-ballot-qualified",
            },
        ],
    )

    assert [unit.status for unit in units] == ["pending", "pending"]
    assert coverage.is_coverage_complete(job) is False

    sidecar = json.loads((job.root / "coverage.json").read_text(encoding="utf-8"))
    assert sidecar["job_id"] == job.id
    assert len(sidecar["units"]) == 2
    assert not (job.root / "coverage.json.tmp").exists()

    rows = coverage.list_units(job)
    assert rows[0].dimensions["state"] == "CA"
    assert rows[1].dimensions["state"] == "TX"


def test_upsert_status_preserves_attempts_and_unblocker(tmp_path: Path) -> None:
    job = _job(tmp_path)
    [unit] = coverage.declare_coverage(
        job,
        [{"state": "NC", "chamber": "House", "source_type": "state-ballot-qualified"}],
    )

    updated = coverage.upsert_unit_status(
        job,
        unit.dim_key,
        "confirmed_gap",
        attempt={
            "task_id": 42,
            "task_kind": "state_election_search",
            "status": "failed",
            "reason": "portal has not published 2026 filings",
        },
        unblocker="Check NC SBE after the candidate filing deadline",
    )

    assert updated.status == "confirmed_gap"
    assert updated.unblocker == "Check NC SBE after the candidate filing deadline"
    assert coverage.is_coverage_complete(job) is True

    loaded = coverage.list_units(job)[0]
    assert loaded.recent_attempts[0].task_id == 42
    assert loaded.recent_attempts[0].reason == "portal has not published 2026 filings"


def test_intake_dimension_shape_expands_to_units(tmp_path: Path) -> None:
    job = _job(tmp_path)
    job.intake["enumeration"] = {
        "required": True,
        "units": {
            "state": ["CA", "TX", "FL"],
            "chamber": ["House"],
            "source_type": ["fec-filed"],
        },
    }

    units = coverage.declare_from_intake(job)

    assert len(units) == 3
    assert {unit.dimensions["state"] for unit in units} == {"CA", "TX", "FL"}
