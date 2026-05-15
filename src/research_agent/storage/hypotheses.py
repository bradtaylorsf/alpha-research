"""Per-job working hypothesis ledger (issue #261)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from research_agent.storage import db
from research_agent.storage.jobs import Job

VALID_STATUSES = frozenset({"open", "confirmed", "refuted", "inconclusive"})


def _now_epoch() -> int:
    return int(time.time())


def _coerce_id_list(values: list[Any] | tuple[Any, ...] | None, *, field: str) -> list[int]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"{field} must be a list of finding ids")
    out: list[int] = []
    for value in values:
        if isinstance(value, bool):
            raise TypeError(f"{field} values must be integer finding ids")
        if isinstance(value, int):
            out.append(value)
            continue
        if isinstance(value, str) and value.strip().isdigit():
            out.append(int(value.strip()))
            continue
        raise TypeError(f"{field} values must be integer finding ids")
    return out


def _validate_inputs(
    *,
    statement: str,
    confidence: float,
    supports: list[Any] | tuple[Any, ...] | None,
    refutes: list[Any] | tuple[Any, ...] | None,
    status: str,
) -> tuple[list[int], list[int]]:
    if not isinstance(statement, str) or not statement.strip():
        raise ValueError("statement must be a non-empty string")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ValueError("confidence must be a number between 0 and 1")
    if not 0 <= float(confidence) <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
    return (
        _coerce_id_list(supports, field="supports"),
        _coerce_id_list(refutes, field="refutes"),
    )


def _row_to_dict(row: Any) -> dict[str, Any]:
    supports_raw = row["supports"]
    refutes_raw = row["refutes"]
    try:
        supports = json.loads(supports_raw) if supports_raw else []
    except (TypeError, json.JSONDecodeError):
        supports = []
    try:
        refutes = json.loads(refutes_raw) if refutes_raw else []
    except (TypeError, json.JSONDecodeError):
        refutes = []
    return {
        "id": int(row["id"]),
        "job_id": row["job_id"],
        "plan_version": int(row["plan_version"]),
        "statement": row["statement"],
        "confidence": float(row["confidence"]),
        "supports": supports if isinstance(supports, list) else [],
        "refutes": refutes if isinstance(refutes, list) else [],
        "status": row["status"],
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
    }


def upsert_hypothesis(
    job: Job,
    *,
    plan_version: int,
    statement: str,
    confidence: float,
    supports: list[Any] | tuple[Any, ...] | None,
    refutes: list[Any] | tuple[Any, ...] | None,
    status: str,
    id: int | None = None,
    db_path: Path | str | None = None,
) -> int:
    """Insert a new hypothesis or update an existing one by id."""
    if not isinstance(plan_version, int) or plan_version < 1:
        raise ValueError("plan_version must be an integer >= 1")
    supports_i, refutes_i = _validate_inputs(
        statement=statement,
        confidence=confidence,
        supports=supports,
        refutes=refutes,
        status=status,
    )
    now = _now_epoch()
    conn = db.connect(db_path or job.db_path)
    try:
        with conn:
            if id is None:
                cur = conn.execute(
                    """
                    INSERT INTO hypotheses (
                        job_id, plan_version, statement, confidence,
                        supports, refutes, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.id,
                        plan_version,
                        statement.strip(),
                        float(confidence),
                        json.dumps(supports_i, sort_keys=True),
                        json.dumps(refutes_i, sort_keys=True),
                        status,
                        now,
                        now,
                    ),
                )
                rowid = cur.lastrowid
                assert rowid is not None  # noqa: S101
                return int(rowid)

            conn.execute(
                """
                UPDATE hypotheses
                SET plan_version = ?, statement = ?, confidence = ?,
                    supports = ?, refutes = ?, status = ?, updated_at = ?
                WHERE id = ? AND job_id = ?
                """,
                (
                    plan_version,
                    statement.strip(),
                    float(confidence),
                    json.dumps(supports_i, sort_keys=True),
                    json.dumps(refutes_i, sort_keys=True),
                    status,
                    now,
                    int(id),
                    job.id,
                ),
            )
            return int(id)
    finally:
        conn.close()


def list_hypotheses(job: Job, *, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Return the current hypothesis ledger for ``job`` ordered by id."""
    conn = db.connect(db_path or job.db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM hypotheses WHERE job_id = ? ORDER BY id ASC",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(row) for row in rows]


def latest_for_job(job: Job, *, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Return the operator-facing current set of hypotheses for ``job``."""
    return list_hypotheses(job, db_path=db_path)


__all__ = [
    "VALID_STATUSES",
    "latest_for_job",
    "list_hypotheses",
    "upsert_hypothesis",
]
