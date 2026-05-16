"""Research agent package - autonomous CLI research tool.

The public API is re-exported lazily so a simple ``import research_agent`` does
not also import daemon/orchestrator modules. Embedders can still use
``from research_agent import start_job`` and receive the same object.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from research_agent.api import (
        ExportResult,
        FindingResult,
        JobStatus,
        JobSummary,
        ReportResult,
        ResumeJobResult,
        SearchFindingResult,
        StartJobResult,
        StopJobResult,
        export_job,
        get_findings,
        get_job_status,
        get_report,
        list_jobs,
        resume_job,
        search_findings,
        start_job,
        stop_job,
    )
    from research_agent.errors import (
        BudgetExceeded,
        InvalidGoal,
        JobAlreadyRunning,
        JobNotFound,
        ResearchAgentError,
    )

__version__ = "0.1.0"

_API_EXPORTS = {
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
}

_ERROR_EXPORTS = {
    "BudgetExceeded",
    "InvalidGoal",
    "JobAlreadyRunning",
    "JobNotFound",
    "ResearchAgentError",
}


def __getattr__(name: str) -> Any:
    if name in _API_EXPORTS:
        value = getattr(import_module("research_agent.api"), name)
    elif name in _ERROR_EXPORTS:
        value = getattr(import_module("research_agent.errors"), name)
    else:
        raise AttributeError(f"module 'research_agent' has no attribute {name!r}")
    globals()[name] = value
    return value


__all__ = [
    "BudgetExceeded",
    "ExportResult",
    "FindingResult",
    "InvalidGoal",
    "JobAlreadyRunning",
    "JobNotFound",
    "JobStatus",
    "JobSummary",
    "ReportResult",
    "ResearchAgentError",
    "ResumeJobResult",
    "SearchFindingResult",
    "StartJobResult",
    "StopJobResult",
    "__version__",
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
