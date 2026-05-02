"""Checkpoint save/restore for the research loop.

Implements §6.2 of the implementation guide: every state transition writes
a row to the cross-job ``checkpoints`` table, and :func:`restore` reads the
most recent row to reconstruct ``(plan, queue state, cost-to-date)`` so the
loop can resume without re-doing completed tasks.

Checkpoint inserts use ``synchronous=FULL`` (the §6.2 belt-and-suspenders
durability option) — a hard kill between two consecutive transitions can
still cost the in-flight task, but it cannot lose the prior committed
checkpoint row. The rest of the read path uses the standard ``NORMAL``
connection.

Resume is idempotent: any task left in ``running`` (i.e. the worker died
mid-execution) is reset to ``pending`` so a fresh worker re-pulls it. The
v1 task surface has no outbound side effects with idempotency keys, so a
clean re-run of a half-done task is the correct semantics.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from research_agent.observability.events import emit
from research_agent.orchestrator.plan import Plan
from research_agent.storage import db
from research_agent.storage.jobs import DEFAULT_JOBS_ROOT, Job

CheckpointKind = Literal[
    "job_started",
    "task_pulled",
    "task_done",
    "synthesis_done",
    "critique_done",
    "replan_done",
    "stop_requested",
]

CHECKPOINT_KINDS: tuple[str, ...] = (
    "job_started",
    "task_pulled",
    "task_done",
    "synthesis_done",
    "critique_done",
    "replan_done",
    "stop_requested",
)


class RestoreState(BaseModel):
    """State needed to resume the research loop after a kill or shutdown."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    last_checkpoint_kind: str | None = None
    last_checkpoint_ts: int | None = None
    last_checkpoint_payload: dict[str, Any] = Field(default_factory=dict)
    plan: Plan | None = None
    plan_version: int | None = None
    pending_task_ids: list[int] = Field(default_factory=list)
    running_task_ids_reset: list[int] = Field(default_factory=list)
    cost_to_date_usd: float = 0.0
    completed_task_count: int = 0


def checkpoint(job: Job, kind: str, payload: dict[str, Any]) -> int:
    """Insert a checkpoint row for ``job`` and return its rowid.

    Validates ``kind`` against :data:`CHECKPOINT_KINDS` and opens the
    connection at ``synchronous=FULL`` so the insert is durable across a
    crash even before fsync amortization.
    """
    if kind not in CHECKPOINT_KINDS:
        raise ValueError(f"unknown checkpoint kind {kind!r}; must be one of {CHECKPOINT_KINDS}")
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be a dict; got {type(payload).__name__}")

    payload_json = json.dumps(payload, sort_keys=True, default=str)
    ts = int(time.time())

    conn = db.connect_for_checkpoints(job.db_path)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO checkpoints (job_id, kind, payload_json, ts) VALUES (?, ?, ?, ?)",
                (job.id, kind, payload_json, ts),
            )
            rowid = cur.lastrowid
        assert rowid is not None  # noqa: S101 — INTEGER PK rowid set after INSERT
    finally:
        conn.close()

    emit(
        job,
        "DEBUG",
        "checkpoint",
        "checkpoint",
        {"checkpoint_kind": kind, "ts": ts, "rowid": int(rowid)},
    )
    return int(rowid)


def _load_latest_checkpoint(
    db_path: Path, job_id: str
) -> tuple[str | None, int | None, dict[str, Any]]:
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT kind, ts, payload_json
            FROM checkpoints
            WHERE job_id = ?
            ORDER BY ts DESC, id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None, None, {}

    payload_raw = row["payload_json"]
    payload = json.loads(payload_raw) if payload_raw else {}
    return row["kind"], int(row["ts"]), payload


def _load_latest_plan(db_path: Path, job_id: str) -> Plan | None:
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM plans WHERE job_id = ? ORDER BY version DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return Plan.model_validate_json(row["payload_json"])


def _reset_running_tasks(db_path: Path, job_id: str) -> list[int]:
    """Reset any ``running`` tasks back to ``pending`` and return reset ids."""
    conn = db.connect(db_path)
    try:
        with conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE job_id = ? AND status = 'running' ORDER BY id ASC",
                (job_id,),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                conn.execute(
                    "UPDATE tasks SET status = 'pending', started_at = NULL"
                    " WHERE job_id = ? AND status = 'running'",
                    (job_id,),
                )
    finally:
        conn.close()
    return ids


def _cost_to_date(db_path: Path, job_id: str) -> float:
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM llm_calls WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return 0.0
    val = row["total"]
    return float(val) if val is not None else 0.0


def _completed_count(db_path: Path, job_id: str) -> int:
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE job_id = ? AND status = 'done'",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["c"]) if row is not None else 0


def _pending_ids(db_path: Path, job_id: str) -> list[int]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE job_id = ? AND status = 'pending' ORDER BY id ASC",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    return [int(r["id"]) for r in rows]


def restore(
    job_id: str,
    *,
    db_path: Path | str = db.DEFAULT_DB_PATH,
    jobs_root: Path | str = DEFAULT_JOBS_ROOT,
) -> RestoreState:
    """Reconstruct the resume state for ``job_id`` from disk + DB.

    Idempotent: a second ``restore()`` after the first returns the same
    state except its ``running_task_ids_reset`` is empty, since the first
    call already moved any in-flight rows back to ``pending``.
    """
    db_path_p = Path(db_path)
    jobs_root_p = Path(jobs_root)

    job = Job.load(job_id, jobs_root=jobs_root_p, db_path=db_path_p)

    last_kind, last_ts, last_payload = _load_latest_checkpoint(db_path_p, job_id)
    plan = _load_latest_plan(db_path_p, job_id)
    plan_version = plan.version if plan is not None else None

    running_reset = _reset_running_tasks(db_path_p, job_id)
    cost = _cost_to_date(db_path_p, job_id)
    completed = _completed_count(db_path_p, job_id)
    pending = _pending_ids(db_path_p, job_id)

    state = RestoreState(
        job_id=job_id,
        last_checkpoint_kind=last_kind,
        last_checkpoint_ts=last_ts,
        last_checkpoint_payload=last_payload,
        plan=plan,
        plan_version=plan_version,
        pending_task_ids=pending,
        running_task_ids_reset=running_reset,
        cost_to_date_usd=cost,
        completed_task_count=completed,
    )

    emit(
        job,
        "INFO",
        "checkpoint",
        "checkpoint",
        {
            "stage": "resume_initialized",
            "last_checkpoint_kind": last_kind,
            "plan_version": plan_version,
            "pending_count": len(pending),
            "running_reset_count": len(running_reset),
            "cost_to_date_usd": cost,
            "completed_task_count": completed,
        },
    )

    return state


__all__ = [
    "CHECKPOINT_KINDS",
    "CheckpointKind",
    "RestoreState",
    "checkpoint",
    "restore",
]
