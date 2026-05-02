"""Storage layer — job folders, SQLite index, markdown/JSON sidecars, source dedup."""

from research_agent.storage.db import (
    DEFAULT_DB_PATH,
    SCHEMA_SQL,
    connect,
    connect_for_checkpoints,
    migrate,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "SCHEMA_SQL",
    "connect",
    "connect_for_checkpoints",
    "migrate",
]
