"""FastAPI wrapper around the stable programmatic research-agent API."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from research_agent import __version__
from research_agent import api as public_api
from research_agent.errors import InvalidGoal, JobAlreadyRunning, JobNotFound
from research_agent.storage import db
from research_agent.storage.jobs import DEFAULT_JOBS_ROOT


@dataclass(frozen=True)
class HttpApiContext:
    jobs_root: Path
    db_path: Path


class StartJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    domain: str = "general"
    budget_usd: float | None = None
    time_cap: int | None = None
    corpus: str | None = None
    disk_cap_gb: float = 10.0
    max_tasks: int | None = None
    local: bool = False
    translate_non_english: bool = False
    fragments: bool = False
    fresh_reset: bool = False
    inbox: bool = False
    intake: dict[str, Any] | None = None
    input_csv: str | None = None
    artifact_name: str = "candidates"
    key_columns: list[str] | None = None
    target_columns: list[str] | None = None
    update_existing: bool = False


class StopJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    graceful: bool = True


class ResumeJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = False
    replan: bool = False
    note: str | None = None


class SearchFindingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    job_id: str | None = None
    kind: Literal["findings", "sources", "both"] = "both"
    fts_only: bool = False
    models_config: dict[str, Any] | None = None


class ExportJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zip: bool = False
    md_bundle: bool = False
    csv_artifact: str | None = None
    out: str | None = None
    include_history: bool = False


def _authorize_request() -> None:
    """Auth insertion point for a future consumer-specific HTTP auth model."""

    return None


def _get_context(request: Request) -> HttpApiContext:
    return request.app.state.http_api_context


ContextDep = Annotated[HttpApiContext, Depends(_get_context)]


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


router = APIRouter(dependencies=[Depends(_authorize_request)])


@router.post(
    "/jobs",
    operation_id="start_job",
    response_model=public_api.StartJobResult,
)
def start_job(payload: StartJobRequest, context: ContextDep) -> public_api.StartJobResult:
    args = payload.model_dump(exclude_none=True)
    if payload.input_csv is not None:
        args["input_csv"] = Path(payload.input_csv)
    return public_api.start_job(**args, jobs_root=context.jobs_root, db_path=context.db_path)


@router.get(
    "/jobs",
    operation_id="list_jobs",
    response_model=list[public_api.JobSummary],
)
def list_jobs(
    context: ContextDep,
    status: str | None = None,
) -> list[public_api.JobSummary]:
    return public_api.list_jobs(status=status, jobs_root=context.jobs_root, db_path=context.db_path)


@router.get(
    "/jobs/{job_id}/status",
    operation_id="get_job_status",
    response_model=public_api.JobStatus,
)
def get_job_status(job_id: str, context: ContextDep) -> public_api.JobStatus:
    return public_api.get_job_status(job_id, jobs_root=context.jobs_root, db_path=context.db_path)


@router.post(
    "/jobs/{job_id}/stop",
    operation_id="stop_job",
    response_model=public_api.StopJobResult,
)
def stop_job(
    job_id: str,
    payload: StopJobRequest,
    context: ContextDep,
) -> public_api.StopJobResult:
    return public_api.stop_job(
        job_id,
        graceful=payload.graceful,
        jobs_root=context.jobs_root,
        db_path=context.db_path,
    )


@router.post(
    "/jobs/{job_id}/resume",
    operation_id="resume_job",
    response_model=public_api.ResumeJobResult,
)
def resume_job(
    job_id: str,
    payload: ResumeJobRequest,
    context: ContextDep,
) -> public_api.ResumeJobResult:
    return public_api.resume_job(
        job_id,
        force=payload.force,
        replan=payload.replan,
        note=payload.note,
        jobs_root=context.jobs_root,
        db_path=context.db_path,
    )


@router.get(
    "/jobs/{job_id}/report",
    operation_id="get_report",
    response_model=public_api.ReportResult,
)
def get_report(job_id: str, context: ContextDep) -> public_api.ReportResult:
    return public_api.get_report(job_id, jobs_root=context.jobs_root)


@router.get(
    "/jobs/{job_id}/findings",
    operation_id="get_findings",
    response_model=list[public_api.FindingResult],
)
def get_findings(job_id: str, context: ContextDep) -> list[public_api.FindingResult]:
    return public_api.get_findings(job_id, jobs_root=context.jobs_root)


@router.post(
    "/findings/search",
    operation_id="search_findings",
    response_model=list[public_api.SearchFindingResult],
)
def search_findings(
    payload: SearchFindingsRequest,
    context: ContextDep,
) -> list[public_api.SearchFindingResult]:
    return public_api.search_findings(
        payload.query,
        job_id=payload.job_id,
        kind=payload.kind,
        fts_only=payload.fts_only,
        jobs_root=context.jobs_root,
        db_path=context.db_path,
        models_config=payload.models_config,
    )


@router.post(
    "/jobs/{job_id}/export",
    operation_id="export_job",
    response_model=public_api.ExportResult,
)
def export_job(
    job_id: str,
    payload: ExportJobRequest,
    context: ContextDep,
) -> public_api.ExportResult:
    return public_api.export_job(
        job_id,
        zip=payload.zip,
        md_bundle=payload.md_bundle,
        csv_artifact=payload.csv_artifact,
        out=Path(payload.out) if payload.out is not None else None,
        include_history=payload.include_history,
        jobs_root=context.jobs_root,
        db_path=context.db_path,
    )


def create_app(
    *,
    jobs_root: Path | str = DEFAULT_JOBS_ROOT,
    db_path: Path | str = db.DEFAULT_DB_PATH,
) -> FastAPI:
    app = FastAPI(
        title="muckwire HTTP API",
        version=__version__,
        description="Lifecycle-only REST wrapper around research_agent.api.",
    )
    app.state.http_api_context = HttpApiContext(jobs_root=Path(jobs_root), db_path=Path(db_path))

    @app.exception_handler(InvalidGoal)
    async def _invalid_goal_handler(_request: Request, exc: InvalidGoal) -> JSONResponse:
        return _error(400, str(exc))

    @app.exception_handler(JobNotFound)
    async def _job_not_found_handler(_request: Request, exc: JobNotFound) -> JSONResponse:
        return _error(404, str(exc))

    @app.exception_handler(JobAlreadyRunning)
    async def _job_already_running_handler(
        _request: Request,
        exc: JobAlreadyRunning,
    ) -> JSONResponse:
        return _error(409, str(exc))

    @app.exception_handler(Exception)
    async def _unexpected_handler(_request: Request, _exc: Exception) -> JSONResponse:
        return _error(500, "internal server error")

    app.include_router(router)
    return app


app = create_app()


__all__ = [
    "ExportJobRequest",
    "ResumeJobRequest",
    "SearchFindingsRequest",
    "StartJobRequest",
    "StopJobRequest",
    "app",
    "create_app",
]
