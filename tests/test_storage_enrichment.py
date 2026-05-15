"""Tests for CSV import and enrichment sidecars."""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from research_agent.storage import artifacts, db, enrichment
from research_agent.storage.jobs import Job


def _job(tmp_path: Path) -> Job:
    db_path = tmp_path / "index.sqlite"
    db.migrate(path=db_path).close()
    return Job.create(
        {"goal": "Enrich candidate CSV", "domain": "political"},
        jobs_root=tmp_path / "jobs",
        db_path=db_path,
        today=date(2026, 5, 15),
    )


def _fixture_csv(tmp_path: Path) -> Path:
    path = tmp_path / "candidates.csv"
    path.write_text(
        "candidate_id,candidate_name,website,status\n"
        "H1,Jane Example,,Filed\n"
        "H2,Robert Example,https://existing.example,Pending\n"
        "H3,Ana Example,,\n",
        encoding="utf-8",
    )
    return path


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_import_csv_as_artifact_preserves_rows_columns_and_provenance(tmp_path: Path) -> None:
    job = _job(tmp_path)
    source = _fixture_csv(tmp_path)

    out = enrichment.import_csv_as_artifact(
        job,
        source,
        artifact_name="candidates",
        key_columns=["candidate_id"],
    )

    assert out == job.root / "artifacts" / "candidates.csv"
    schema, rows = artifacts.read_artifact(job, "candidates")
    assert [column.name for column in schema.columns] == [
        "candidate_id",
        "candidate_name",
        "website",
        "status",
    ]
    assert [row["candidate_id"] for row in rows] == ["H1", "H2", "H3"]

    exported = list(csv.DictReader(out.open()))
    assert [row["candidate_id"] for row in exported] == ["H1", "H2", "H3"]
    meta = json.loads((job.root / "artifacts" / "candidates.meta.json").read_text())
    assert meta["key_columns"] == ["candidate_id"]
    assert meta["original_columns"] == ["candidate_id", "candidate_name", "website", "status"]

    provenance = _read_jsonl(job.root / "artifacts" / "candidates.provenance.jsonl")
    assert len(provenance) == 12
    assert provenance[0]["action"] == "imported"
    assert provenance[0]["source_kind"] == "operator_input"


def test_enrich_artifact_fills_empty_cells_and_preserves_non_empty_by_default(
    tmp_path: Path,
) -> None:
    job = _job(tmp_path)
    enrichment.import_csv_as_artifact(
        job,
        _fixture_csv(tmp_path),
        artifact_name="candidates",
        key_columns=["candidate_id"],
    )

    result = enrichment.enrich_artifact(
        job,
        "candidates",
        updates=[
            {
                "candidate_id": "H1",
                "values": {
                    "website": "https://jane.example",
                    "status": "Qualified",
                    "ballot_status": "qualified",
                },
                "source_url": "https://state.example/h1",
                "source_kind": "state_election",
                "confidence": 0.91,
                "task_id": 100,
            },
            {
                "candidate_id": "H2",
                "website": "https://new.example",
                "source_url": "https://state.example/h2",
                "source_kind": "state_election",
                "confidence": 0.75,
                "task_id": 101,
            },
        ],
    )

    assert result == {"changed": 2, "conflicts": 2, "rows": 3}
    schema, rows = artifacts.read_artifact(job, "candidates")
    assert [column.name for column in schema.columns] == [
        "candidate_id",
        "candidate_name",
        "website",
        "status",
        "ballot_status",
    ]
    assert rows[0]["website"] == "https://jane.example"
    assert rows[0]["status"] == "Filed"
    assert rows[0]["ballot_status"] == "qualified"
    assert rows[1]["website"] == "https://existing.example"
    meta = json.loads((job.root / "artifacts" / "candidates.meta.json").read_text())
    assert meta["original_columns"] == ["candidate_id", "candidate_name", "website", "status"]

    conflicts = _read_jsonl(job.root / "artifacts" / "candidates.conflicts.jsonl")
    assert len(conflicts) == 2
    assert {item["column"] for item in conflicts} == {"status", "website"}
    assert conflicts[0]["action"] == "review_needed"

    provenance = _read_jsonl(job.root / "artifacts" / "candidates.provenance.jsonl")
    filled = [item for item in provenance if item["action"] == "filled_empty"]
    assert {item["column"] for item in filled} == {"website", "ballot_status"}


def test_enrich_artifact_can_overwrite_non_empty_cells(tmp_path: Path) -> None:
    job = _job(tmp_path)
    enrichment.import_csv_as_artifact(
        job,
        _fixture_csv(tmp_path),
        artifact_name="candidates",
        key_columns=["candidate_id"],
    )

    result = enrichment.enrich_artifact(
        job,
        "candidates",
        updates=[
            {
                "candidate_id": "H2",
                "website": "https://replacement.example",
                "source_url": "https://state.example/h2",
                "source_kind": "state_election",
                "confidence": 0.8,
            }
        ],
        overwrite_non_empty=True,
    )

    _, rows = artifacts.read_artifact(job, "candidates")
    assert result["changed"] == 1
    assert result["conflicts"] == 0
    assert rows[1]["website"] == "https://replacement.example"
    provenance = _read_jsonl(job.root / "artifacts" / "candidates.provenance.jsonl")
    assert provenance[-1]["action"] == "overwrote_non_empty"


def test_read_artifact_with_provenance_round_trips(tmp_path: Path) -> None:
    job = _job(tmp_path)
    enrichment.import_csv_as_artifact(
        job,
        _fixture_csv(tmp_path),
        artifact_name="candidates",
        key_columns=["candidate_id"],
    )

    schema, rows, provenance = enrichment.read_artifact_with_provenance(job, "candidates")

    assert schema.name == "candidates"
    assert len(rows) == 3
    assert len(provenance) == 12
