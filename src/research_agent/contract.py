"""Read-only helpers for the stable ``jobs/<job-id>/`` folder contract."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from research_agent.observability.events import Event
from research_agent.storage.jobs import JOB_SCHEMA_VERSION
from research_agent.tools.models import Source


class ContractReadError(ValueError):
    """Raised when a job-folder artifact is missing or violates the contract."""


class JobMetadata(BaseModel):
    """Parsed ``job.json`` metadata."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = JOB_SCHEMA_VERSION
    id: str
    goal: str
    domain: str | None = None
    status: str
    created_at: int
    last_activity_at: int | None = None
    completion_reason: str | None = None
    intake: dict[str, Any] = Field(default_factory=dict)


class Finding(BaseModel):
    """Parsed finding markdown plus its JSON sidecar."""

    model_config = ConfigDict(extra="allow")

    id: int
    claim: str
    confidence: float
    source_ids: list[int]
    contradicts: list[int] | None = None
    tags: list[str] | None = None
    target_fragments: list[str] | None = None
    md_path: str
    created_at: int
    body_md: str


class Report(BaseModel):
    """Parsed ``report.md`` with any materialized source sidecars."""

    model_config = ConfigDict(extra="forbid")

    job_id: str | None = None
    report_md: str
    path: str
    sources: list[Source] = Field(default_factory=list)


def _as_path(path: str | Path) -> Path:
    return path if isinstance(path, Path) else Path(path)


def _job_root(path: str | Path) -> Path:
    p = _as_path(path)
    if p.is_dir():
        return p
    if p.name == "job.json":
        return p.parent
    return p


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractReadError(f"missing contract file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractReadError(f"invalid JSON in contract file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractReadError(f"contract file must contain a JSON object: {path}")
    return data


def _safe_relative_path(value: str, *, field_name: str) -> Path:
    rel = Path(value)
    if rel.is_absolute() or ".." in rel.parts:
        raise ContractReadError(f"{field_name} must stay inside the job folder: {value!r}")
    return rel


def read_job(path: str | Path) -> JobMetadata:
    """Read ``job.json`` from a job folder.

    Example:
        ``job = read_job(Path("jobs/2026-05-16-example"))``
    """

    root = _job_root(path)
    job_json = root if root.name == "job.json" and root.is_file() else root / "job.json"
    return JobMetadata.model_validate(_read_json(job_json))


def iter_findings(path: str | Path) -> Iterable[Finding]:
    """Yield findings from ``findings/*.json`` in monotonic filename order.

    Example:
        ``claims = [finding.claim for finding in iter_findings(job_root)]``
    """

    root = _job_root(path)
    findings_dir = root / "findings"

    def _iter() -> Iterator[Finding]:
        if not findings_dir.is_dir():
            return
        for sidecar in sorted(findings_dir.glob("[0-9][0-9][0-9][0-9][0-9][0-9].json")):
            data = _read_json(sidecar)
            md_rel = str(data.get("md_path") or f"findings/{sidecar.stem}.md")
            md_path = root / _safe_relative_path(md_rel, field_name="finding md_path")
            try:
                body_md = md_path.read_text(encoding="utf-8")
            except FileNotFoundError as exc:
                raise ContractReadError(f"finding sidecar points at missing md: {md_path}") from exc
            yield Finding.model_validate({**data, "md_path": md_rel, "body_md": body_md})

    return _iter()


def _source_sidecars(root: Path) -> list[Path]:
    sources_dir = root / "sources" if (root / "sources").is_dir() else root
    if not sources_dir.is_dir():
        return []
    return sorted(
        p
        for p in sources_dir.glob("*.json")
        if p.is_file() and not p.name.endswith(".tmp")
    )


def _source_from_sidecar(sidecar: Path) -> Source:
    data = _read_json(sidecar)
    md_rel = data.get("md_path")
    if isinstance(md_rel, str) and md_rel:
        rel = _safe_relative_path(md_rel, field_name="source md_path")
        candidate = sidecar.parent.parent / rel
        md_path = candidate if candidate.exists() else sidecar.with_suffix(".md")
    else:
        md_path = sidecar.with_suffix(".md")
    try:
        cleaned_text = md_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ContractReadError(f"source sidecar points at missing md: {md_path}") from exc

    fetched_raw = data.get("fetched_at")
    if isinstance(fetched_raw, (int, float)) and not isinstance(fetched_raw, bool):
        fetched_at = datetime.fromtimestamp(int(fetched_raw), UTC)
    elif isinstance(fetched_raw, str):
        try:
            fetched_at = datetime.fromisoformat(fetched_raw)
        except ValueError as exc:
            raise ContractReadError(f"invalid fetched_at in {sidecar}: {fetched_raw!r}") from exc
    else:
        raise ContractReadError(f"missing fetched_at in {sidecar}")

    archive_url = data.get("archive_url") or None
    return Source.model_validate(
        {
            "url": data.get("url") or "",
            "title": data.get("title") or "",
            "cleaned_text": cleaned_text,
            "raw_html": data.get("raw_html"),
            "fetched_at": fetched_at,
            "source_kind": data.get("source_kind") or data.get("kind"),
            "archive_url": archive_url,
            "metadata": data.get("metadata") or {},
        }
    )


def read_source(path: str | Path) -> Source:
    """Read one source from ``sources/<sha256>.json`` or ``sources/<sha256>.md``.

    Example:
        ``source = read_source(job_root / "sources" / "<sha>.json")``
    """

    p = _as_path(path)
    if p.is_dir():
        sidecars = _source_sidecars(p)
        if not sidecars:
            raise ContractReadError(f"no source sidecars under {p}")
        if len(sidecars) > 1:
            raise ContractReadError(
                f"source path must identify one source; found {len(sidecars)} under {p}"
            )
        return _source_from_sidecar(sidecars[0])
    if p.suffix == ".md":
        sidecar = p.with_suffix(".json")
    elif p.suffix == ".json":
        sidecar = p
    else:
        sidecar = p.with_suffix(".json")
    return _source_from_sidecar(sidecar)


def read_report(path: str | Path) -> Report:
    """Read ``report.md`` from a job folder and load materialized sources.

    Example:
        ``report = read_report(Path("jobs/2026-05-16-example"))``
    """

    root = _job_root(path)
    report_path = root if root.name == "report.md" and root.is_file() else root / "report.md"
    try:
        report_md = report_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ContractReadError(f"missing report.md: {report_path}") from exc

    job_id: str | None = None
    try:
        job_id = read_job(root).id
    except ContractReadError:
        job_id = None

    sources: list[Source] = []
    for sidecar in _source_sidecars(root):
        sources.append(_source_from_sidecar(sidecar))
    return Report(job_id=job_id, report_md=report_md, path=str(report_path), sources=sources)


def tail_events(path: str | Path) -> Iterable[Event]:
    """Yield existing ``events.jsonl`` entries from a job folder.

    Example:
        ``events = list(tail_events(job_root))``
    """

    root = _job_root(path)
    events_path = (
        root
        if root.name == "events.jsonl" and root.is_file()
        else root / "events.jsonl"
    )

    def _iter() -> Iterator[Event]:
        try:
            lines = events_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError as exc:
            raise ContractReadError(f"missing events.jsonl: {events_path}") from exc
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            yield Event.model_validate_json(line)

    return _iter()


__all__ = [
    "ContractReadError",
    "Finding",
    "JobMetadata",
    "Report",
    "read_job",
    "iter_findings",
    "read_report",
    "read_source",
    "tail_events",
]
