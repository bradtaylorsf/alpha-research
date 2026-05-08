"""Structured event log: ``events.jsonl`` writer + SQLite mirror.

Implements the §4 observability contract from the implementation guide.
Every state-changing operation emits exactly one :class:`Event` that is
appended to the per-job ``events.jsonl`` (append-only, atomic-line) and
mirrored to the cross-job ``events`` table for SQL queries.

Both writes are performed in :func:`emit`. JSONL is written first because
it's the canonical tail target the future UI subscribes to; the SQL row is
the secondary index. If the SQL insert fails we re-raise — but the JSONL
line is already on disk and a future tailer will see it.

The ``schema_version`` field is part of the §14 hard-to-change-later list:
bumped when the on-disk format changes incompatibly.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from research_agent.storage import db
from research_agent.storage.jobs import Job

EventLevel = Literal["DEBUG", "INFO", "WARN", "ERROR"]

EventKind = Literal[
    "job_started",
    "job_stopped",
    "task_pulled",
    "task_done",
    "task_failed",
    "plan_created",
    "plan_subgoals_updated",
    "plan_subgoals_reopened",
    "subgoals_reopened",
    "synthesis_done",
    "synthesis_written",
    "synthesis_failed",
    "critique_done",
    "critique_written",
    "drain_replan",
    "replan_triggered",
    "replan_truncated",
    "findings_truncated",
    "llm_call",
    "lmstudio_degraded",
    "lmstudio_recovered",
    "lmstudio_rerouted",
    "tool_call",
    "checkpoint",
    "prompt_loaded",
    "source_pruned",
    "pdf_vlm_escalation",
    "ocr_vlm_escalation",
    "cornerstone_extract",
    "cornerstone_fallback_triggered",
    "error",
    "warning",
]


class Event(BaseModel):
    """One structured event in the job event log."""

    model_config = ConfigDict(extra="forbid")

    ts: int = Field(default_factory=lambda: int(time.time()))
    level: EventLevel
    actor: str | None = None
    kind: EventKind
    payload: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = 1


def emit(
    job: Job,
    level: EventLevel,
    actor: str | None,
    kind: EventKind,
    payload: dict[str, Any] | None = None,
    *,
    db_path: Path | str | None = None,
) -> Event:
    """Append one event to ``events.jsonl`` and mirror to the ``events`` table.

    Returns the constructed :class:`Event`. Raises :class:`pydantic.ValidationError`
    on bad input before either side is written.
    """
    event = Event(
        level=level,
        actor=actor,
        kind=kind,
        payload=payload if payload is not None else {},
    )

    line = event.model_dump_json() + "\n"
    events_path = job.root / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    # Append-mode + a single write call: POSIX guarantees atomic appends for
    # writes <= PIPE_BUF, so concurrent writers cannot tear lines.
    with events_path.open("a", encoding="utf-8") as f:
        f.write(line)

    db_path_p = Path(db_path) if db_path is not None else job.db_path
    payload_json = json.dumps(event.payload, sort_keys=True, default=str)
    conn = db.connect(db_path_p)
    try:
        with conn:
            conn.execute(
                "INSERT INTO events (job_id, ts, level, actor, kind, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (job.id, event.ts, event.level, event.actor, event.kind, payload_json),
            )
    finally:
        conn.close()

    return event


async def tail_events(
    job: Job,
    *,
    follow: bool = False,
    level: str | None = None,
) -> AsyncIterator[Event]:
    """Async generator over a job's ``events.jsonl``.

    Reads the existing file from byte offset 0, yielding parsed :class:`Event`
    instances. With ``follow=True`` keeps watching the file and yields newly
    appended events; cancel the awaiting task to stop.

    Lines that fail to parse (blank, half-written from a torn writer) are
    skipped silently — the canonical SQL mirror is the source of truth for
    auditing.
    """
    level_norm = level.upper() if level else None
    path = job.root / "events.jsonl"

    def _parse(buf: bytes) -> list[Event]:
        out: list[Event] = []
        for raw in buf.decode("utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                ev = Event.model_validate_json(line)
            except Exception:
                continue
            if level_norm and ev.level != level_norm:
                continue
            out.append(ev)
        return out

    offset = 0
    if path.exists():
        with path.open("rb") as f:
            data = f.read()
            offset = f.tell()
        for ev in _parse(data):
            yield ev

    if not follow:
        return

    from watchfiles import awatch

    async for _changes in awatch(path):
        if not path.exists():
            continue
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
            offset = f.tell()
        for ev in _parse(data):
            yield ev


__all__ = [
    "Event",
    "EventKind",
    "EventLevel",
    "emit",
    "tail_events",
]
