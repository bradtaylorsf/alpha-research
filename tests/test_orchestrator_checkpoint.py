"""Tests for ``research_agent.orchestrator.checkpoint``."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from research_agent.orchestrator.checkpoint import (
    CHECKPOINT_KINDS,
    checkpoint,
    restore,
)
from research_agent.orchestrator.loop import Handler, run_loop
from research_agent.orchestrator.plan import Plan, Subgoal, TaskSpec
from research_agent.storage import db
from research_agent.storage.jobs import Job
from research_agent.storage.markdown import write_plan
from research_agent.storage.tasks import (
    STATUS_DONE,
    STATUS_PENDING,
    STATUS_RUNNING,
    enqueue,
    mark_running,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.sqlite"
    db.migrate(path=path).close()
    return path


@pytest.fixture
def jobs_root(tmp_path: Path) -> Path:
    return tmp_path / "jobs"


@pytest.fixture
def job(jobs_root: Path, db_path: Path) -> Job:
    return Job.create(
        {"goal": "Investigate Widget Co"},
        jobs_root=jobs_root,
        db_path=db_path,
    )


@pytest.fixture
def plan(job: Job) -> Plan:
    """Persist a plan whose subgoals are all open so the loop runs to drain."""
    p = Plan(
        version=1,
        objective="Investigate the target",
        subgoals=[Subgoal(id=1, description="Gather", done=False)],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=3,
    )
    write_plan(job, p.model_dump())
    return p


def _read_checkpoint_rows(db_path: Path, job_id: str) -> list[dict[str, Any]]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, kind, payload_json, ts FROM checkpoints WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _read_task_rows(db_path: Path, job_id: str) -> list[dict[str, Any]]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, kind, status, started_at, finished_at FROM tasks"
            " WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _checkpoint_synchronous_pragma(job: Job) -> str:
    """Open a checkpoint connection and read back its ``synchronous`` pragma.

    SQLite returns ``synchronous`` as an integer code: 0=OFF, 1=NORMAL, 2=FULL,
    3=EXTRA. We resolve to a string for readable assertions.
    """
    conn = db.connect_for_checkpoints(job.db_path)
    try:
        row = conn.execute("PRAGMA synchronous").fetchone()
    finally:
        conn.close()
    return {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}.get(int(row[0]), "UNKNOWN")


# ---------------------------------------------------------------------------
# Direct ``checkpoint()`` tests
# ---------------------------------------------------------------------------


def test_checkpoint_inserts_row_with_synchronous_full(job: Job, db_path: Path) -> None:
    """One row per kind, ts is monotonic-ish, payload round-trips, sync=FULL."""
    assert _checkpoint_synchronous_pragma(job) == "FULL"

    payload = {"plan_version": 1, "task_id": 42, "kind": "web_search"}
    rowids: list[int] = []
    for kind in CHECKPOINT_KINDS:
        rowids.append(checkpoint(job, kind, payload))

    rows = _read_checkpoint_rows(db_path, job.id)
    assert len(rows) == len(CHECKPOINT_KINDS)
    assert [r["kind"] for r in rows] == list(CHECKPOINT_KINDS)
    assert [r["id"] for r in rows] == rowids
    for r in rows:
        assert json.loads(r["payload_json"]) == payload
        assert isinstance(r["ts"], int)


def test_checkpoint_rejects_unknown_kind(job: Job) -> None:
    with pytest.raises(ValueError, match="unknown checkpoint kind"):
        checkpoint(job, "not_a_real_kind", {})


def test_checkpoint_rejects_non_dict_payload(job: Job) -> None:
    with pytest.raises(TypeError):
        checkpoint(job, "job_started", "not a dict")  # type: ignore[arg-type]


def test_checkpoint_emits_event(job: Job, db_path: Path) -> None:
    """Each checkpoint() call should write one ``checkpoint`` event."""
    checkpoint(job, "job_started", {"plan_version": 1})
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT kind, level, payload_json FROM events WHERE job_id = ?",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    kinds = [r["kind"] for r in rows]
    assert kinds.count("checkpoint") == 1


# ---------------------------------------------------------------------------
# ``restore()`` tests
# ---------------------------------------------------------------------------


def test_restore_returns_none_when_no_checkpoints(
    job: Job, db_path: Path, plan: Plan, jobs_root: Path
) -> None:
    state = restore(job.id, db_path=db_path, jobs_root=jobs_root)
    assert state.job_id == job.id
    assert state.last_checkpoint_kind is None
    assert state.last_checkpoint_ts is None
    assert state.last_checkpoint_payload == {}
    assert state.plan is not None
    assert state.plan_version == 1
    assert state.pending_task_ids == []
    assert state.running_task_ids_reset == []
    assert state.cost_to_date_usd == 0.0
    assert state.completed_task_count == 0


def test_restore_reads_latest_checkpoint(
    job: Job, db_path: Path, plan: Plan, jobs_root: Path
) -> None:
    checkpoint(job, "job_started", {"plan_version": 1})
    checkpoint(job, "task_pulled", {"task_id": 1, "kind": "web_search"})
    checkpoint(job, "task_done", {"task_id": 1, "kind": "web_search"})

    state = restore(job.id, db_path=db_path, jobs_root=jobs_root)
    assert state.last_checkpoint_kind == "task_done"
    assert state.last_checkpoint_payload == {"task_id": 1, "kind": "web_search"}
    assert state.last_checkpoint_ts is not None


def test_restore_resets_running_tasks_to_pending(
    job: Job, db_path: Path, plan: Plan, jobs_root: Path
) -> None:
    ids = enqueue(
        job,
        [
            TaskSpec(kind="web_search", payload={"q": "a"}),
            TaskSpec(kind="web_fetch", payload={"url": "https://x"}),
        ],
        plan_version=1,
    )
    mark_running(ids[0], db_path=db_path)

    rows_before = _read_task_rows(db_path, job.id)
    by_id_before = {r["id"]: r for r in rows_before}
    assert by_id_before[ids[0]]["status"] == STATUS_RUNNING

    state = restore(job.id, db_path=db_path, jobs_root=jobs_root)
    assert state.running_task_ids_reset == [ids[0]]

    rows_after = _read_task_rows(db_path, job.id)
    by_id_after = {r["id"]: r for r in rows_after}
    assert by_id_after[ids[0]]["status"] == STATUS_PENDING
    assert by_id_after[ids[0]]["started_at"] is None
    assert by_id_after[ids[1]]["status"] == STATUS_PENDING

    # Pending list should now include both since we reset the running one.
    assert state.pending_task_ids == ids

    # Idempotent: a second call has nothing to reset.
    state2 = restore(job.id, db_path=db_path, jobs_root=jobs_root)
    assert state2.running_task_ids_reset == []
    assert state2.pending_task_ids == ids


def test_restore_loads_latest_plan_and_cost(
    job: Job, db_path: Path, plan: Plan, jobs_root: Path
) -> None:
    p2 = plan.model_copy(update={"version": 2})
    write_plan(job, p2.model_dump())

    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO llm_calls
                    (job_id, ts, tier, provider, model, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    int(time.time()),
                    "frontier",
                    "openrouter",
                    "anthropic/claude-opus-4-7",
                    0.50,
                ),
            )
            conn.execute(
                """
                INSERT INTO llm_calls
                    (job_id, ts, tier, provider, model, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    int(time.time()),
                    "general",
                    "lmstudio",
                    "llama-3.3",
                    0.05,
                ),
            )
    finally:
        conn.close()

    state = restore(job.id, db_path=db_path, jobs_root=jobs_root)
    assert state.plan_version == 2
    assert state.plan is not None and state.plan.version == 2
    assert state.cost_to_date_usd == pytest.approx(0.55)


def test_restore_counts_completed_tasks(
    job: Job, db_path: Path, plan: Plan, jobs_root: Path
) -> None:
    ids = enqueue(
        job,
        [
            TaskSpec(kind="web_search", payload={"q": "a"}),
            TaskSpec(kind="web_search", payload={"q": "b"}),
        ],
        plan_version=1,
    )
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (STATUS_DONE, ids[0]),
            )
    finally:
        conn.close()

    state = restore(job.id, db_path=db_path, jobs_root=jobs_root)
    assert state.completed_task_count == 1
    assert state.pending_task_ids == [ids[1]]


# ---------------------------------------------------------------------------
# End-to-end: kill mid-loop, restore, resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_midloop_resume_e2e(
    job: Job, db_path: Path, plan: Plan, jobs_root: Path
) -> None:
    """Cancel mid-loop, restore, and verify the cancelled task replays."""
    ids = enqueue(
        job,
        [
            TaskSpec(kind="web_search", payload={"q": "0"}),
            TaskSpec(kind="web_search", payload={"q": "1"}),
            TaskSpec(kind="web_search", payload={"q": "2"}),
        ],
        plan_version=1,
    )

    invocations = {"n": 0}

    async def kill_second(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        invocations["n"] += 1
        if invocations["n"] == 2:
            raise asyncio.CancelledError()
        return {"ok": True}

    handlers: dict[str, Handler] = {"web_search": kill_second}
    with pytest.raises(asyncio.CancelledError):
        await run_loop(
            job,
            router=None,
            plan=plan,
            handlers=handlers,
            retry_waits=(0,),
        )

    # State after the kill: task[0] done, task[1] running, task[2] pending.
    rows_mid = _read_task_rows(db_path, job.id)
    by_id_mid = {r["id"]: r for r in rows_mid}
    assert by_id_mid[ids[0]]["status"] == STATUS_DONE
    assert by_id_mid[ids[1]]["status"] == STATUS_RUNNING
    assert by_id_mid[ids[2]]["status"] == STATUS_PENDING

    state = restore(job.id, db_path=db_path, jobs_root=jobs_root)
    assert state.running_task_ids_reset == [ids[1]]
    assert state.completed_task_count == 1
    assert sorted(state.pending_task_ids) == sorted([ids[1], ids[2]])
    assert state.last_checkpoint_kind in CHECKPOINT_KINDS

    # Resume: a handler that always succeeds — the cancelled task plus the
    # remaining tasks should both run to ``done``.
    async def always_ok(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    result = await run_loop(
        job,
        router=None,
        plan=plan,
        handlers={"web_search": always_ok},
        retry_waits=(0,),
    )
    assert result["tasks_done"] == 2

    rows_final = _read_task_rows(db_path, job.id)
    assert all(r["status"] == STATUS_DONE for r in rows_final)
