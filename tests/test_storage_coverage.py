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


# ---------------------------------------------------------------------------
# Corpus dossier coverage (issue #356)
# ---------------------------------------------------------------------------


def _dossier_job(tmp_path: Path) -> Job:
    db_path = tmp_path / "index.sqlite"
    db.migrate(path=db_path).close()
    return Job.create(
        {
            "goal": "Exhaustive dossier of UFO records",
            "domain": "general",
            "corpus_dossier": True,
        },
        jobs_root=tmp_path / "jobs",
        db_path=db_path,
        today=date(2026, 5, 19),
    )


def _seed_page_source(
    job: Job, *, parent_file: str, page_no: int | None, page_chunk: int | None
) -> None:
    """Write a Source row + sidecar mimicking dossier-mode per-page ingest."""
    from research_agent.storage.sources import write_source

    body = f"{parent_file} page {page_no} chunk {page_chunk} body content"
    write_source(
        job,
        url=parent_file,
        title=Path(parent_file).name,
        raw_content=body,
        kind="local",
        metadata={
            "parent_file": parent_file,
            "page_no": page_no,
            "page_chunk": page_chunk,
        },
    )


def test_declare_corpus_units_emits_one_unit_per_page(tmp_path: Path) -> None:
    """5 PDFs × 6 pages each → 30 page units, all pending after declaration."""
    job = _dossier_job(tmp_path)
    pdf_uris = [f"file:///pdf{i}.pdf" for i in range(1, 6)]
    for uri in pdf_uris:
        for page_no in range(1, 7):
            _seed_page_source(job, parent_file=uri, page_no=page_no, page_chunk=None)

    units = coverage.declare_corpus_units(job)

    assert len(units) == 30
    assert all(unit.status == "pending" for unit in units)
    keys = {(u.dimensions["file"], u.dimensions["page"]) for u in units}
    expected = {(uri, str(p)) for uri in pdf_uris for p in range(1, 7)}
    # dim_key normalises ints to strings; just confirm the (file, page) pairs.
    pairs = {(u.dimensions["file"], u.dimensions["page"]) for u in units}
    assert {(uri, str(p)) for uri, p in pairs} == expected
    assert len(keys) == 30


def test_declare_corpus_units_is_idempotent(tmp_path: Path) -> None:
    """Re-running declare_corpus_units preserves statuses + does not duplicate."""
    job = _dossier_job(tmp_path)
    parent = "file:///doc.pdf"
    for page_no in (1, 2, 3):
        _seed_page_source(job, parent_file=parent, page_no=page_no, page_chunk=None)

    first = coverage.declare_corpus_units(job)
    assert len(first) == 3

    middle = next(u for u in first if u.dimensions["page"] == "2")
    coverage.upsert_unit_status(job, middle.dim_key, "complete")

    second = coverage.declare_corpus_units(job)
    assert len(second) == 3
    statuses = {u.dimensions["page"]: u.status for u in second}
    assert statuses == {"1": "pending", "2": "complete", "3": "pending"}


def test_declare_corpus_units_skips_legacy_sources_without_metadata(
    tmp_path: Path,
) -> None:
    """Sources without parent_file metadata (legacy thematic ingests) are skipped."""
    from research_agent.storage.sources import write_source

    job = _dossier_job(tmp_path)
    _seed_page_source(
        job, parent_file="file:///dossier.pdf", page_no=1, page_chunk=None
    )
    write_source(
        job,
        url="file:///legacy.pdf",
        title="legacy.pdf",
        raw_content="legacy body without metadata",
        kind="local",
    )

    units = coverage.declare_corpus_units(job)

    files = {u.dimensions.get("file") for u in units}
    assert files == {"file:///dossier.pdf"}
    assert len(units) == 1


def test_declare_corpus_units_blocks_is_coverage_complete(tmp_path: Path) -> None:
    """A dossier job with open page units is NOT coverage-complete."""
    job = _dossier_job(tmp_path)
    for page_no in (1, 2, 3):
        _seed_page_source(
            job, parent_file="file:///x.pdf", page_no=page_no, page_chunk=None
        )

    coverage.declare_corpus_units(job)

    assert coverage.is_coverage_complete(job) is False


def test_declare_corpus_units_handles_non_paginated_chunks(tmp_path: Path) -> None:
    """HTML/MD sources stamp page=None and still register as units."""
    job = _dossier_job(tmp_path)
    _seed_page_source(
        job, parent_file="file:///notes.html", page_no=None, page_chunk=None
    )
    _seed_page_source(
        job, parent_file="file:///notes.md", page_no=None, page_chunk=None
    )

    units = coverage.declare_corpus_units(job)

    files = {u.dimensions["file"] for u in units}
    assert files == {"file:///notes.html", "file:///notes.md"}
    for unit in units:
        assert "page" not in unit.dimensions


def test_file_status_rolls_up_page_units(tmp_path: Path) -> None:
    """File status derives from the union of page-unit statuses."""
    job = _dossier_job(tmp_path)
    pdf = "file:///rollup.pdf"
    for page_no in (1, 2, 3, 4):
        _seed_page_source(job, parent_file=pdf, page_no=page_no, page_chunk=None)
    units = coverage.declare_corpus_units(job)
    keyed = {u.dimensions["page"]: u for u in units}

    assert coverage.file_status(job, pdf) == "pending"

    coverage.upsert_unit_status(job, keyed["1"].dim_key, "complete")
    coverage.upsert_unit_status(job, keyed["2"].dim_key, "complete")
    assert coverage.file_status(job, pdf) == "in_progress"

    coverage.upsert_unit_status(job, keyed["3"].dim_key, "complete")
    coverage.upsert_unit_status(job, keyed["4"].dim_key, "complete")
    assert coverage.file_status(job, pdf) == "complete"


def test_file_status_confirmed_gap_when_all_pages_gap(tmp_path: Path) -> None:
    job = _dossier_job(tmp_path)
    pdf = "file:///allgap.pdf"
    for page_no in (1, 2):
        _seed_page_source(job, parent_file=pdf, page_no=page_no, page_chunk=None)
    units = coverage.declare_corpus_units(job)
    for unit in units:
        coverage.upsert_unit_status(job, unit.dim_key, "confirmed_gap")

    assert coverage.file_status(job, pdf) == "confirmed_gap"


def test_file_status_mixed_terminal_is_complete(tmp_path: Path) -> None:
    """Some pages complete, others confirmed_gap → file is 'complete' overall."""
    job = _dossier_job(tmp_path)
    pdf = "file:///mixed.pdf"
    for page_no in (1, 2, 3):
        _seed_page_source(job, parent_file=pdf, page_no=page_no, page_chunk=None)
    units = coverage.declare_corpus_units(job)
    keyed = {u.dimensions["page"]: u for u in units}
    coverage.upsert_unit_status(job, keyed["1"].dim_key, "complete")
    coverage.upsert_unit_status(job, keyed["2"].dim_key, "complete")
    coverage.upsert_unit_status(job, keyed["3"].dim_key, "confirmed_gap")

    assert coverage.file_status(job, pdf) == "complete"


def test_file_status_unknown_file_is_pending(tmp_path: Path) -> None:
    job = _dossier_job(tmp_path)
    _seed_page_source(
        job, parent_file="file:///known.pdf", page_no=1, page_chunk=None
    )
    coverage.declare_corpus_units(job)

    assert coverage.file_status(job, "file:///missing.pdf") == "pending"


def test_file_status_rejects_empty_url(tmp_path: Path) -> None:
    import pytest

    job = _dossier_job(tmp_path)
    with pytest.raises(ValueError):
        coverage.file_status(job, "")


def test_goal_complete_gated_by_dossier_page_units(tmp_path: Path) -> None:
    """_is_goal_complete must return False while any page unit is open."""
    from research_agent.orchestrator.loop import _is_goal_complete
    from research_agent.orchestrator.plan import Plan, Subgoal, TaskSpec

    job = _dossier_job(tmp_path)
    for page_no in (1, 2):
        _seed_page_source(
            job, parent_file="file:///gate.pdf", page_no=page_no, page_chunk=None
        )
    units = coverage.declare_corpus_units(job)
    assert len(units) == 2

    # Minimal valid plan whose subgoals are all done — without coverage
    # gating, plan.is_complete() would return True. Coverage must hold
    # _is_goal_complete at False.
    plan = Plan(
        version=1,
        objective="Exhaustive dossier of UFO records",
        subgoals=[
            Subgoal(id=1, description="ingest all pages", done=True),
        ],
        task_template=[
            TaskSpec(kind="local_corpus_query", payload={"query": "ingest pages"}),
        ],
        expected_iterations=1,
    )

    assert _is_goal_complete(job, plan) is False

    # Flip every page unit terminal-complete → coverage closes, plan
    # is already done, _is_goal_complete is now True.
    for unit in coverage.list_units(job):
        coverage.upsert_unit_status(job, unit.dim_key, "complete")

    assert _is_goal_complete(job, plan) is True


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
