"""Smoke check for the public ``research_agent`` import surface."""

from __future__ import annotations

import inspect
from pathlib import Path

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

FUNCTIONS = [
    start_job,
    get_job_status,
    list_jobs,
    stop_job,
    resume_job,
    get_report,
    get_findings,
    search_findings,
    export_job,
]

SAMPLE_JOB_ID = "2026-05-16-investigate-widget-co-financials"
FIXTURE_JOBS_ROOT = Path("tests/fixtures/jobs")


def main() -> None:
    for fn in FUNCTIONS:
        assert callable(fn), f"{fn!r} is not callable"
        sig = inspect.signature(fn)
        assert sig.return_annotation is not inspect.Signature.empty, fn.__name__
        assert fn.__doc__ and "Example:" in fn.__doc__, fn.__name__

    status = get_job_status(SAMPLE_JOB_ID, jobs_root=FIXTURE_JOBS_ROOT)
    jobs = list_jobs(jobs_root=FIXTURE_JOBS_ROOT)
    report = get_report(SAMPLE_JOB_ID, jobs_root=FIXTURE_JOBS_ROOT)
    findings = get_findings(SAMPLE_JOB_ID, jobs_root=FIXTURE_JOBS_ROOT)
    hits = search_findings("Widget Co", job_id=SAMPLE_JOB_ID, jobs_root=FIXTURE_JOBS_ROOT)

    assert status.status == "completed"
    assert any(job.job_id == SAMPLE_JOB_ID for job in jobs)
    assert report.report_md
    assert findings
    assert hits
    print("OK programmatic API", SAMPLE_JOB_ID, len(findings), "findings")


if __name__ == "__main__":
    main()
