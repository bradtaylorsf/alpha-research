"""Tests for the public job-folder contract readers."""

from __future__ import annotations

from pathlib import Path

from research_agent.contract import (
    Finding,
    JobMetadata,
    Report,
    iter_findings,
    read_job,
    read_report,
    read_source,
    tail_events,
)
from research_agent.observability.events import Event
from research_agent.tools.models import Source

FIXTURE_JOB = Path("tests/fixtures/jobs/sample")
SOURCE_SHA = "f3b84c259a337057de85f6ed9121ea598fd40bb27f4031735ac2d8de0b98ca26"


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_contract_readers_round_trip_fixture_job_without_mutation() -> None:
    before = _snapshot(FIXTURE_JOB)

    job = read_job(FIXTURE_JOB)
    findings = list(iter_findings(FIXTURE_JOB))
    report = read_report(FIXTURE_JOB)
    events = list(tail_events(FIXTURE_JOB))
    source = read_source(FIXTURE_JOB / "sources" / f"{SOURCE_SHA}.json")

    assert isinstance(job, JobMetadata)
    assert job.id == "2026-05-16-investigate-widget-co-financials"
    assert job.schema_version == 1
    assert job.status == "completed"

    assert findings and all(isinstance(item, Finding) for item in findings)
    assert findings[0].claim.startswith("Widget Co reported")
    assert findings[0].body_md.startswith("# Finding 000001")

    assert isinstance(report, Report)
    assert report.job_id == job.id
    assert report.report_md.startswith("# Report")
    assert report.sources

    assert events and all(isinstance(item, Event) for item in events)
    assert events[0].kind == "job_started"

    assert isinstance(source, Source)
    assert source.url == "https://example.com/widget-co-filing"
    assert source.source_kind == "web"
    assert "Primary source text" in source.cleaned_text
    assert source.metadata["lang"] == "en"

    assert _snapshot(FIXTURE_JOB) == before


def test_read_source_accepts_job_root_when_single_source_exists() -> None:
    source = read_source(FIXTURE_JOB)

    assert source.title == "Widget Co Sample Filing"
    assert source.metadata["pub_date"] == "2026-05-16"
