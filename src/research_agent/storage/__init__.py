"""Storage layer — job folders, SQLite index, markdown/JSON sidecars, source dedup."""

from research_agent.storage.db import (
    DEFAULT_DB_PATH,
    SCHEMA_SQL,
    connect,
    connect_for_checkpoints,
    migrate,
)
from research_agent.storage.jobs import (
    DEFAULT_JOBS_ROOT,
    Job,
    list_jobs,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_JOBS_ROOT",
    "Job",
    "SCHEMA_SQL",
    "connect",
    "connect_for_checkpoints",
    "list_jobs",
    "migrate",
]
