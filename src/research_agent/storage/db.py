"""SQLite cross-job index.

Implements the schema locked in by §10 of ``research-agent-implementation-guide.md``:
the 10 content tables (``jobs``, ``plans``, ``tasks``, ``findings``, ``sources``,
``job_sources``, ``syntheses``, ``checkpoints``, ``events``, ``llm_calls``) plus
two FTS5 virtual tables (``findings_fts``, ``sources_fts``) wired to their content
tables via ``content=``/``content_rowid=``.

The DB lives at ``data/index.sqlite`` and is opened in WAL mode with foreign-key
enforcement. ``synchronous`` defaults to ``NORMAL``; checkpoint writers may opt
into ``FULL`` via :func:`connect_for_checkpoints`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path("data/index.sqlite")

SCHEMA_SQL = """
-- Jobs
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    domain TEXT,
    status TEXT NOT NULL,
    intake_json TEXT NOT NULL,
    time_cap_hours INTEGER,
    budget_cap_usd REAL,
    aggressiveness TEXT,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    last_activity_at INTEGER,
    pid INTEGER,
    cost_so_far_usd REAL DEFAULT 0
);

-- Plan versions
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(job_id, version)
);

-- Tasks (the queue)
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    plan_version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    parent_task_id INTEGER REFERENCES tasks(id),
    depth INTEGER DEFAULT 0,
    started_at INTEGER,
    finished_at INTEGER,
    result_json TEXT,
    error TEXT,
    retry_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_job ON tasks(job_id, status);

-- Findings
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    md_path TEXT NOT NULL,
    claim TEXT NOT NULL,
    confidence REAL NOT NULL,
    source_ids TEXT NOT NULL,
    contradicts TEXT,
    embedding BLOB,
    tags TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_job ON findings(job_id);

-- Sources (deduplicated across jobs)
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    url TEXT,
    title TEXT,
    fetched_at INTEGER NOT NULL,
    archive_url TEXT,
    md_path TEXT NOT NULL,
    kind TEXT,
    embedding BLOB
);

-- Job ↔ source many-to-many
CREATE TABLE IF NOT EXISTS job_sources (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    source_id INTEGER NOT NULL REFERENCES sources(id),
    PRIMARY KEY (job_id, source_id)
);

-- Synthesis passes
CREATE TABLE IF NOT EXISTS syntheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    version INTEGER NOT NULL,
    md_path TEXT NOT NULL,
    model TEXT NOT NULL,
    cost_usd REAL,
    created_at INTEGER NOT NULL,
    UNIQUE(job_id, version)
);

-- Critique passes (paired with syntheses; uses a different model tier)
CREATE TABLE IF NOT EXISTS critiques (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    version INTEGER NOT NULL,
    md_path TEXT NOT NULL,
    model TEXT NOT NULL,
    cost_usd REAL,
    should_replan INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(job_id, version)
);

-- Checkpoints (one per state transition)
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_job_ts ON checkpoints(job_id, ts);

-- Events (mirror of events.jsonl, for SQL queries from the future UI)
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,
    actor TEXT,
    kind TEXT NOT NULL,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_job_ts ON events(job_id, ts);

-- LLM call ledger (cost tracking)
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT REFERENCES jobs(id),
    ts INTEGER NOT NULL,
    tier TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    latency_ms INTEGER,
    cost_usd REAL,
    finish_reason TEXT
);

-- FTS5 over findings.claim and sources.title
CREATE VIRTUAL TABLE IF NOT EXISTS findings_fts USING fts5(
    claim, content=findings, content_rowid=id
);
CREATE VIRTUAL TABLE IF NOT EXISTS sources_fts USING fts5(
    title, content=sources, content_rowid=id
);
"""

_ALLOWED_SYNCHRONOUS = {"OFF", "NORMAL", "FULL", "EXTRA"}


def connect(
    path: Path | str = DEFAULT_DB_PATH,
    *,
    synchronous: str = "NORMAL",
) -> sqlite3.Connection:
    """Open a SQLite connection with the project's standard pragmas.

    - WAL journal mode (concurrent readers + single writer)
    - Foreign keys enforced
    - ``synchronous`` defaults to ``NORMAL``; pass ``'FULL'`` for checkpoint writers
    - ``row_factory`` set to :class:`sqlite3.Row`
    """
    sync = synchronous.upper()
    if sync not in _ALLOWED_SYNCHRONOUS:
        raise ValueError(
            f"synchronous must be one of {sorted(_ALLOWED_SYNCHRONOUS)}; got {synchronous!r}"
        )

    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA synchronous={sync}")
    return conn


def connect_for_checkpoints(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection with ``synchronous=FULL`` for durable checkpoint writes."""
    return connect(path, synchronous="FULL")


def migrate(
    conn: sqlite3.Connection | None = None,
    *,
    path: Path | str = DEFAULT_DB_PATH,
) -> sqlite3.Connection:
    """Apply :data:`SCHEMA_SQL` to ``conn`` (or a new connection at ``path``).

    Idempotent: every DDL statement uses ``IF NOT EXISTS`` so repeated calls are safe.
    Returns the connection so callers can reuse it.
    """
    if conn is None:
        conn = connect(path)
    with conn:
        conn.executescript(SCHEMA_SQL)
    return conn
