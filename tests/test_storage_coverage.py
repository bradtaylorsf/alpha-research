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


def test_fec_enum_row_matches_full_chamber_name_declaration(tmp_path: Path) -> None:
    """FEC enum emits office='H' + office_full='House'; declared coverage is
    'House'. The dimension extractor must prefer office_full so the FEC row
    actually marks the declared unit complete."""
    job = _job(tmp_path)
    [unit] = coverage.declare_coverage(
        job,
        [{"state": "NC", "chamber": "House", "source_type": "fec-filed"}],
    )

    task = {"id": 1, "kind": "fec_search", "payload": {"kind": "candidates_enumerate"}}
    result = {
        "results": [
            {
                "url": "https://www.fec.gov/data/candidate/H6NC01123/",
                "extras": {
                    "candidate_id": "H6NC01123",
                    "state": "NC",
                    "office": "H",
                    "office_full": "House",
                    "district_or_seat": "01",
                    "source_type": "fec-filed",
                },
            }
        ]
    }
    coverage.update_from_task_result(job, task, result)

    [reloaded] = coverage.list_units(job)
    assert reloaded.dim_key == unit.dim_key
    assert reloaded.status == "complete"


def test_state_election_row_carries_state_ballot_qualified_source_type(tmp_path: Path) -> None:
    """state_election rows must emit source_type='state-ballot-qualified' so
    declared coverage with that source_type is matched. File-format details
    belong on a different field."""
    job = _job(tmp_path)
    coverage.declare_coverage(
        job,
        [{"state": "MD", "chamber": "Senate", "source_type": "state-ballot-qualified"}],
    )

    task = {"id": 1, "kind": "state_election_search", "payload": {"state": "MD"}}
    result = {
        "results": [
            {
                "url": "https://elections.maryland.gov/...",
                "extras": {
                    "state": "MD",
                    "chamber": "Senate",
                    "source_type": "state-ballot-qualified",
                    "source_file_type": "html",
                    "candidate_name": "Robert Example",
                },
            }
        ]
    }
    coverage.update_from_task_result(job, task, result)

    [reloaded] = coverage.list_units(job)
    assert reloaded.status == "complete"


def test_update_from_extract_findings_marks_page_complete(tmp_path: Path) -> None:
    """Successful extract_findings closes the matching dossier page unit (#388)."""
    job = _dossier_job(tmp_path)
    parent = "file:///doc.pdf"
    _seed_page_source(job, parent_file=parent, page_no=2, page_chunk=None)
    coverage.declare_corpus_units(job)

    conn = db.connect(job.db_path)
    try:
        row = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()
    finally:
        conn.close()
    source_id = int(row["id"])

    task = {
        "id": 99,
        "kind": "extract_findings",
        "payload": {"source_id": source_id, "sub_question": "entities"},
    }
    coverage.update_from_task_result(job, task, {"findings_written": 3})

    units = coverage.list_units(job)
    assert len(units) == 1
    assert units[0].status == "complete"


def test_mark_task_failed_uses_source_id_for_dossier_page(tmp_path: Path) -> None:
    job = _dossier_job(tmp_path)
    parent = "file:///doc.pdf"
    _seed_page_source(job, parent_file=parent, page_no=1, page_chunk=None)
    coverage.declare_corpus_units(job)

    conn = db.connect(job.db_path)
    try:
        row = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()
    finally:
        conn.close()

    task = {
        "id": 100,
        "kind": "extract_findings",
        "payload": {"source_id": int(row["id"])},
    }
    coverage.mark_task_failed(job, task, "yaml parse failed")

    assert coverage.list_units(job)[0].status == "failed"
