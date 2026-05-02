"""Tests for `research_agent.storage.tasks`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_agent.orchestrator.plan import TaskSpec
from research_agent.storage import db
from research_agent.storage.jobs import Job
from research_agent.storage.tasks import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    enqueue,
    mark_done,
    mark_failed,
    mark_running,
    next_pending,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def jobs_root(tmp_path: Path) -> Path:
    return tmp_path / "jobs"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.sqlite"
    db.migrate(path=path).close()
    return path


@pytest.fixture
def job(jobs_root: Path, db_path: Path) -> Job:
    return Job.create(
        {"goal": "Investigate Widget Co"},
        jobs_root=jobs_root,
        db_path=db_path,
    )


def _specs(n: int) -> list[TaskSpec]:
    kinds = [
        "web_search",
        "web_fetch",
        "arxiv_search",
        "github_search",
        "news_search",
    ]
    return [
        TaskSpec(kind=kinds[i % len(kinds)], payload={"i": i, "q": f"query-{i}"})  # type: ignore[arg-type]
        for i in range(n)
    ]


def _all_rows(db_path: Path, job_id: str) -> list[dict]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------


def test_enqueue_inserts_rows(job: Job, db_path: Path) -> None:
    specs = _specs(5)
    ids = enqueue(job, specs, plan_version=1)

    assert len(ids) == 5
    assert ids == sorted(ids), "rowids should come back in submission order"

    rows = _all_rows(db_path, job.id)
    assert len(rows) == 5
    for row, spec in zip(rows, specs, strict=True):
        assert row["status"] == STATUS_PENDING
        assert row["kind"] == spec.kind
        assert row["plan_version"] == 1
        assert row["retry_count"] == 0
        assert row["started_at"] is None
        assert row["finished_at"] is None
        assert json.loads(row["payload_json"]) == spec.payload


def test_enqueue_rejects_empty_list(job: Job) -> None:
    with pytest.raises(ValueError):
        enqueue(job, [], plan_version=1)


def test_enqueue_rejects_non_taskspec_items(job: Job) -> None:
    with pytest.raises(TypeError):
        enqueue(job, [{"kind": "web_search"}], plan_version=1)  # type: ignore[list-item]


def test_enqueue_rejects_invalid_plan_version(job: Job) -> None:
    with pytest.raises(ValueError):
        enqueue(job, _specs(1), plan_version=0)
    with pytest.raises(ValueError):
        enqueue(job, _specs(1), plan_version=-1)


# ---------------------------------------------------------------------------
# next_pending
# ---------------------------------------------------------------------------


def test_next_pending_returns_oldest_first(job: Job, db_path: Path) -> None:
    specs = _specs(5)
    ids = enqueue(job, specs, plan_version=1)

    seen: list[int] = []
    for _ in range(5):
        row = next_pending(job)
        assert row is not None
        seen.append(row["id"])
        # Round-trip the payload.
        assert isinstance(row["payload"], dict)
        assert row["payload"]["i"] == ids.index(row["id"])
        assert row["status"] == STATUS_PENDING
        mark_done(row["id"], {"ok": True}, db_path=db_path)

    assert seen == ids
    assert next_pending(job) is None


def test_next_pending_returns_none_when_empty(job: Job) -> None:
    assert next_pending(job) is None


def test_next_pending_skips_non_pending(job: Job, db_path: Path) -> None:
    ids = enqueue(job, _specs(3), plan_version=1)
    # row 0 -> done, row 1 -> running, row 2 -> failed (then back to pending? no, failed)
    mark_done(ids[0], None, db_path=db_path)
    mark_running(ids[1], db_path=db_path)
    mark_failed(ids[2], "boom", db_path=db_path)

    assert next_pending(job) is None

    # Now enqueue a fresh pending row; it must be picked up.
    new_ids = enqueue(job, _specs(1), plan_version=1)
    row = next_pending(job)
    assert row is not None
    assert row["id"] == new_ids[0]


# ---------------------------------------------------------------------------
# mark_running
# ---------------------------------------------------------------------------


def test_mark_running_only_advances_pending_rows(job: Job, db_path: Path) -> None:
    [task_id] = enqueue(job, _specs(1), plan_version=1)
    mark_running(task_id, db_path=db_path)

    rows = _all_rows(db_path, job.id)
    assert rows[0]["status"] == STATUS_RUNNING
    assert rows[0]["started_at"] is not None

    # Calling again must not re-stamp started_at (guarded by status='pending').
    started_first = rows[0]["started_at"]
    mark_running(task_id, db_path=db_path)
    rows2 = _all_rows(db_path, job.id)
    assert rows2[0]["started_at"] == started_first


# ---------------------------------------------------------------------------
# mark_done
# ---------------------------------------------------------------------------


def test_mark_done_is_atomic_and_sets_result(job: Job, db_path: Path) -> None:
    [task_id] = enqueue(job, _specs(1), plan_version=1)
    mark_done(task_id, {"hits": 3, "ok": True}, db_path=db_path)

    rows = _all_rows(db_path, job.id)
    assert rows[0]["status"] == STATUS_DONE
    assert rows[0]["finished_at"] is not None
    assert json.loads(rows[0]["result_json"]) == {"hits": 3, "ok": True}


def test_mark_done_accepts_none_result(job: Job, db_path: Path) -> None:
    [task_id] = enqueue(job, _specs(1), plan_version=1)
    mark_done(task_id, None, db_path=db_path)

    rows = _all_rows(db_path, job.id)
    assert rows[0]["status"] == STATUS_DONE
    assert rows[0]["result_json"] is None


# ---------------------------------------------------------------------------
# mark_failed
# ---------------------------------------------------------------------------


def test_mark_failed_increments_retry_and_sets_error(job: Job, db_path: Path) -> None:
    [task_id] = enqueue(job, _specs(1), plan_version=1)

    mark_failed(task_id, "first failure", db_path=db_path)
    rows = _all_rows(db_path, job.id)
    assert rows[0]["status"] == STATUS_FAILED
    assert rows[0]["error"] == "first failure"
    assert rows[0]["retry_count"] == 1
    assert rows[0]["finished_at"] is not None

    mark_failed(task_id, "second failure", db_path=db_path)
    rows = _all_rows(db_path, job.id)
    assert rows[0]["error"] == "second failure"
    assert rows[0]["retry_count"] == 2
