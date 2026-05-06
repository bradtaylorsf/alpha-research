"""Task queue operations against the ``tasks`` table.

Implements the queue half of §10 of ``research-agent-implementation-guide.md``.
The orchestrator runs as a single daemon per job, so we deliberately do NOT
take a ``SELECT ... FOR UPDATE`` (SQLite has no row-level lock anyway) — the
single-writer assumption is the locking story. Atomicity for state
transitions comes from each write helper running its ``UPDATE`` inside a
``with conn:`` block (the implicit BEGIN/COMMIT pair).

Each helper opens its own connection via :func:`research_agent.storage.db.connect`
and closes it on exit; callers don't share a connection across operations.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from research_agent.orchestrator.plan import TaskSpec
from research_agent.storage import db
from research_agent.storage.jobs import Job

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


def _now_epoch() -> int:
    return int(time.time())


def enqueue(job: Job, task_specs: list[TaskSpec], plan_version: int) -> list[int]:
    """Insert one ``tasks`` row per spec; return rowids in submission order.

    All inserts run inside a single ``with conn:`` transaction so a partial
    enqueue cannot leave half a plan's worth of rows behind.
    """
    if not isinstance(plan_version, int) or plan_version < 1:
        raise ValueError(f"plan_version must be an int >= 1; got {plan_version!r}")
    if not isinstance(task_specs, list) or not task_specs:
        raise ValueError("task_specs must be a non-empty list of TaskSpec")
    for i, spec in enumerate(task_specs):
        if not isinstance(spec, TaskSpec):
            raise TypeError(f"task_specs[{i}] must be a TaskSpec; got {type(spec).__name__}")

    inserted: list[int] = []
    conn = db.connect(job.db_path)
    try:
        with conn:
            for spec in task_specs:
                cur = conn.execute(
                    """
                    INSERT INTO tasks (
                        job_id, plan_version, kind, payload_json, status, retry_count
                    ) VALUES (?, ?, ?, ?, ?, 0)
                    """,
                    (
                        job.id,
                        plan_version,
                        spec.kind,
                        json.dumps(spec.payload, sort_keys=True),
                        STATUS_PENDING,
                    ),
                )
                rowid = cur.lastrowid
                assert rowid is not None  # noqa: S101 — INTEGER PK rowid always set after INSERT
                inserted.append(int(rowid))
    finally:
        conn.close()

    return inserted


def next_pending(job: Job) -> dict | None:
    """Return the oldest pending task for ``job`` (by ``id`` ascending) or ``None``.

    Does NOT mutate state — the caller is responsible for transitioning the
    row to ``running`` via :func:`mark_running` once it commits to executing
    it. Keeping the read non-mutating means a daemon can peek without
    locking itself out of a clean shutdown.
    """
    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            """
            SELECT id, job_id, plan_version, kind, payload_json, status, retry_count
            FROM tasks
            WHERE job_id = ? AND status = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (job.id, STATUS_PENDING),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    return {
        "id": int(row["id"]),
        "job_id": row["job_id"],
        "plan_version": int(row["plan_version"]),
        "kind": row["kind"],
        "payload": json.loads(row["payload_json"]),
        "status": row["status"],
        "retry_count": int(row["retry_count"]),
    }


def mark_running(task_id: int, *, db_path: Path | str = db.DEFAULT_DB_PATH) -> None:
    """Atomically transition ``pending`` → ``running`` and stamp ``started_at``.

    The ``status='pending'`` guard in the WHERE clause prevents accidentally
    re-starting a task that another caller already advanced, which keeps the
    state machine honest even though the single-daemon assumption means
    contention shouldn't happen in practice.
    """
    now = _now_epoch()
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, started_at = ?
                WHERE id = ? AND status = ?
                """,
                (STATUS_RUNNING, now, task_id, STATUS_PENDING),
            )
    finally:
        conn.close()


def mark_done(
    task_id: int,
    result: dict | None,
    *,
    db_path: Path | str = db.DEFAULT_DB_PATH,
) -> None:
    """Mark a task done with optional ``result`` payload (single atomic UPDATE)."""
    now = _now_epoch()
    # ``default=str`` coerces unknown types (datetime, Path, sets) to their
    # str() form rather than crashing the daemon. Handlers should still
    # pre-serialize via ``model_dump(mode='json')`` for clean ISO strings.
    result_json = None if result is None else json.dumps(result, sort_keys=True, default=str)
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, finished_at = ?, result_json = ?
                WHERE id = ?
                """,
                (STATUS_DONE, now, result_json, task_id),
            )
    finally:
        conn.close()


def mark_failed(
    task_id: int,
    error: str,
    *,
    db_path: Path | str = db.DEFAULT_DB_PATH,
) -> None:
    """Mark a task failed, record the error, and bump ``retry_count`` atomically."""
    now = _now_epoch()
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, finished_at = ?, error = ?, retry_count = retry_count + 1
                WHERE id = ?
                """,
                (STATUS_FAILED, now, error, task_id),
            )
    finally:
        conn.close()


__all__ = [
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "enqueue",
    "mark_done",
    "mark_failed",
    "mark_running",
    "next_pending",
]
