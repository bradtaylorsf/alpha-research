"""Research agent package - autonomous CLI research tool."""

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
