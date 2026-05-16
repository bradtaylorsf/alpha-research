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
    cost_so_far_usd REAL DEFAULT 0,
    completion_reason TEXT
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
    target_fragments TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_job ON findings(job_id);

-- Sources (deduplicated across jobs)
-- ``md_path`` is nullable: the disk-cap watcher (issue #38) prunes the
-- on-disk markdown for the lowest-relevance sources and clears this column
-- to NULL while leaving the row in place for cross-job audit. A future
-- fetch with the same sha256 rewrites the file and restores the path.
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    url TEXT,
    title TEXT,
    fetched_at INTEGER NOT NULL,
    archive_url TEXT,
    md_path TEXT,
    kind TEXT,
    embedding BLOB,
    parent_source_id INTEGER REFERENCES sources(id)
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

-- Section-level synthesis fragments
CREATE TABLE IF NOT EXISTS fragments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    section_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    md_path TEXT NOT NULL,
    json_path TEXT NOT NULL,
    synthesis_version INTEGER,
    source_finding_ids TEXT NOT NULL,
    cited_source_ids TEXT,
    model TEXT,
    tier TEXT,
    confidence REAL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(job_id, section_id, version)
);
CREATE INDEX IF NOT EXISTS idx_fragments_job_section
    ON fragments(job_id, section_id);

-- Section-level critique passes
CREATE TABLE IF NOT EXISTS fragment_critiques (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    section_id TEXT NOT NULL,
    fragment_version INTEGER NOT NULL,
    version INTEGER NOT NULL,
    md_path TEXT NOT NULL,
    json_path TEXT NOT NULL,
    model TEXT NOT NULL,
    cost_usd REAL,
    status TEXT NOT NULL,
    confidence REAL,
    should_replan INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(job_id, section_id, version)
);
CREATE INDEX IF NOT EXISTS idx_fragment_critiques_job_section
    ON fragment_critiques(job_id, section_id);

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

-- Per-job working hypotheses
CREATE TABLE IF NOT EXISTS hypotheses (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    plan_version INTEGER NOT NULL,
    statement TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    supports TEXT NOT NULL,
    refutes TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'confirmed', 'refuted', 'inconclusive')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hypotheses_job_status ON hypotheses(job_id, status);

-- Per-job coverage ledger for complete-list / enumeration work.
CREATE TABLE IF NOT EXISTS coverage_units (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    dim_key TEXT NOT NULL,
    dims_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'pending',
            'in_progress',
            'complete',
            'not_yet_public',
            'confirmed_gap',
            'failed'
        )
    ),
    recent_attempts_json TEXT NOT NULL DEFAULT '[]',
    last_attempt_json TEXT,
    unblocker TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (job_id, dim_key)
);
CREATE INDEX IF NOT EXISTS idx_coverage_units_job_status
    ON coverage_units(job_id, status);

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


def _migrate_sources_md_path_nullable(conn: sqlite3.Connection) -> None:
    """Drop the legacy ``NOT NULL`` constraint on ``sources.md_path``.

    SQLite can't ``ALTER COLUMN`` to remove a NOT NULL, so we detect the old
    schema via ``PRAGMA table_info(sources)`` and, when found, rebuild the
    table: create ``sources_new`` with the relaxed column, copy rows, drop
    the old table, rename. Idempotent — a no-op once the column is already
    nullable. Runs inside the caller's transaction.
    """
    cols = conn.execute("PRAGMA table_info(sources)").fetchall()
    if not cols:
        return  # table doesn't exist yet; SCHEMA_SQL will create it nullable
    md_path_col = next((c for c in cols if c["name"] == "md_path"), None)
    if md_path_col is None or md_path_col["notnull"] == 0:
        return  # already nullable

    # FTS5 external-content tables reference ``sources`` by rowid; drop the
    # virtual table first, rebuild ``sources``, then re-create + repopulate
    # the FTS index from the new content table.
    conn.executescript(
        """
        DROP TABLE IF EXISTS sources_fts;
        CREATE TABLE sources_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sha256 TEXT NOT NULL UNIQUE,
            url TEXT,
            title TEXT,
            fetched_at INTEGER NOT NULL,
            archive_url TEXT,
            md_path TEXT,
            kind TEXT,
            embedding BLOB
        );
        INSERT INTO sources_new
            (id, sha256, url, title, fetched_at, archive_url, md_path, kind, embedding)
            SELECT id, sha256, url, title, fetched_at, archive_url, md_path, kind, embedding
            FROM sources;
        DROP TABLE sources;
        ALTER TABLE sources_new RENAME TO sources;
        CREATE VIRTUAL TABLE sources_fts USING fts5(
            title, content=sources, content_rowid=id
        );
        INSERT INTO sources_fts(sources_fts) VALUES('rebuild');
        """
    )


def _migrate_sources_parent_source_id(conn: sqlite3.Connection) -> None:
    """Add ``sources.parent_source_id`` to legacy DBs (issue #206).

    The cornerstone vector index writes one ``cornerstone_chunk`` row per
    chunk and links it back to the parent cornerstone source via this
    column so retrieval can filter by the parent. ``ALTER TABLE`` is the
    safe path here because the column is nullable. Idempotent — checked
    via ``PRAGMA table_info`` before adding.
    """
    cols = conn.execute("PRAGMA table_info(sources)").fetchall()
    if not cols:
        return
    if any(c["name"] == "parent_source_id" for c in cols):
        return
    conn.execute(
        "ALTER TABLE sources ADD COLUMN parent_source_id INTEGER REFERENCES sources(id)"
    )


def _migrate_jobs_completion_reason(conn: sqlite3.Connection) -> None:
    """Add ``jobs.completion_reason`` to legacy DBs that predate issue #39.

    ``ALTER TABLE jobs ADD COLUMN`` is the only safe way to add a nullable
    column without rebuilding the table; this helper checks
    ``PRAGMA table_info(jobs)`` first so it is idempotent. Runs inside the
    caller's transaction.
    """
    cols = conn.execute("PRAGMA table_info(jobs)").fetchall()
    if not cols:
        return  # table doesn't exist yet; SCHEMA_SQL will create it with the column
    if any(c["name"] == "completion_reason" for c in cols):
        return  # already migrated
    conn.execute("ALTER TABLE jobs ADD COLUMN completion_reason TEXT")


def _migrate_findings_target_fragments(conn: sqlite3.Connection) -> None:
    """Add nullable ``findings.target_fragments`` to legacy DBs (issue #325)."""
    cols = conn.execute("PRAGMA table_info(findings)").fetchall()
    if not cols:
        return
    if any(c["name"] == "target_fragments" for c in cols):
        return
    conn.execute("ALTER TABLE findings ADD COLUMN target_fragments TEXT")


def _migrate_hypotheses_table(conn: sqlite3.Connection) -> None:
    """Create the hypotheses ledger for DBs that predate issue #261."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS hypotheses (
            id INTEGER PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id),
            plan_version INTEGER NOT NULL,
            statement TEXT NOT NULL,
            confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
            supports TEXT NOT NULL,
            refutes TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('open', 'confirmed', 'refuted', 'inconclusive')
            ),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_hypotheses_job_status
            ON hypotheses(job_id, status);
        """
    )


def _migrate_coverage_units_table(conn: sqlite3.Connection) -> None:
    """Create the enumeration coverage ledger for DBs that predate issue #305."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS coverage_units (
            job_id TEXT NOT NULL REFERENCES jobs(id),
            dim_key TEXT NOT NULL,
            dims_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'pending',
                    'in_progress',
                    'complete',
                    'not_yet_public',
                    'confirmed_gap',
                    'failed'
                )
            ),
            recent_attempts_json TEXT NOT NULL DEFAULT '[]',
            last_attempt_json TEXT,
            unblocker TEXT,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (job_id, dim_key)
        );
        CREATE INDEX IF NOT EXISTS idx_coverage_units_job_status
            ON coverage_units(job_id, status);
        """
    )


def _migrate_fragments_table(conn: sqlite3.Connection) -> None:
    """Create the section-fragment ledger for DBs that predate issue #324."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS fragments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id),
            section_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            md_path TEXT NOT NULL,
            json_path TEXT NOT NULL,
            synthesis_version INTEGER,
            source_finding_ids TEXT NOT NULL,
            cited_source_ids TEXT,
            model TEXT,
            tier TEXT,
            confidence REAL,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(job_id, section_id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_fragments_job_section
            ON fragments(job_id, section_id);
        """
    )


def _migrate_fragment_critiques_table(conn: sqlite3.Connection) -> None:
    """Create the section-fragment critique ledger for DBs before issue #328."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS fragment_critiques (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id),
            section_id TEXT NOT NULL,
            fragment_version INTEGER NOT NULL,
            version INTEGER NOT NULL,
            md_path TEXT NOT NULL,
            json_path TEXT NOT NULL,
            model TEXT NOT NULL,
            cost_usd REAL,
            status TEXT NOT NULL,
            confidence REAL,
            should_replan INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(job_id, section_id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_fragment_critiques_job_section
            ON fragment_critiques(job_id, section_id);
        """
    )


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
        _migrate_sources_md_path_nullable(conn)
        _migrate_sources_parent_source_id(conn)
        _migrate_jobs_completion_reason(conn)
        _migrate_findings_target_fragments(conn)
        _migrate_hypotheses_table(conn)
        _migrate_coverage_units_table(conn)
        _migrate_fragments_table(conn)
        _migrate_fragment_critiques_table(conn)
    return conn
