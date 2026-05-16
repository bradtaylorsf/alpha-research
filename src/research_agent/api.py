"""Stable programmatic API for embedding the research agent."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import zipfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from research_agent import daemon
from research_agent.contract import iter_findings, read_job, read_report, tail_events
from research_agent.errors import InvalidGoal, JobAlreadyRunning, JobNotFound
from research_agent.storage import db
from research_agent.storage import jobs as jobs_store
from research_agent.storage.enrichment import import_csv_as_artifact
from research_agent.storage.export import export_csv, export_md_bundle, export_zip
from research_agent.storage.jobs import (
    DEFAULT_JOBS_ROOT,
    RESUME_REPLAN_FILE,
    Job,
    _atomic_write_json,
)
from research_agent.storage.search import ALLOWED_KINDS, search_fts, search_hybrid
from research_agent.tools.models import Source
from research_agent.ui import render


class StartJobResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    daemon_pid: int
    reused: bool = False
    archived_report: str | None = None


class StopJobResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stopped: bool


class ResumeJobResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resumed: bool
    daemon_pid: int


class JobSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    job_id: str = Field(alias="id")
    goal: str
    status: str
    created_at: int
    updated_at: int | None = Field(default=None, alias="last_activity_at")
    domain: str | None = None
    spent_usd: float = Field(default=0.0, alias="cost_so_far_usd")
    completion_reason: str | None = None


class JobStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: str
    spent_usd: float
    time_elapsed: int | None
    current_iteration: int
    last_event_summary: str | None = None


class ReportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    report_md: str
    sources: list[Source] = Field(default_factory=list)


class FindingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = "finding"
    title: str
    body: str
    citations: list[int] = Field(default_factory=list)
    source_url: str | None = None
    created_at: int


class SearchFindingResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    score: float | None = None
    kind: str
    snippet: str | None = None
    source_url: str | None = None
    job_id: str
    id: int | None = None
    md_path: str | None = None
    title_or_claim: str | None = None


class ExportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    bytes: int


def _normalize_goal(goal: str | None) -> str:
    if not isinstance(goal, str) or not goal.strip():
        raise InvalidGoal("goal must be a non-empty string")
    return goal.strip()


def _load_job(job_id: str, *, jobs_root: Path | str, db_path: Path | str) -> Job:
    try:
        return Job.load(job_id, jobs_root=jobs_root, db_path=db_path)
    except Exception as exc:
        raise JobNotFound(f"job not found: {job_id}") from exc


def _job_root(job_id: str, jobs_root: Path | str) -> Path:
    root = Path(jobs_root) / job_id
    if (root / "job.json").exists():
        return root
    jobs_root_p = Path(jobs_root)
    if jobs_root_p.is_dir():
        for child in jobs_root_p.iterdir():
            if not child.is_dir() or not (child / "job.json").exists():
                continue
            try:
                if read_job(child).id == job_id:
                    return child
            except Exception:
                continue
    raise JobNotFound(f"job not found: {job_id}")


def _fallback_job_summaries(jobs_root: Path | str) -> list[JobSummary]:
    root = Path(jobs_root)
    if not root.is_dir():
        return []
    rows: list[JobSummary] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / "job.json").exists():
            continue
        meta = read_job(child)
        rows.append(
            JobSummary(
                id=meta.id,
                goal=meta.goal,
                domain=meta.domain,
                status=meta.status,
                created_at=meta.created_at,
                last_activity_at=meta.last_activity_at,
                cost_so_far_usd=0.0,
                completion_reason=meta.completion_reason,
            )
        )
    return sorted(rows, key=lambda row: row.created_at, reverse=True)


def _last_event_summary(root: Path) -> str | None:
    events = list(tail_events(root))
    if not events:
        return None
    event = events[-1]
    return f"{event.level} {event.kind}"


def _is_daemon_alive(job_id: str, jobs_root: Path | str) -> bool:
    try:
        return daemon.is_daemon_alive(job_id, jobs_root=jobs_root)
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        return daemon.is_daemon_alive(job_id)


def _spawn_daemon(job_id: str, jobs_root: Path | str) -> int:
    try:
        return daemon.spawn_daemon(job_id, jobs_root=jobs_root)
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        return daemon.spawn_daemon(job_id)


def _source_url_for_ids(root: Path, source_ids: list[int]) -> str | None:
    if not source_ids:
        return None
    try:
        report = read_report(root)
    except Exception:
        return None
    idx = source_ids[0] - 1
    if idx < 0 or idx >= len(report.sources):
        return None
    return report.sources[idx].url or None


def start_job(
    goal: str,
    *,
    domain: str = "general",
    budget_usd: float | None = None,
    time_cap: int | None = None,
    corpus: str | None = None,
    disk_cap_gb: float = 10.0,
    max_tasks: int | None = None,
    local: bool = False,
    translate_non_english: bool = False,
    fragments: bool = False,
    fresh_reset: bool = False,
    inbox: bool = False,
    intake: dict[str, Any] | None = None,
    input_csv: Path | None = None,
    artifact_name: str = "candidates",
    key_columns: list[str] | None = None,
    target_columns: list[str] | None = None,
    update_existing: bool = False,
    jobs_root: Path | str = DEFAULT_JOBS_ROOT,
    db_path: Path | str = db.DEFAULT_DB_PATH,
) -> StartJobResult:
    """Create a job and spawn its daemon.

    Example:
        ``result = start_job("Investigate Widget Co", budget_usd=5.0)``
    """

    goal_text = _normalize_goal(goal)
    if max_tasks is not None and max_tasks < 1:
        raise InvalidGoal("max_tasks must be >= 1")
    if input_csv is not None and not input_csv.is_file():
        raise InvalidGoal(f"input_csv not found: {input_csv}")
    key_cols = list(key_columns or [])
    if input_csv is not None and not key_cols:
        raise InvalidGoal("key_columns is required when input_csv is set")

    if intake is None:
        intake_data: dict[str, Any] = {
            "goal": goal_text,
            "domain": domain,
            "time_cap_hours": time_cap,
            "budget_cap_usd": budget_usd,
            "disk_cap_gb": disk_cap_gb,
            "translate_non_english": translate_non_english,
            "fragments": fragments,
            "inbox": inbox,
        }
        if corpus:
            intake_data["corpus"] = corpus
    else:
        intake_data = dict(intake)
        intake_data["goal"] = goal_text

    if max_tasks is not None:
        intake_data["max_tasks"] = max_tasks
    if local:
        local_cfg = Path("config/models.local.yaml")
        if not local_cfg.exists():
            raise InvalidGoal(f"--local requires {local_cfg} (not found)")
        os.environ["RESEARCH_MODELS_CONFIG"] = str(local_cfg)
        os.environ["RESEARCH_DAEMON_SKIP_HEALTH_CHECKS"] = "1"
        intake_data["local"] = True
    if fragments or intake_data.get("fragments"):
        os.environ["RESEARCH_FRAGMENT_SYNTH"] = "1"
        intake_data["fragments"] = True
    if input_csv is not None:
        intake_data["input_csv_artifact"] = artifact_name
        intake_data["enrichment"] = {
            "artifact": artifact_name,
            "input_csv": str(input_csv),
            "key_columns": key_cols,
            "target_columns": list(target_columns or []),
            "overwrite_non_empty": bool(update_existing),
        }

    db.migrate(path=db_path).close()
    jobs_root_p = Path(jobs_root)
    existing = (
        Job.find_by_goal_slug(goal_text, jobs_root=jobs_root_p, db_path=db_path)
        if not fresh_reset
        else None
    )
    archived: Path | None = None
    reused = existing is not None
    if existing is not None:
        if _is_daemon_alive(existing.id, jobs_root_p):
            raise JobAlreadyRunning(f"job {existing.id} is already running")
        archived = existing.archive_and_soft_reset()
        existing.intake = intake_data
        _atomic_write_json(existing.root / "intake.json", intake_data)
        meta_path = existing.root / "job.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["intake"] = intake_data
        meta["domain"] = intake_data.get("domain") or meta.get("domain")
        _atomic_write_json(meta_path, meta)
        intake_json = json.dumps(intake_data, sort_keys=True)
        conn = db.connect(db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE jobs SET intake_json = ?, time_cap_hours = ?,"
                    " budget_cap_usd = ?, aggressiveness = ?, domain = ?"
                    " WHERE id = ?",
                    (
                        intake_json,
                        intake_data.get("time_cap_hours"),
                        intake_data.get("budget_cap_usd"),
                        intake_data.get("aggressiveness"),
                        intake_data.get("domain") or existing.domain,
                        existing.id,
                    ),
                )
        finally:
            conn.close()
        job = existing
    else:
        try:
            job = Job.create(intake_data, jobs_root=jobs_root_p, db_path=db_path)
        except FileExistsError as exc:
            raise JobAlreadyRunning(str(exc)) from exc
        except ValueError as exc:
            raise InvalidGoal(str(exc)) from exc

    if input_csv is not None:
        import_csv_as_artifact(
            job,
            input_csv,
            artifact_name=artifact_name,
            key_columns=key_cols,
            target_columns=list(target_columns or []),
        )

    pid = _spawn_daemon(job.id, jobs_root_p)
    return StartJobResult(
        job_id=job.id,
        daemon_pid=pid,
        reused=reused,
        archived_report=str(archived) if archived is not None else None,
    )


def get_job_status(
    job_id: str,
    *,
    jobs_root: Path | str = DEFAULT_JOBS_ROOT,
    db_path: Path | str = db.DEFAULT_DB_PATH,
) -> JobStatus:
    """Return current status and lightweight progress for one job.

    Example:
        ``status = get_job_status("2026-05-16-investigate-widget-co")``
    """

    try:
        job = _load_job(job_id, jobs_root=jobs_root, db_path=db_path)
        data = render.load_status_data(job, db_path=Path(db_path))
        started_at = data.get("started_at")
        elapsed = int(time.time()) - int(started_at) if started_at else None
        recent = data.get("recent_events") or []
        last_event = recent[0] if recent else None
        summary = None
        if last_event:
            summary = f"{last_event.get('level')} {last_event.get('kind')}"
        return JobStatus(
            job_id=job.id,
            status=job.status,
            spent_usd=float(data.get("cost") or 0.0),
            time_elapsed=elapsed,
            current_iteration=int(data.get("plan_version") or 0),
            last_event_summary=summary,
        )
    except JobNotFound:
        root = _job_root(job_id, jobs_root)
        meta = read_job(root)
        elapsed = int(time.time()) - int(meta.created_at)
        return JobStatus(
            job_id=meta.id,
            status=meta.status,
            spent_usd=0.0,
            time_elapsed=elapsed,
            current_iteration=0,
            last_event_summary=_last_event_summary(root),
        )


def list_jobs(
    status: str | None = None,
    *,
    jobs_root: Path | str = DEFAULT_JOBS_ROOT,
    db_path: Path | str = db.DEFAULT_DB_PATH,
) -> list[JobSummary]:
    """List research jobs newest first.

    Example:
        ``jobs = list_jobs(status="completed")``
    """

    try:
        rows = jobs_store.list_jobs(status=status, db_path=db_path)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        rows = []
    except Exception:
        rows = []
    if rows:
        return [JobSummary.model_validate(row) for row in rows]
    summaries = _fallback_job_summaries(jobs_root)
    if status is not None:
        summaries = [row for row in summaries if row.status == status]
    return summaries


def stop_job(
    job_id: str,
    *,
    graceful: bool = True,
    jobs_root: Path | str = DEFAULT_JOBS_ROOT,
    db_path: Path | str = db.DEFAULT_DB_PATH,
) -> StopJobResult:
    """Stop a job gracefully or by killing its daemon process.

    Example:
        ``stop_job("2026-05-16-investigate-widget-co")``
    """

    job = _load_job(job_id, jobs_root=jobs_root, db_path=db_path)
    if graceful:
        job.request_stop()
        return StopJobResult(stopped=True)
    try:
        job.kill()
    except FileNotFoundError as exc:
        raise JobNotFound(f"no daemon PID file for job {job_id}") from exc
    try:
        (job.root / "daemon.pid").unlink()
    except FileNotFoundError:
        pass
    return StopJobResult(stopped=True)


def resume_job(
    job_id: str,
    *,
    force: bool = False,
    replan: bool = False,
    note: str | None = None,
    jobs_root: Path | str = DEFAULT_JOBS_ROOT,
    db_path: Path | str = db.DEFAULT_DB_PATH,
) -> ResumeJobResult:
    """Restart a stranded job daemon.

    Example:
        ``resume_job("2026-05-16-investigate-widget-co", force=True)``
    """

    job = _load_job(job_id, jobs_root=jobs_root, db_path=db_path)
    if _is_daemon_alive(job.id, jobs_root):
        raise JobAlreadyRunning(f"job {job_id} is already running")
    if job.status in {"completed", "failed"} and not force:
        raise InvalidGoal(f"job {job_id} is {job.status}; pass --force to resume anyway")
    if note and not replan:
        raise InvalidGoal("--note requires --replan")
    try:
        (job.root / "STOP").unlink()
    except FileNotFoundError:
        pass
    if replan:
        payload: dict[str, Any] = {}
        if note:
            payload["note"] = note
        _atomic_write_json(job.root / RESUME_REPLAN_FILE, payload)
    pid = _spawn_daemon(job.id, jobs_root)
    return ResumeJobResult(resumed=True, daemon_pid=pid)


def get_report(
    job_id: str,
    *,
    jobs_root: Path | str = DEFAULT_JOBS_ROOT,
) -> ReportResult:
    """Read a job's current report.

    Example:
        ``report = get_report("2026-05-16-investigate-widget-co")``
    """

    root = _job_root(job_id, jobs_root)
    try:
        report = read_report(root)
    except Exception as exc:
        raise JobNotFound(f"report.md not present for job {job_id}") from exc
    return ReportResult(job_id=job_id, report_md=report.report_md, sources=report.sources)


def get_findings(
    job_id: str,
    *,
    jobs_root: Path | str = DEFAULT_JOBS_ROOT,
) -> list[FindingResult]:
    """Read finding files from a job folder.

    Example:
        ``findings = get_findings("2026-05-16-investigate-widget-co")``
    """

    root = _job_root(job_id, jobs_root)
    out: list[FindingResult] = []
    for finding in iter_findings(root):
        out.append(
            FindingResult(
                title=finding.claim,
                body=finding.body_md,
                citations=finding.source_ids,
                source_url=_source_url_for_ids(root, finding.source_ids),
                created_at=finding.created_at,
            )
        )
    return out


def search_findings(
    query: str,
    *,
    job_id: str | None = None,
    kind: Literal["findings", "sources", "both"] = "both",
    fts_only: bool = False,
    jobs_root: Path | str = DEFAULT_JOBS_ROOT,
    db_path: Path | str = db.DEFAULT_DB_PATH,
    models_config: dict[str, Any] | None = None,
) -> list[SearchFindingResult]:
    """Search findings and sources across jobs.

    Example:
        ``hits = search_findings("quantum", fts_only=True)``
    """

    if not isinstance(query, str) or not query.strip():
        raise InvalidGoal("query must be a non-empty string")
    if kind not in ALLOWED_KINDS:
        raise InvalidGoal(f"kind must be one of {list(ALLOWED_KINDS)}; got {kind!r}")
    if job_id is not None:
        _job_root(job_id, jobs_root)
    try:
        if fts_only:
            rows = search_fts(query, job_id=job_id, kind=kind, db_path=db_path)
        else:
            rows = search_hybrid(
                query,
                job_id=job_id,
                kind=kind,
                db_path=db_path,
                models_config=models_config,
            )
    except Exception:
        has_folder_surface = Path(jobs_root) != DEFAULT_JOBS_ROOT and Path(jobs_root).exists()
        if Path(db_path).exists() and not has_folder_surface:
            raise
        rows = []

    if not rows:
        rows = _search_fixture_findings(query, job_id=job_id, jobs_root=jobs_root)
    return [SearchFindingResult.model_validate(row) for row in rows]


def _search_fixture_findings(
    query: str,
    *,
    job_id: str | None,
    jobs_root: Path | str,
) -> list[dict[str, Any]]:
    tokens = [token.lower() for token in query.split() if token.strip()]
    if not tokens:
        return []
    roots: list[Path]
    if job_id:
        roots = [_job_root(job_id, jobs_root)]
    else:
        root = Path(jobs_root)
        roots = [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
    rows: list[dict[str, Any]] = []
    for root in roots:
        for finding in iter_findings(root):
            haystack = f"{finding.claim}\n{finding.body_md}".lower()
            if not all(token in haystack for token in tokens):
                continue
            rows.append(
                {
                    "score": 1.0,
                    "kind": "finding",
                    "snippet": finding.claim,
                    "source_url": _source_url_for_ids(root, finding.source_ids),
                    "job_id": read_job(root).id,
                    "id": finding.id,
                    "md_path": finding.md_path,
                    "title_or_claim": finding.claim,
                }
            )
    return rows


def export_job(
    job_id: str,
    *,
    zip: bool = False,  # noqa: A002 - mirrors CLI flag
    md_bundle: bool = False,
    csv_artifact: str | None = None,
    out: Path | None = None,
    include_history: bool = False,
    jobs_root: Path | str = DEFAULT_JOBS_ROOT,
    db_path: Path | str = db.DEFAULT_DB_PATH,
) -> ExportResult:
    """Export a job as a zip, markdown bundle, or CSV artifact.

    Example:
        ``bundle = export_job(job_id, md_bundle=True, out=Path("job.md"))``
    """

    selected = sum([bool(zip), bool(md_bundle), bool(csv_artifact)])
    if selected != 1:
        raise InvalidGoal("exactly one of zip, md_bundle, or csv_artifact must be set")
    default_name = (
        f"{job_id}-{csv_artifact}.csv"
        if csv_artifact
        else f"{job_id}{'.zip' if zip else '.md'}"
    )
    out_path = Path.cwd() / default_name if out is None else Path(out)
    if out_path.exists() and out_path.is_dir():
        out_path = out_path / default_name

    try:
        job = _load_job(job_id, jobs_root=jobs_root, db_path=db_path)
    except JobNotFound:
        written = _export_folder_without_db(
            _job_root(job_id, jobs_root),
            out_path,
            zip=zip,
            md_bundle=md_bundle,
            include_history=include_history,
        )
        return ExportResult(path=str(written), bytes=written.stat().st_size)

    if zip:
        written = export_zip(job, out_path, include_history=include_history)
    elif md_bundle:
        written = export_md_bundle(job, out_path, include_history=include_history)
    else:
        assert csv_artifact is not None
        written = export_csv(job, csv_artifact, out_path)
    return ExportResult(path=str(written), bytes=Path(written).stat().st_size)


def _export_folder_without_db(
    root: Path,
    out_path: Path,
    *,
    zip: bool,
    md_bundle: bool,
    include_history: bool,
) -> Path:
    if zip:
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(root)
                if not include_history and rel.parts and rel.parts[0] == "report.history":
                    continue
                zf.write(path, f"{root.name}/{rel.as_posix()}")
        os.replace(tmp, out_path)
        return out_path
    if md_bundle:
        report = read_report(root)
        body = report.report_md if report.report_md.endswith("\n") else report.report_md + "\n"
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, out_path)
        return out_path
    raise JobNotFound("CSV artifact export requires a DB-backed job")


__all__ = [
    "ExportResult",
    "FindingResult",
    "JobStatus",
    "JobSummary",
    "ReportResult",
    "ResumeJobResult",
    "SearchFindingResult",
    "StartJobResult",
    "StopJobResult",
    "export_job",
    "get_findings",
    "get_job_status",
    "get_report",
    "list_jobs",
    "resume_job",
    "search_findings",
    "start_job",
    "stop_job",
]
