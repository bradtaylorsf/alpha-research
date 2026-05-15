"""Structured per-job artifacts (issue #304)."""

from __future__ import annotations

import csv
import io
import json
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from research_agent.storage.jobs import Job, _atomic_write_text

_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class ArtifactColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    required: bool = False


class ArtifactSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    schema_version: int = Field(ge=1)
    columns: list[ArtifactColumn]
    description: str | None = None


CANDIDATE_ROSTER_SCHEMA = ArtifactSchema(
    name="candidates",
    schema_version=1,
    description="Candidate roster table.",
    columns=[
        ArtifactColumn(name="state", required=True),
        ArtifactColumn(name="chamber", required=True),
        ArtifactColumn(name="district_or_seat"),
        ArtifactColumn(name="candidate_name", required=True),
        ArtifactColumn(name="party"),
        ArtifactColumn(name="candidate_status"),
        ArtifactColumn(name="confidence"),
        ArtifactColumn(name="official_campaign_website"),
        ArtifactColumn(name="source_url", required=True),
        ArtifactColumn(name="source_kind"),
        ArtifactColumn(name="source_retrieved_at"),
        ArtifactColumn(name="notes"),
    ],
)


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not _ARTIFACT_NAME_RE.match(name):
        raise ValueError(
            "artifact name must start with a letter/number and contain only "
            "letters, numbers, underscores, or hyphens"
        )
    return name


def _artifact_dir(job: Job) -> Path:
    path = job.root / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fieldnames(schema: ArtifactSchema) -> list[str]:
    return [column.name for column in schema.columns]


def _rows_for_csv(schema: ArtifactSchema, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = _fieldnames(schema)
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({field: row.get(field, "") for field in fields})
    return out


def _csv_text(schema: ArtifactSchema, rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_fieldnames(schema), extrasaction="ignore")
    writer.writeheader()
    for row in _rows_for_csv(schema, rows):
        writer.writerow(row)
    return buffer.getvalue()


def write_table_artifact(
    job: Job,
    name: str,
    *,
    schema: ArtifactSchema,
    rows: list[dict[str, Any]],
    source_coverage: str | None = None,
) -> Path:
    """Write schema, JSONL, CSV, and metadata sidecars for a table artifact."""
    artifact_name = _validate_name(name)
    if schema.name != artifact_name:
        schema = schema.model_copy(update={"name": artifact_name})
    artifact_dir = _artifact_dir(job)

    schema_path = artifact_dir / f"{artifact_name}.schema.json"
    jsonl_path = artifact_dir / f"{artifact_name}.jsonl"
    csv_path = artifact_dir / f"{artifact_name}.csv"
    meta_path = artifact_dir / f"{artifact_name}.meta.json"

    _atomic_write_text(
        schema_path,
        json.dumps(schema.model_dump(), indent=2, sort_keys=True) + "\n",
    )
    jsonl = "".join(json.dumps(row, sort_keys=True, default=str) + "\n" for row in rows)
    _atomic_write_text(jsonl_path, jsonl)
    _atomic_write_text(csv_path, _csv_text(schema, rows))
    _atomic_write_text(
        meta_path,
        json.dumps(
            {
                "artifact_name": artifact_name,
                "schema_version": schema.schema_version,
                "row_count": len(rows),
                "generated_at_epoch": int(time.time()),
                "source_job_id": job.id,
                "source_coverage": source_coverage or "",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return csv_path


def read_artifact(job: Job, name: str) -> tuple[ArtifactSchema, list[dict[str, Any]]]:
    artifact_name = _validate_name(name)
    artifact_dir = _artifact_dir(job)
    schema_path = artifact_dir / f"{artifact_name}.schema.json"
    jsonl_path = artifact_dir / f"{artifact_name}.jsonl"
    if not schema_path.exists() or not jsonl_path.exists():
        raise FileNotFoundError(f"artifact {artifact_name!r} not found for job {job.id}")
    schema = ArtifactSchema.model_validate_json(schema_path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            rows.append(item)
    return schema, rows


def list_artifacts(job: Job) -> list[dict[str, Any]]:
    artifact_dir = _artifact_dir(job)
    out: list[dict[str, Any]] = []
    for meta_path in sorted(artifact_dir.glob("*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        name = str(meta.get("artifact_name") or meta_path.name.removesuffix(".meta.json"))
        csv_path = artifact_dir / f"{name}.csv"
        out.append(
            {
                "name": name,
                "schema_version": meta.get("schema_version"),
                "row_count": meta.get("row_count"),
                "generated_at_epoch": meta.get("generated_at_epoch"),
                "source_coverage": meta.get("source_coverage") or "",
                "csv_path": str(csv_path.relative_to(job.root)),
            }
        )
    return out


__all__ = [
    "ArtifactColumn",
    "ArtifactSchema",
    "CANDIDATE_ROSTER_SCHEMA",
    "list_artifacts",
    "read_artifact",
    "write_table_artifact",
]
