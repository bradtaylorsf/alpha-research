"""Tests for the stable top-level programmatic API."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from research_agent import daemon as daemon_mod
from research_agent import (
    export_job,
    get_findings,
    get_job_status,
    get_job_status_detail,
    get_report,
    list_jobs,
    resume_job,
    search_findings,
    start_job,
    stop_job,
)
from research_agent.api import ExportResult, StartJobResult
from research_agent.errors import JobNotFound
from research_agent.storage import db
from research_agent.storage.jobs import Job
from research_agent.storage.markdown import write_finding
from research_agent.storage.sources import write_source

SAMPLE_JOB_ID = "2026-05-16-investigate-widget-co-financials"
FIXTURE_JOBS_ROOT = Path("tests/fixtures/jobs")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.sqlite"
    db.migrate(path=path).close()
    return path


def test_read_entry_points_work_against_fixture_job(tmp_path: Path) -> None:
    status = get_job_status(SAMPLE_JOB_ID, jobs_root=FIXTURE_JOBS_ROOT)
    status_detail = get_job_status_detail(SAMPLE_JOB_ID, jobs_root=FIXTURE_JOBS_ROOT)
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
    assert status_detail.goal == "Investigate Widget Co financials"
    assert status_detail.id == SAMPLE_JOB_ID
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
    monkeypatch.setattr(daemon_mod, "spawn_daemon", lambda _job_id, **_kw: 4242)
    monkeypatch.setattr(daemon_mod, "is_daemon_alive", lambda *_a, **_kw: False)

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


def test_start_job_local_passes_env_to_daemon_without_mutating_process(
    tmp_path: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs_root = tmp_path / "jobs"
    captured_env: dict[str, str] = {}

    def _spawn(_job_id: str, **kwargs) -> int:  # type: ignore[no-untyped-def]
        captured_env.update(kwargs.get("env") or {})
        return 4242

    monkeypatch.delenv("RESEARCH_MODELS_CONFIG", raising=False)
    monkeypatch.delenv("RESEARCH_DAEMON_SKIP_HEALTH_CHECKS", raising=False)
    monkeypatch.setattr(daemon_mod, "spawn_daemon", _spawn)
    monkeypatch.setattr(daemon_mod, "is_daemon_alive", lambda *_a, **_kw: False)

    started = start_job(
        "Investigate local API lifecycle",
        local=True,
        jobs_root=jobs_root,
        db_path=db_path,
    )

    assert started.daemon_pid == 4242
    assert captured_env["RESEARCH_MODELS_CONFIG"] == "config/models.local.yaml"
    assert captured_env["RESEARCH_DAEMON_SKIP_HEALTH_CHECKS"] == "1"
    assert "RESEARCH_MODELS_CONFIG" not in os.environ
    assert "RESEARCH_DAEMON_SKIP_HEALTH_CHECKS" not in os.environ


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


def test_list_jobs_scopes_db_rows_to_supplied_jobs_root(
    tmp_path: Path,
    db_path: Path,
) -> None:
    other_job = Job.create(
        {"goal": "Programmatic list job", "domain": "general"},
        jobs_root=tmp_path / "other-jobs",
        db_path=db_path,
    )

    rows = list_jobs(jobs_root=FIXTURE_JOBS_ROOT, db_path=db_path)

    assert any(row.job_id == SAMPLE_JOB_ID for row in rows)
    assert all(row.job_id != other_job.id for row in rows)


def test_entry_points_reject_invalid_job_id_before_folder_lookup(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "job.json").write_text("{}", encoding="utf-8")
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()

    with pytest.raises(JobNotFound):
        get_report("../outside", jobs_root=jobs_root)


def test_get_findings_maps_source_url_by_db_source_id(
    tmp_path: Path,
    db_path: Path,
) -> None:
    jobs_root = tmp_path / "jobs"
    job = Job.create(
        {"goal": "Programmatic finding source lookup", "domain": "general"},
        jobs_root=jobs_root,
        db_path=db_path,
    )
    first_source = write_source(
        job,
        url="https://example.com/first",
        title="First",
        raw_content="First source body",
        kind="web",
    )
    second_source = write_source(
        job,
        url="https://example.com/second",
        title="Second",
        raw_content="Second source body",
        kind="web",
    )
    write_finding(
        job,
        "The second source supports this finding.",
        0.9,
        source_ids=[second_source],
    )

    findings = get_findings(job.id, jobs_root=jobs_root, db_path=db_path)

    assert first_source != second_source
    assert findings[0].source_url == "https://example.com/second"
