"""Tests for `research_agent.observability.events`."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_agent.observability.events import Event, emit, tail_events
from research_agent.storage import db
from research_agent.storage.jobs import Job


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
        {"goal": "Investigate observability"},
        jobs_root=jobs_root,
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


def test_event_model_defaults() -> None:
    before = int(time.time())
    ev = Event(level="INFO", kind="job_started")
    after = int(time.time())

    assert ev.schema_version == 1
    assert ev.actor is None
    assert ev.payload == {}
    assert before <= ev.ts <= after


def test_event_model_extra_forbid_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Event(level="INFO", kind="job_started", surprise="boom")  # type: ignore[call-arg]


def test_event_model_rejects_bad_level() -> None:
    with pytest.raises(ValidationError):
        Event(level="LOUD", kind="job_started")  # type: ignore[arg-type]


def test_event_model_rejects_bad_kind() -> None:
    with pytest.raises(ValidationError):
        Event(level="INFO", kind="not-a-real-kind")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# emit
# ---------------------------------------------------------------------------


def _events_lines(job: Job) -> list[str]:
    text = (job.root / "events.jsonl").read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


def _events_rows(job: Job) -> list[dict]:
    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT job_id, ts, level, actor, kind, payload_json FROM events"
            " WHERE job_id = ? ORDER BY id",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def test_emit_writes_jsonl_and_db_row(job: Job) -> None:
    ev = emit(job, "INFO", "orchestrator", "job_started", {"goal": job.goal})

    assert isinstance(ev, Event)
    assert ev.kind == "job_started"
    assert ev.actor == "orchestrator"

    lines = _events_lines(job)
    assert len(lines) == 1
    parsed = Event.model_validate_json(lines[0])
    assert parsed.kind == "job_started"
    assert parsed.actor == "orchestrator"
    assert parsed.payload == {"goal": job.goal}
    assert parsed.schema_version == 1

    rows = _events_rows(job)
    assert len(rows) == 1
    row = rows[0]
    assert row["job_id"] == job.id
    assert row["level"] == "INFO"
    assert row["actor"] == "orchestrator"
    assert row["kind"] == "job_started"
    assert row["ts"] == parsed.ts
    assert json.loads(row["payload_json"]) == {"goal": job.goal}


def test_emit_default_payload_is_empty_dict(job: Job) -> None:
    ev = emit(job, "INFO", None, "checkpoint")
    assert ev.payload == {}
    rows = _events_rows(job)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"]) == {}
    assert rows[0]["actor"] is None


def test_emit_invalid_kind_rejected(job: Job) -> None:
    # Pre-condition: events.jsonl exists from Job.create() but is empty.
    pre_lines = _events_lines(job)
    pre_rows = _events_rows(job)
    assert pre_lines == []
    assert pre_rows == []

    with pytest.raises(ValidationError):
        emit(job, "INFO", None, "definitely-not-a-kind")  # type: ignore[arg-type]

    # No partial write on either side.
    assert _events_lines(job) == []
    assert _events_rows(job) == []


def test_emit_invalid_level_rejected(job: Job) -> None:
    with pytest.raises(ValidationError):
        emit(job, "LOUD", None, "checkpoint")  # type: ignore[arg-type]
    assert _events_lines(job) == []
    assert _events_rows(job) == []


def test_emit_100_events_counts_match(job: Job) -> None:
    """The AC test: JSONL line count == events table row count == 100."""
    levels = ["DEBUG", "INFO", "WARN", "ERROR"]
    kinds = [
        "job_started",
        "task_pulled",
        "task_done",
        "task_failed",
        "plan_created",
        "synthesis_done",
        "critique_done",
        "llm_call",
        "tool_call",
        "checkpoint",
    ]

    sent: list[Event] = []
    for i in range(100):
        sent.append(
            emit(
                job,
                levels[i % len(levels)],  # type: ignore[arg-type]
                f"actor-{i % 3}",
                kinds[i % len(kinds)],  # type: ignore[arg-type]
                {"i": i, "tag": f"v{i}"},
            )
        )

    lines = _events_lines(job)
    rows = _events_rows(job)
    assert len(lines) == 100
    assert len(rows) == 100

    parsed = [Event.model_validate_json(line) for line in lines]
    for p, r, expected in zip(parsed, rows, sent, strict=True):
        assert p.ts == r["ts"] == expected.ts
        assert p.kind == r["kind"] == expected.kind
        assert p.payload == json.loads(r["payload_json"]) == expected.payload


def test_emit_db_path_override(job: Job, tmp_path: Path) -> None:
    """Explicit db_path overrides the job's default (used by daemon configs)."""
    alt = tmp_path / "alt.sqlite"
    db.migrate(path=alt).close()

    emit(job, "INFO", None, "checkpoint", {"alt": True}, db_path=alt)

    # Original DB stayed empty; alt got the row.
    assert _events_rows(job) == []
    conn = db.connect(alt)
    try:
        rows = conn.execute(
            "SELECT job_id, kind FROM events WHERE job_id = ?",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    assert [(r["job_id"], r["kind"]) for r in rows] == [(job.id, "checkpoint")]


# ---------------------------------------------------------------------------
# tail_events
# ---------------------------------------------------------------------------


async def _collect(agen, *, limit: int | None = None) -> list[Event]:
    out: list[Event] = []
    async for ev in agen:
        out.append(ev)
        if limit is not None and len(out) >= limit:
            break
    return out


async def test_tail_events_no_follow_yields_existing(job: Job) -> None:
    emit(job, "INFO", "a", "job_started", {"i": 0})
    emit(job, "DEBUG", "b", "task_pulled", {"i": 1})
    emit(job, "ERROR", None, "task_failed", {"i": 2})

    events = await _collect(tail_events(job))
    assert [(ev.kind, ev.payload["i"]) for ev in events] == [
        ("job_started", 0),
        ("task_pulled", 1),
        ("task_failed", 2),
    ]


async def test_tail_events_level_filter(job: Job) -> None:
    emit(job, "INFO", None, "job_started")
    emit(job, "ERROR", None, "task_failed")
    emit(job, "DEBUG", None, "checkpoint")

    events = await _collect(tail_events(job, level="ERROR"))
    assert [ev.kind for ev in events] == ["task_failed"]


async def test_tail_events_skips_torn_lines(job: Job) -> None:
    events_path = job.root / "events.jsonl"
    # Simulate a corrupt / torn line from an older or crashed writer:
    # a line-terminated but malformed JSON entry, plus a stray blank line.
    with events_path.open("a", encoding="utf-8") as f:
        f.write('{"level": "INFO", "kind": "job_started"\n')  # no closing brace
        f.write("\n")  # blank line
    emit(job, "INFO", None, "task_done", {"ok": True})

    events = await _collect(tail_events(job))
    assert len(events) == 1
    assert events[0].kind == "task_done"
    assert events[0].payload == {"ok": True}


async def test_tail_events_follow_yields_appended(job: Job) -> None:
    emit(job, "INFO", None, "job_started", {"i": 0})

    received: list[Event] = []

    async def consumer() -> None:
        async for ev in tail_events(job, follow=True):
            received.append(ev)
            if len(received) >= 3:
                return

    task = asyncio.create_task(consumer())
    # Give the watcher a beat to register; small but non-zero so the awatch
    # loop is set up before the new appends land.
    await asyncio.sleep(0.2)
    emit(job, "INFO", None, "task_pulled", {"i": 1})
    emit(job, "INFO", None, "task_done", {"i": 2})

    try:
        await asyncio.wait_for(task, timeout=5.0)
    except TimeoutError:
        task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task
        pytest.fail(f"follow tail did not deliver 3 events; got {len(received)}")

    assert [ev.payload["i"] for ev in received] == [0, 1, 2]
