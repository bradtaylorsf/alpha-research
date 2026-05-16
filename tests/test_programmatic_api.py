"""Tests for the stable top-level programmatic API."""

from __future__ import annotations

from pathlib import Path

import pytest

from research_agent import (
    export_job,
    get_findings,
    get_job_status,
    get_report,
    list_jobs,
    resume_job,
    search_findings,
    start_job,
    stop_job,
)
from research_agent.api import ExportResult, StartJobResult
from research_agent.storage import db
from research_agent.storage.jobs import Job

SAMPLE_JOB_ID = "2026-05-16-investigate-widget-co-financials"
FIXTURE_JOBS_ROOT = Path("tests/fixtures/jobs")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.sqlite"
    db.migrate(path=path).close()
    return path


def test_read_entry_points_work_against_fixture_job(tmp_path: Path) -> None:
    status = get_job_status(SAMPLE_JOB_ID, jobs_root=FIXTURE_JOBS_ROOT)
    jobs = list_jobs(jobs_root=FIXTURE_JOBS_ROOT)
    report = get_report(SAMPLE_JOB_ID, jobs_root=FIXTURE_JOBS_ROOT)
    findings = get_findings(SAMPLE_JOB_ID, jobs_root=FIXTURE_JOBS_ROOT)
    hits = search_findings(
        "Widget Co",
        job_id=SAMPLE_JOB_ID,
        jobs_root=FIXTURE_JOBS_ROOT,
    )
    exported = export_job(
        SAMPLE_JOB_ID,
        md_bundle=True,
        out=tmp_path / "bundle.md",
        jobs_root=FIXTURE_JOBS_ROOT,
    )

    assert status.status == "completed"
    assert any(job.job_id == SAMPLE_JOB_ID for job in jobs)
    assert report.report_md.startswith("# Report")
    assert report.sources
    assert findings[0].citations == [1]
    assert hits and hits[0].job_id == SAMPLE_JOB_ID
    assert isinstance(exported, ExportResult)
    assert Path(exported.path).read_text(encoding="utf-8").startswith("# Report")
    assert exported.bytes > 0


def test_start_stop_resume_entry_points_use_plain_args(
    tmp_path: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs_root = tmp_path / "jobs"
    monkeypatch.setattr("research_agent.api.daemon.spawn_daemon", lambda _job_id, **_kw: 4242)
    monkeypatch.setattr("research_agent.api.daemon.is_daemon_alive", lambda *_a, **_kw: False)

    started = start_job(
        "Investigate API lifecycle",
        budget_usd=1.0,
        jobs_root=jobs_root,
        db_path=db_path,
    )
    assert isinstance(started, StartJobResult)
    assert started.daemon_pid == 4242

    stopped = stop_job(started.job_id, jobs_root=jobs_root, db_path=db_path)
    assert stopped.stopped is True
    assert (jobs_root / started.job_id / "STOP").exists()

    resumed = resume_job(started.job_id, jobs_root=jobs_root, db_path=db_path)
    assert resumed.resumed is True
    assert resumed.daemon_pid == 4242
    assert not (jobs_root / started.job_id / "STOP").exists()


def test_list_jobs_reads_db_rows(tmp_path: Path, db_path: Path) -> None:
    jobs_root = tmp_path / "jobs"
    job = Job.create(
        {"goal": "Programmatic list job", "domain": "general"},
        jobs_root=jobs_root,
        db_path=db_path,
    )

    rows = list_jobs(jobs_root=jobs_root, db_path=db_path)

    assert rows[0].job_id == job.id
    assert rows[0].goal == "Programmatic list job"
