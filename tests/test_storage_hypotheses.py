"""Tests for the per-job hypothesis ledger."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from research_agent.storage import db, hypotheses
from research_agent.storage.jobs import Job


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.sqlite"
    db.migrate(path=path).close()
    return path


@pytest.fixture
def job(tmp_path: Path, db_path: Path) -> Job:
    return Job.create(
        {"goal": "Investigate hypothesis ledger"},
        jobs_root=tmp_path / "jobs",
        db_path=db_path,
        today=date(2026, 5, 15),
    )


def test_upsert_hypothesis_inserts_and_round_trips_json_lists(job: Job) -> None:
    hid = hypotheses.upsert_hypothesis(
        job,
        plan_version=1,
        statement="The delay is due to permitting friction.",
        confidence=0.6,
        supports=[1, "2"],
        refutes=[3],
        status="open",
    )

    rows = hypotheses.list_hypotheses(job)

    assert len(rows) == 1
    assert rows[0]["id"] == hid
    assert rows[0]["statement"] == "The delay is due to permitting friction."
    assert rows[0]["confidence"] == pytest.approx(0.6)
    assert rows[0]["supports"] == [1, 2]
    assert rows[0]["refutes"] == [3]
    assert rows[0]["status"] == "open"


def test_upsert_hypothesis_updates_existing_id(job: Job) -> None:
    hid = hypotheses.upsert_hypothesis(
        job,
        plan_version=1,
        statement="The contractor underbid.",
        confidence=0.4,
        supports=[],
        refutes=[],
        status="open",
    )

    same = hypotheses.upsert_hypothesis(
        job,
        id=hid,
        plan_version=2,
        statement="The contractor underbid and change orders slowed delivery.",
        confidence=0.7,
        supports=[10],
        refutes=[11, 12],
        status="inconclusive",
    )

    assert same == hid
    rows = hypotheses.list_hypotheses(job)
    assert len(rows) == 1
    assert rows[0]["plan_version"] == 2
    assert rows[0]["statement"] == "The contractor underbid and change orders slowed delivery."
    assert rows[0]["confidence"] == pytest.approx(0.7)
    assert rows[0]["supports"] == [10]
    assert rows[0]["refutes"] == [11, 12]
    assert rows[0]["status"] == "inconclusive"


def test_upsert_hypothesis_validates_confidence_and_status(job: Job) -> None:
    with pytest.raises(ValueError, match="confidence"):
        hypotheses.upsert_hypothesis(
            job,
            plan_version=1,
            statement="Bad confidence",
            confidence=1.2,
            supports=[],
            refutes=[],
            status="open",
        )

    with pytest.raises(ValueError, match="status"):
        hypotheses.upsert_hypothesis(
            job,
            plan_version=1,
            statement="Bad status",
            confidence=0.5,
            supports=[],
            refutes=[],
            status="maybe",
        )


def test_latest_for_job_returns_current_rows(job: Job) -> None:
    first = hypotheses.upsert_hypothesis(
        job,
        plan_version=1,
        statement="H1",
        confidence=0.2,
        supports=[],
        refutes=[],
        status="open",
    )
    second = hypotheses.upsert_hypothesis(
        job,
        plan_version=2,
        statement="H2",
        confidence=0.8,
        supports=[5],
        refutes=[],
        status="confirmed",
    )

    rows = hypotheses.latest_for_job(job)

    assert [row["id"] for row in rows] == [first, second]
