"""Markdown writers for findings, plans, syntheses, and reports.

Implements the per-job content layout from §4 of the implementation guide:
findings get monotonic six-digit ids (``findings/000001.md`` + sidecar JSON);
plans and syntheses are versioned four-digit (``plan/0001.md``,
``synthesis/0001.md``); ``report.md`` rotates prior copies into
``report.history/<UTC ISO timestamp>.md`` before each new write.

Every write produces both a markdown file (the human-readable content)
and a JSON sidecar (structured metadata mirrored into SQLite). All file
writes go through the atomic ``*.tmp`` + :func:`os.replace` pattern from
§16 to keep tail-watching readers from observing half-written content.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_agent.storage import db
from research_agent.storage.jobs import Job, _atomic_write_json, _atomic_write_text


def _now_epoch() -> int:
    return int(time.time())


def _validate_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"confidence must be a number in [0, 1]; got {value!r}")
    conf = float(value)
    if conf < 0.0 or conf > 1.0:
        raise ValueError(f"confidence must be in [0, 1]; got {conf}")
    return conf


def _validate_source_ids(value: Any) -> list[int]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"source_ids must be a non-empty list of ints; got {value!r}")
    out: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"source_ids must contain ints; got {item!r}")
        out.append(item)
    return out


def _render_finding_md(
    *,
    finding_id: int,
    claim: str,
    confidence: float,
    source_ids: list[int],
    contradicts: list[int] | None,
    tags: list[str] | None,
) -> str:
    contradicts_str = ", ".join(str(i) for i in contradicts) if contradicts else "—"
    tags_str = ", ".join(tags) if tags else "—"
    sources_str = ", ".join(str(i) for i in source_ids)
    return (
        f"# Finding {finding_id:06d}\n"
        f"\n"
        f"**Confidence:** {confidence}\n"
        f"**Sources:** {sources_str}\n"
        f"**Contradicts:** {contradicts_str}\n"
        f"**Tags:** {tags_str}\n"
        f"\n"
        f"---\n"
        f"\n"
        f"{claim.strip()}\n"
    )


def write_finding(
    job: Job,
    claim: str,
    confidence: float,
    source_ids: list[int],
    contradicts: list[int] | None = None,
    tags: list[str] | None = None,
) -> int:
    """Write a finding's md + json sidecar and insert the ``findings`` row.

    The autoincrement id from the DB drives the zero-padded filename
    (``findings/{id:06d}.md``) so a future UI can deep-link.
    """
    if not isinstance(claim, str) or not claim.strip():
        raise ValueError("claim must be a non-empty string")
    conf = _validate_confidence(confidence)
    sids = _validate_source_ids(source_ids)
    contradicts_list = list(contradicts) if contradicts else None
    tags_list = list(tags) if tags else None

    now = _now_epoch()
    source_ids_json = json.dumps(sids)
    contradicts_json = json.dumps(contradicts_list) if contradicts_list is not None else None
    tags_json = json.dumps(tags_list) if tags_list is not None else None

    conn = db.connect(job.db_path)
    try:
        with conn:
            cur = conn.execute(
                """
                INSERT INTO findings (
                    job_id, md_path, claim, confidence,
                    source_ids, contradicts, tags, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    "",
                    claim,
                    conf,
                    source_ids_json,
                    contradicts_json,
                    tags_json,
                    now,
                ),
            )
            assert cur.lastrowid is not None
            finding_id = int(cur.lastrowid)
            md_rel = f"findings/{finding_id:06d}.md"
            json_rel = f"findings/{finding_id:06d}.json"

            md_body = _render_finding_md(
                finding_id=finding_id,
                claim=claim,
                confidence=conf,
                source_ids=sids,
                contradicts=contradicts_list,
                tags=tags_list,
            )
            sidecar = {
                "id": finding_id,
                "claim": claim,
                "confidence": conf,
                "source_ids": sids,
                "contradicts": contradicts_list,
                "tags": tags_list,
                "md_path": md_rel,
                "created_at": now,
            }
            _atomic_write_text(job.root / md_rel, md_body)
            _atomic_write_json(job.root / json_rel, sidecar)

            conn.execute(
                "UPDATE findings SET md_path = ? WHERE id = ?",
                (md_rel, finding_id),
            )
    finally:
        conn.close()

    return finding_id


def _next_version(conn: Any, table: str, job_id: str) -> int:
    row = conn.execute(
        f"SELECT COALESCE(MAX(version), 0) + 1 AS next FROM {table} WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return int(row["next"])


def _render_plan_md(version: int, payload: dict[str, Any], created_at: int) -> str:
    pretty = json.dumps(payload, indent=2, sort_keys=True)
    return (
        f"# Plan v{version:04d}\n"
        f"\n"
        f"Created: {datetime.fromtimestamp(created_at, UTC).isoformat()}\n"
        f"\n"
        f"```json\n"
        f"{pretty}\n"
        f"```\n"
    )


def write_plan(job: Job, payload: dict[str, Any]) -> int:
    """Write the next plan version (md + json) and insert into ``plans``."""
    if not isinstance(payload, dict):
        raise ValueError(f"payload must be a dict; got {type(payload).__name__}")

    now = _now_epoch()
    payload_json = json.dumps(payload, sort_keys=True)

    conn = db.connect(job.db_path)
    try:
        with conn:
            version = _next_version(conn, "plans", job.id)
            md_rel = f"plan/{version:04d}.md"
            json_rel = f"plan/{version:04d}.json"

            _atomic_write_text(job.root / md_rel, _render_plan_md(version, payload, now))
            _atomic_write_json(job.root / json_rel, payload)

            conn.execute(
                """
                INSERT INTO plans (job_id, version, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (job.id, version, payload_json, now),
            )
    finally:
        conn.close()

    return version


def write_synthesis(
    job: Job,
    content: str,
    model: str,
    cost_usd: float | None = None,
) -> int:
    """Write the next synthesis version (md + json) and insert into ``syntheses``."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty string")
    if not isinstance(model, str) or not model:
        raise ValueError("model must be a non-empty string")
    if cost_usd is not None and (
        isinstance(cost_usd, bool) or not isinstance(cost_usd, (int, float))
    ):
        raise ValueError(f"cost_usd must be a number or None; got {cost_usd!r}")

    now = _now_epoch()
    cost = float(cost_usd) if cost_usd is not None else None

    conn = db.connect(job.db_path)
    try:
        with conn:
            version = _next_version(conn, "syntheses", job.id)
            md_rel = f"synthesis/{version:04d}.md"
            json_rel = f"synthesis/{version:04d}.json"

            md_body = content if content.endswith("\n") else content + "\n"
            _atomic_write_text(job.root / md_rel, md_body)
            _atomic_write_json(
                job.root / json_rel,
                {
                    "version": version,
                    "model": model,
                    "cost_usd": cost,
                    "created_at": now,
                },
            )

            conn.execute(
                """
                INSERT INTO syntheses (job_id, version, md_path, model, cost_usd, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job.id, version, md_rel, model, cost, now),
            )
    finally:
        conn.close()

    return version


def write_report(job: Job, content: str) -> Path:
    """Rotate any prior ``report.md`` into ``report.history/`` then write fresh content."""
    if not isinstance(content, str):
        raise ValueError(f"content must be a string; got {type(content).__name__}")

    report = job.root / "report.md"
    history_dir = job.root / "report.history"
    history_dir.mkdir(parents=True, exist_ok=True)

    if report.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        archived = history_dir / f"{stamp}.md"
        # If two rotations land in the same second, append a suffix rather
        # than clobbering. Atomic via os.replace inside _atomic rename below
        # is unnecessary — the source already exists fully written.
        suffix = 1
        while archived.exists():
            archived = history_dir / f"{stamp}-{suffix}.md"
            suffix += 1
        os.replace(report, archived)

    _atomic_write_text(report, content if content.endswith("\n") else content + "\n")
    return report


__all__ = [
    "write_finding",
    "write_plan",
    "write_report",
    "write_synthesis",
]
