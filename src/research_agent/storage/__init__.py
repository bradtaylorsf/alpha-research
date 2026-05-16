"""Storage layer — job folders, SQLite index, markdown/JSON sidecars, source dedup."""

from research_agent.storage.db import (
    DEFAULT_DB_PATH,
    SCHEMA_SQL,
    connect,
    connect_for_checkpoints,
    migrate,
)
from research_agent.storage.export import (
    export_md_bundle,
    export_zip,
)
from research_agent.storage.jobs import (
    DEFAULT_JOBS_ROOT,
    Job,
    list_jobs,
)
from research_agent.storage.markdown import (
    latest_fragment,
    latest_fragments,
    write_finding,
    write_finding_translation,
    write_fragment,
    write_plan,
    write_report,
    write_synthesis,
)
from research_agent.storage.search import search_fts
from research_agent.storage.sources import (
    clean_content,
    content_sha256,
    write_source,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_JOBS_ROOT",
    "Job",
    "SCHEMA_SQL",
    "clean_content",
    "connect",
    "connect_for_checkpoints",
    "content_sha256",
    "export_md_bundle",
    "export_zip",
    "list_jobs",
    "latest_fragment",
    "latest_fragments",
    "migrate",
    "search_fts",
    "write_fragment",
    "write_finding",
    "write_finding_translation",
    "write_plan",
    "write_report",
    "write_source",
    "write_synthesis",
]
