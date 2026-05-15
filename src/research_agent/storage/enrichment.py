"""CSV import and enrichment workflow for existing table artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from research_agent.storage import artifacts
from research_agent.storage.artifacts import ArtifactColumn, ArtifactSchema
from research_agent.storage.jobs import Job, _atomic_write_text

_PROVENANCE_SUFFIX = ".provenance.jsonl"
_CONFLICTS_SUFFIX = ".conflicts.jsonl"
_UPDATE_META_KEYS = {
    "source_url",
    "source_kind",
    "confidence",
    "task_id",
    "timestamp",
    "values",
}


def _now_epoch() -> int:
    return int(time.time())


def _artifact_dir(job: Job) -> Path:
    path = job.root / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_meta(job: Job, name: str) -> dict[str, Any]:
    path = _artifact_dir(job) / f"{name}.meta.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_meta(job: Job, name: str, updates: dict[str, Any]) -> None:
    path = _artifact_dir(job) / f"{name}.meta.json"
    meta = _read_meta(job, name)
    meta.update(updates)
    _atomic_write_text(path, json.dumps(meta, indent=2, sort_keys=True) + "\n")


def _jsonl_text(items: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(item, sort_keys=True, default=str) + "\n" for item in items)


def _append_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
    _atomic_write_text(path, existing + _jsonl_text(items))


def _value_hash(value: Any) -> str:
    return hashlib.sha256(str(value if value is not None else "").encode("utf-8")).hexdigest()


def _key_for(row: dict[str, Any], key_columns: list[str]) -> dict[str, str]:
    return {key: str(row.get(key, "")) for key in key_columns}


def _key_tuple(row: dict[str, Any], key_columns: list[str]) -> tuple[str, ...]:
    return tuple(str(row.get(key, "")) for key in key_columns)


def _validate_keys(columns: list[str], key_columns: list[str]) -> None:
    if not key_columns:
        raise ValueError("at least one key column is required")
    missing = [key for key in key_columns if key not in columns]
    if missing:
        raise ValueError(f"key columns not present in CSV/artifact: {missing}")


def import_csv_as_artifact(
    job: Job,
    csv_path: Path | str,
    *,
    artifact_name: str,
    key_columns: list[str],
    target_columns: list[str] | None = None,
    schema: ArtifactSchema | None = None,
) -> Path:
    """Import an operator-supplied CSV into a structured artifact."""
    source = Path(csv_path)
    if not source.is_file():
        raise FileNotFoundError(f"input CSV not found: {source}")
    with source.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        _validate_keys(columns, key_columns)
        rows = [dict(row) for row in reader]
    if schema is None:
        schema = ArtifactSchema(
            name=artifact_name,
            schema_version=1,
            description="Imported CSV artifact.",
            columns=[
                ArtifactColumn(name=column, required=column in key_columns)
                for column in columns
            ],
        )
    csv_out = artifacts.write_table_artifact(
        job,
        artifact_name,
        schema=schema,
        rows=rows,
        source_coverage=f"imported {len(rows)} rows from {source.name}",
    )
    _write_meta(
        job,
        artifact_name,
        {
            "key_columns": key_columns,
            "target_columns": target_columns or [],
            "original_columns": columns,
            "original_row_count": len(rows),
            "input_csv_path": str(source),
        },
    )

    now = _now_epoch()
    provenance: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        key = _key_for(row, key_columns)
        for column in columns:
            value = row.get(column, "")
            provenance.append(
                {
                    "row_index": row_index,
                    "key": key,
                    "column": column,
                    "value_hash": _value_hash(value),
                    "action": "imported",
                    "source_kind": "operator_input",
                    "source_url": str(source),
                    "confidence": 1.0,
                    "task_id": None,
                    "timestamp": now,
                }
            )
    _atomic_write_text(
        _artifact_dir(job) / f"{artifact_name}{_PROVENANCE_SUFFIX}",
        _jsonl_text(provenance),
    )
    conflicts_path = _artifact_dir(job) / f"{artifact_name}{_CONFLICTS_SUFFIX}"
    if not conflicts_path.exists():
        _atomic_write_text(conflicts_path, "")
    return csv_out


def _schema_with_columns(schema: ArtifactSchema, columns: list[str]) -> ArtifactSchema:
    existing = [column.name for column in schema.columns]
    missing = [column for column in columns if column not in existing]
    if not missing:
        return schema
    return schema.model_copy(
        update={
            "schema_version": schema.schema_version + 1,
            "columns": [
                *schema.columns,
                *[ArtifactColumn(name=column, required=False) for column in missing],
            ],
        }
    )


def _update_values(
    update: dict[str, Any],
    key_columns: list[str],
    target_columns: list[str] | None = None,
) -> dict[str, Any]:
    values = update.get("values")
    if isinstance(values, dict):
        raw = dict(values)
    else:
        raw = {
            key: value
            for key, value in update.items()
            if key not in set(key_columns) | _UPDATE_META_KEYS
        }
    if target_columns is None:
        return raw
    allowed = set(target_columns)
    return {key: value for key, value in raw.items() if key in allowed}


def enrich_artifact(
    job: Job,
    name: str,
    *,
    updates: list[dict[str, Any]],
    key_columns: list[str] | None = None,
    target_columns: list[str] | None = None,
    overwrite_non_empty: bool = False,
    conflict_policy: str = "review_needed",
) -> dict[str, int]:
    """Apply sourced cell updates to an existing imported artifact."""
    if conflict_policy not in {"review_needed", "overwrite"}:
        raise ValueError("conflict_policy must be 'review_needed' or 'overwrite'")
    schema, rows = artifacts.read_artifact(job, name)
    meta = _read_meta(job, name)
    keys = key_columns or [str(k) for k in meta.get("key_columns") or []]
    targets = target_columns
    if targets is None and isinstance(meta.get("target_columns"), list):
        targets = [str(column) for column in meta.get("target_columns") or []]
    if targets == []:
        targets = None
    _validate_keys([column.name for column in schema.columns], keys)
    row_by_key = {_key_tuple(row, keys): row for row in rows}

    changed = 0
    conflicts = 0
    provenance: list[dict[str, Any]] = []
    conflict_rows: list[dict[str, Any]] = []
    new_columns: list[str] = []

    for update in updates:
        key = _key_tuple(update, keys)
        row = row_by_key.get(key)
        if row is None:
            continue
        values = _update_values(update, keys, targets)
        for column, incoming in values.items():
            if incoming in (None, ""):
                continue
            if column not in [c.name for c in schema.columns] and column not in new_columns:
                new_columns.append(column)
            current = row.get(column, "")
            now = int(update.get("timestamp") or _now_epoch())
            provenance_base = {
                "row_index": rows.index(row),
                "key": _key_for(row, keys),
                "column": column,
                "source_url": update.get("source_url"),
                "source_kind": update.get("source_kind"),
                "confidence": update.get("confidence"),
                "task_id": update.get("task_id"),
                "timestamp": now,
            }
            if current in (None, "") or overwrite_non_empty or conflict_policy == "overwrite":
                row[column] = incoming
                changed += 1
                provenance.append(
                    {
                        **provenance_base,
                        "action": "filled_empty"
                        if current in (None, "")
                        else "overwrote_non_empty",
                        "value_hash": _value_hash(incoming),
                    }
                )
                continue
            if str(current) == str(incoming):
                continue
            conflicts += 1
            conflict_rows.append(
                {
                    **provenance_base,
                    "action": "review_needed",
                    "existing_value": current,
                    "proposed_value": incoming,
                    "existing_value_hash": _value_hash(current),
                    "proposed_value_hash": _value_hash(incoming),
                }
            )

    if new_columns:
        schema = _schema_with_columns(schema, new_columns)
    artifacts.write_table_artifact(job, name, schema=schema, rows=rows)
    _write_meta(
        job,
        name,
        {
            **meta,
            "key_columns": keys,
            "target_columns": targets or [],
            "last_enriched_at_epoch": _now_epoch(),
            "last_enrichment_changed_cells": changed,
            "last_enrichment_conflicts": conflicts,
        },
    )
    artifact_dir = _artifact_dir(job)
    if provenance:
        _append_jsonl(artifact_dir / f"{name}{_PROVENANCE_SUFFIX}", provenance)
    if conflict_rows:
        _append_jsonl(artifact_dir / f"{name}{_CONFLICTS_SUFFIX}", conflict_rows)
    elif not (artifact_dir / f"{name}{_CONFLICTS_SUFFIX}").exists():
        _atomic_write_text(artifact_dir / f"{name}{_CONFLICTS_SUFFIX}", "")
    return {"changed": changed, "conflicts": conflicts, "rows": len(rows)}


def read_artifact_with_provenance(
    job: Job,
    name: str,
) -> tuple[ArtifactSchema, list[dict[str, Any]], list[dict[str, Any]]]:
    schema, rows = artifacts.read_artifact(job, name)
    path = _artifact_dir(job) / f"{name}{_PROVENANCE_SUFFIX}"
    provenance: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                provenance.append(item)
    return schema, rows, provenance


__all__ = [
    "enrich_artifact",
    "import_csv_as_artifact",
    "read_artifact_with_provenance",
]
