"""Per-job coverage ledger for enumeration/list-building jobs."""

from __future__ import annotations

import itertools
import json
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from research_agent.storage import db
from research_agent.storage.jobs import Job, _atomic_write_text

CoverageStatus = Literal[
    "pending",
    "in_progress",
    "complete",
    "not_yet_public",
    "confirmed_gap",
    "failed",
]

BLOCKING_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "failed"})
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"complete", "not_yet_public", "confirmed_gap"}
)
KNOWN_DIMENSIONS: tuple[str, ...] = (
    "state",
    "chamber",
    "district_or_seat",
    "source_type",
)
_MAX_ATTEMPTS_PER_UNIT = 8
_SOURCE_TYPE_BY_KIND = {
    "fec_search": "fec-filed",
    "fec_candidates_search": "fec-filed",
    "state_election_search": "state-ballot-qualified",
}


class CoverageAttempt(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: int | None = None
    task_kind: str | None = None
    status: str | None = None
    reason: str | None = None
    source_url: str | None = None
    timestamp: int = Field(default_factory=lambda: int(time.time()))


class CoverageUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dim_key: str
    dimensions: dict[str, str] = Field(default_factory=dict)
    status: CoverageStatus = "pending"
    required: bool = True
    recent_attempts: list[CoverageAttempt] = Field(default_factory=list)
    unblocker: str | None = None
    updated_at: int = Field(default_factory=lambda: int(time.time()))


def _now_epoch() -> int:
    return int(time.time())


def _clean_dimension_value(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return ""
    return re.sub(r"\s+", " ", text)


def _normalize_for_key(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "none"


def _normalize_dimensions(raw: dict[str, Any]) -> dict[str, str]:
    dims: dict[str, str] = {}
    for key, value in raw.items():
        if value in (None, "", []):
            continue
        clean = _clean_dimension_value(value)
        if clean:
            dims[str(key)] = clean
    return dims


def dim_key_for(dimensions: dict[str, Any]) -> str:
    dims = _normalize_dimensions(dimensions)
    if not dims:
        raise ValueError("coverage unit dimensions cannot be empty")
    parts = [
        f"{key}={_normalize_for_key(value)}"
        for key, value in sorted(dims.items())
    ]
    return "|".join(parts)


def _coerce_unit(unit: CoverageUnit | dict[str, Any]) -> CoverageUnit:
    if isinstance(unit, CoverageUnit):
        return unit
    if not isinstance(unit, dict):
        raise TypeError(f"coverage unit must be a dict or CoverageUnit, got {type(unit).__name__}")

    raw_dims = unit.get("dimensions")
    if isinstance(raw_dims, dict):
        dimensions = _normalize_dimensions(raw_dims)
    else:
        dimensions = _normalize_dimensions({k: unit.get(k) for k in KNOWN_DIMENSIONS})
    dim_key = str(unit.get("dim_key") or dim_key_for(dimensions))
    attempts = [
        a if isinstance(a, CoverageAttempt) else CoverageAttempt.model_validate(a)
        for a in unit.get("recent_attempts", [])
        if isinstance(a, (CoverageAttempt, dict))
    ]
    return CoverageUnit(
        dim_key=dim_key,
        dimensions=dimensions,
        status=unit.get("status") or "pending",
        required=bool(unit.get("required", True)),
        recent_attempts=attempts[-_MAX_ATTEMPTS_PER_UNIT:],
        unblocker=unit.get("unblocker"),
        updated_at=int(unit.get("updated_at") or _now_epoch()),
    )


def _row_to_unit(row: Any) -> CoverageUnit:
    attempts_raw = json.loads(row["recent_attempts_json"] or "[]")
    attempts = [
        CoverageAttempt.model_validate(item)
        for item in attempts_raw
        if isinstance(item, dict)
    ]
    return CoverageUnit(
        dim_key=row["dim_key"],
        dimensions=json.loads(row["dims_json"]),
        status=row["status"],
        recent_attempts=attempts,
        unblocker=row["unblocker"],
        updated_at=int(row["updated_at"]),
    )


def _sidecar_path(job: Job):
    return job.root / "coverage.json"


def _write_sidecar(job: Job) -> None:
    units = list_units(job)
    payload = {
        "job_id": job.id,
        "updated_at": _now_epoch(),
        "blocking_statuses": sorted(BLOCKING_STATUSES),
        "units": [unit.model_dump(mode="json") for unit in units],
    }
    _atomic_write_text(
        _sidecar_path(job),
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def _read_existing_by_key(job: Job) -> dict[str, CoverageUnit]:
    return {unit.dim_key: unit for unit in list_units(job)}


def declare_coverage(job: Job, units: list[CoverageUnit | dict[str, Any]]) -> list[CoverageUnit]:
    """Declare required coverage units, preserving existing statuses and attempts."""
    coerced = [_coerce_unit(unit) for unit in units]
    if not coerced:
        return []
    existing = _read_existing_by_key(job)
    now = _now_epoch()
    conn = db.connect(job.db_path)
    try:
        with conn:
            for unit in coerced:
                prior = existing.get(unit.dim_key)
                status = prior.status if prior is not None else unit.status
                attempts = prior.recent_attempts if prior is not None else unit.recent_attempts
                unblocker = prior.unblocker if prior is not None else unit.unblocker
                conn.execute(
                    """
                    INSERT INTO coverage_units (
                        job_id, dim_key, dims_json, status, recent_attempts_json,
                        last_attempt_json, unblocker, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id, dim_key) DO UPDATE SET
                        dims_json = excluded.dims_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        job.id,
                        unit.dim_key,
                        json.dumps(unit.dimensions, sort_keys=True),
                        status,
                        json.dumps([a.model_dump(mode="json") for a in attempts]),
                        json.dumps(attempts[-1].model_dump(mode="json")) if attempts else None,
                        unblocker,
                        now,
                    ),
                )
    finally:
        conn.close()
    _write_sidecar(job)
    return list_units(job)


def _dimension_product(units_shape: dict[str, Any]) -> list[dict[str, Any]]:
    keys = [key for key, value in units_shape.items() if value not in (None, "", [])]
    value_lists: list[list[Any]] = []
    for key in keys:
        value = units_shape[key]
        if isinstance(value, list):
            value_lists.append(value)
        else:
            value_lists.append([value])
    return [dict(zip(keys, values, strict=True)) for values in itertools.product(*value_lists)]


def units_from_intake(intake: dict[str, Any]) -> list[dict[str, Any]]:
    """Return explicit coverage units from an intake ``enumeration`` block."""
    enum = intake.get("enumeration")
    if not isinstance(enum, dict):
        return []
    raw_units = enum.get("units") or enum.get("coverage_units")
    if isinstance(raw_units, list):
        return [unit for unit in raw_units if isinstance(unit, dict)]
    if isinstance(raw_units, dict):
        return _dimension_product(raw_units)
    dimensions = enum.get("dimensions")
    if isinstance(dimensions, dict):
        return _dimension_product(dimensions)
    return []


def declare_from_intake(job: Job) -> list[CoverageUnit]:
    units = units_from_intake(job.intake or {})
    if not units:
        return []
    return declare_coverage(job, units)


def list_units(job: Job, statuses: set[str] | None = None) -> list[CoverageUnit]:
    params: list[Any] = [job.id]
    sql = (
        "SELECT dim_key, dims_json, status, recent_attempts_json, unblocker, updated_at"
        " FROM coverage_units WHERE job_id = ?"
    )
    if statuses:
        placeholders = ",".join("?" for _ in sorted(statuses))
        sql += f" AND status IN ({placeholders})"
        params.extend(sorted(statuses))
    sql += " ORDER BY dim_key ASC"
    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_row_to_unit(row) for row in rows]


def has_coverage(job: Job) -> bool:
    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM coverage_units WHERE job_id = ? LIMIT 1",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def blocking_units(job: Job) -> list[CoverageUnit]:
    return list_units(job, set(BLOCKING_STATUSES))


def is_coverage_complete(job: Job) -> bool:
    if not has_coverage(job):
        return True
    return not blocking_units(job)


def upsert_unit_status(
    job: Job,
    dim_key: str,
    status: CoverageStatus,
    *,
    attempt: CoverageAttempt | dict[str, Any] | None = None,
    unblocker: str | None = None,
) -> CoverageUnit:
    existing = {unit.dim_key: unit for unit in list_units(job)}
    unit = existing.get(dim_key)
    if unit is None:
        raise KeyError(f"coverage unit not found: {dim_key}")
    attempts = list(unit.recent_attempts)
    parsed_attempt: CoverageAttempt | None = None
    if attempt is not None:
        parsed_attempt = (
            attempt
            if isinstance(attempt, CoverageAttempt)
            else CoverageAttempt.model_validate(attempt)
        )
        attempts.append(parsed_attempt)
        attempts = attempts[-_MAX_ATTEMPTS_PER_UNIT:]
    now = _now_epoch()
    final_unblocker = unblocker if unblocker is not None else unit.unblocker
    conn = db.connect(job.db_path)
    try:
        with conn:
            conn.execute(
                """
                UPDATE coverage_units
                SET status = ?, recent_attempts_json = ?, last_attempt_json = ?,
                    unblocker = ?, updated_at = ?
                WHERE job_id = ? AND dim_key = ?
                """,
                (
                    status,
                    json.dumps([a.model_dump(mode="json") for a in attempts]),
                    json.dumps(parsed_attempt.model_dump(mode="json")) if parsed_attempt else None,
                    final_unblocker,
                    now,
                    job.id,
                    dim_key,
                ),
            )
    finally:
        conn.close()
    _write_sidecar(job)
    return CoverageUnit(
        dim_key=unit.dim_key,
        dimensions=unit.dimensions,
        status=status,
        recent_attempts=attempts,
        unblocker=final_unblocker,
        updated_at=now,
    )


def set_matching_units(
    job: Job,
    dimensions: dict[str, Any],
    status: CoverageStatus,
    *,
    attempt: CoverageAttempt | dict[str, Any] | None = None,
    unblocker: str | None = None,
) -> list[CoverageUnit]:
    """Set every unit whose declared dimensions match the supplied dimensions."""
    normalized = {k: _normalize_for_key(v) for k, v in _normalize_dimensions(dimensions).items()}
    updated: list[CoverageUnit] = []
    for unit in list_units(job):
        if unit.status in TERMINAL_STATUSES and status != "complete":
            continue
        unit_dims = {
            key: _normalize_for_key(value)
            for key, value in unit.dimensions.items()
            if value not in (None, "")
        }
        if all(normalized.get(key) == value for key, value in unit_dims.items()):
            updated.append(
                upsert_unit_status(
                    job,
                    unit.dim_key,
                    status,
                    attempt=attempt,
                    unblocker=unblocker,
                )
            )
    return updated


def _extract_dimensions_from_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    extras = mapping.get("extras")
    metadata = mapping.get("metadata")
    pools = [
        mapping,
        extras if isinstance(extras, dict) else {},
        metadata if isinstance(metadata, dict) else {},
    ]
    dims: dict[str, Any] = {}
    for pool in pools:
        state = pool.get("state")
        if state and "state" not in dims:
            dims["state"] = state
        chamber = pool.get("chamber") or pool.get("office_full") or pool.get("office")
        if chamber and "chamber" not in dims:
            dims["chamber"] = chamber
        district = (
            pool.get("district_or_seat")
            or pool.get("district")
            or pool.get("district_number")
            or pool.get("seat")
        )
        if district not in (None, "", []) and "district_or_seat" not in dims:
            dims["district_or_seat"] = district
        source_type = pool.get("source_type") or pool.get("source_class")
        if source_type and "source_type" not in dims:
            dims["source_type"] = source_type
    return dims


def dimensions_from_task_and_row(
    task: dict[str, Any],
    row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    dims = _extract_dimensions_from_mapping(payload)
    if row:
        dims.update(_extract_dimensions_from_mapping(row))
    kind = str(task.get("kind") or "")
    dims.setdefault("source_type", _SOURCE_TYPE_BY_KIND.get(kind))
    return {k: v for k, v in dims.items() if v not in (None, "", [])}


def update_from_task_result(job: Job, task: dict[str, Any], result: dict[str, Any] | None) -> None:
    """Best-effort coverage update from connector task output."""
    if not has_coverage(job):
        return
    attempt_base = {
        "task_id": task.get("id"),
        "task_kind": task.get("kind"),
        "status": "done",
    }
    rows = result.get("results") if isinstance(result, dict) else None
    if isinstance(rows, list) and rows:
        for row in rows:
            if not isinstance(row, dict):
                continue
            dims = dimensions_from_task_and_row(task, row)
            if dims:
                set_matching_units(
                    job,
                    dims,
                    "complete",
                    attempt={**attempt_base, "source_url": row.get("url")},
                )
        return
    if isinstance(rows, list) and not rows:
        dims = dimensions_from_task_and_row(task)
        if dims:
            set_matching_units(
                job,
                dims,
                "failed",
                attempt={**attempt_base, "status": "failed", "reason": "0 results"},
            )


def mark_task_failed(job: Job, task: dict[str, Any], reason: str) -> None:
    if not has_coverage(job):
        return
    dims = dimensions_from_task_and_row(task)
    if not dims:
        return
    set_matching_units(
        job,
        dims,
        "failed",
        attempt={
            "task_id": task.get("id"),
            "task_kind": task.get("kind"),
            "status": "failed",
            "reason": reason,
        },
    )


def replan_context(job: Job) -> dict[str, Any] | None:
    if not has_coverage(job):
        return None
    units = list_units(job)
    uncovered = [unit for unit in units if unit.status in BLOCKING_STATUSES]
    return {
        "complete": not uncovered,
        "total_units": len(units),
        "uncovered_units": [unit.model_dump(mode="json") for unit in uncovered[:100]],
        "recent_attempts_per_unit": {
            unit.dim_key: [a.model_dump(mode="json") for a in unit.recent_attempts]
            for unit in uncovered[:100]
            if unit.recent_attempts
        },
    }


__all__ = [
    "BLOCKING_STATUSES",
    "CoverageAttempt",
    "CoverageStatus",
    "CoverageUnit",
    "blocking_units",
    "declare_coverage",
    "declare_from_intake",
    "dim_key_for",
    "dimensions_from_task_and_row",
    "has_coverage",
    "is_coverage_complete",
    "list_units",
    "mark_task_failed",
    "replan_context",
    "set_matching_units",
    "units_from_intake",
    "update_from_task_result",
    "upsert_unit_status",
]
