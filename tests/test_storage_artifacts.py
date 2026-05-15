"""Tests for structured table artifacts."""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from research_agent.storage import artifacts, db
from research_agent.storage.jobs import Job


def _job(tmp_path: Path) -> Job:
    db_path = tmp_path / "index.sqlite"
    db.migrate(path=db_path).close()
    return Job.create(
        {"goal": "Build candidate roster"},
        jobs_root=tmp_path / "jobs",
        db_path=db_path,
        today=date(2026, 5, 15),
    )


def test_write_table_artifact_writes_schema_jsonl_csv_and_meta(tmp_path: Path) -> None:
    job = _job(tmp_path)
    rows = [
        {
            "state": "CA",
            "chamber": "House",
            "district_or_seat": "1",
            "candidate_name": "Jane Doe",
            "source_url": "https://example.com/ca",
        },
        {
            "state": "NV",
            "chamber": "Senate",
            "candidate_name": "John Smith",
            "source_url": "https://example.com/nv",
            "party": "Independent",
        },
    ]

    csv_path = artifacts.write_table_artifact(
        job,
        "candidates",
        schema=artifacts.CANDIDATE_ROSTER_SCHEMA,
        rows=rows,
        source_coverage="2 states",
    )

    artifact_dir = job.root / "artifacts"
    assert csv_path == artifact_dir / "candidates.csv"
    assert (artifact_dir / "candidates.schema.json").exists()
    assert (artifact_dir / "candidates.jsonl").exists()
    assert (artifact_dir / "candidates.meta.json").exists()
    assert list(artifact_dir.glob("*.tmp")) == []

    schema = json.loads((artifact_dir / "candidates.schema.json").read_text(encoding="utf-8"))
    assert schema["name"] == "candidates"
    assert [column["name"] for column in schema["columns"]][:4] == [
        "state",
        "chamber",
        "district_or_seat",
        "candidate_name",
    ]

    csv_rows = list(csv.DictReader((artifact_dir / "candidates.csv").open()))
    assert csv_rows[0]["party"] == ""
    assert csv_rows[1]["party"] == "Independent"
    assert "official_campaign_website" in csv_rows[0]

    meta = json.loads((artifact_dir / "candidates.meta.json").read_text(encoding="utf-8"))
    assert meta["artifact_name"] == "candidates"
    assert meta["row_count"] == 2
    assert meta["source_job_id"] == job.id
    assert meta["source_coverage"] == "2 states"


def test_read_and_list_artifacts_round_trip(tmp_path: Path) -> None:
    job = _job(tmp_path)
    rows = [{"state": "CA", "chamber": "House", "candidate_name": "Jane", "source_url": "u"}]
    artifacts.write_table_artifact(
        job,
        "candidates",
        schema=artifacts.CANDIDATE_ROSTER_SCHEMA,
        rows=rows,
    )

    schema, loaded_rows = artifacts.read_artifact(job, "candidates")
    listed = artifacts.list_artifacts(job)

    assert schema.name == "candidates"
    assert loaded_rows == rows
    assert listed[0]["name"] == "candidates"
    assert listed[0]["row_count"] == 1
    assert listed[0]["csv_path"] == "artifacts/candidates.csv"
