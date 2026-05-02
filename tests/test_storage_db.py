"""Tests for `research_agent.storage.db` schema and connection helpers."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from research_agent.storage import db

EXPECTED_TABLES = {
    "jobs",
    "plans",
    "tasks",
    "findings",
    "sources",
    "job_sources",
    "syntheses",
    "checkpoints",
    "events",
    "llm_calls",
    "findings_fts",
    "sources_fts",
}

EXPECTED_INDEXES = {
    "idx_tasks_status_job",
    "idx_findings_job",
    "idx_checkpoints_job_ts",
    "idx_events_job_ts",
}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "index.sqlite"


def _scalar(conn: sqlite3.Connection, sql: str) -> object:
    cur = conn.execute(sql)
    row = cur.fetchone()
    assert row is not None, f"expected a row from: {sql}"
    return row[0]


def test_migrate_creates_wal_db(db_path: Path) -> None:
    conn = db.migrate(path=db_path)
    try:
        assert db_path.exists()
        assert _scalar(conn, "PRAGMA journal_mode") == "wal"
        assert _scalar(conn, "PRAGMA foreign_keys") == 1
        # synchronous=NORMAL maps to integer 1
        assert _scalar(conn, "PRAGMA synchronous") == 1
    finally:
        conn.close()


def test_migrate_idempotent(db_path: Path) -> None:
    conn = db.migrate(path=db_path)
    try:
        before = _scalar(
            conn,
            "SELECT count(*) FROM sqlite_master WHERE type IN ('table','index')",
        )
    finally:
        conn.close()

    conn2 = db.migrate(path=db_path)
    try:
        after = _scalar(
            conn2,
            "SELECT count(*) FROM sqlite_master WHERE type IN ('table','index')",
        )
    finally:
        conn2.close()

    assert before == after


def test_all_tables_present(db_path: Path) -> None:
    conn = db.migrate(path=db_path)
    try:
        table_names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        index_names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    finally:
        conn.close()

    missing_tables = EXPECTED_TABLES - table_names
    assert not missing_tables, f"missing tables: {missing_tables}"

    missing_indexes = EXPECTED_INDEXES - index_names
    assert not missing_indexes, f"missing indexes: {missing_indexes}"


def test_insert_and_query_each_table(db_path: Path) -> None:
    conn = db.migrate(path=db_path)
    try:
        now = int(time.time())
        job_id = "2026-05-02-test-job"

        # jobs (parent of nearly everything)
        conn.execute(
            """
            INSERT INTO jobs (id, goal, status, intake_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, "investigate widget co", "pending", "{}", now),
        )

        # plans
        conn.execute(
            """
            INSERT INTO plans (job_id, version, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (job_id, 1, "{}", now),
        )

        # tasks
        conn.execute(
            """
            INSERT INTO tasks (job_id, plan_version, kind, payload_json, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, 1, "web_search", "{}", "pending"),
        )

        # findings
        conn.execute(
            """
            INSERT INTO findings
                (job_id, md_path, claim, confidence, source_ids, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_id, "findings/0001.md", "the sky is blue", 0.9, "[]", now),
        )

        # sources (must precede job_sources)
        cur = conn.execute(
            """
            INSERT INTO sources (sha256, url, title, fetched_at, md_path, kind)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("a" * 64, "https://example.com", "Example", now, "sources/0001.md", "web"),
        )
        source_id = cur.lastrowid

        # job_sources
        conn.execute(
            "INSERT INTO job_sources (job_id, source_id) VALUES (?, ?)",
            (job_id, source_id),
        )

        # syntheses
        conn.execute(
            """
            INSERT INTO syntheses (job_id, version, md_path, model, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, 1, "synthesis-v1.md", "gpt-test", now),
        )

        # checkpoints
        conn.execute(
            """
            INSERT INTO checkpoints (job_id, kind, payload_json, ts)
            VALUES (?, ?, ?, ?)
            """,
            (job_id, "job_started", "{}", now),
        )

        # events
        conn.execute(
            """
            INSERT INTO events (job_id, ts, level, kind)
            VALUES (?, ?, ?, ?)
            """,
            (job_id, now, "INFO", "test"),
        )

        # llm_calls
        conn.execute(
            """
            INSERT INTO llm_calls (job_id, ts, tier, provider, model)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, now, "cloud", "openrouter", "anthropic/claude-test"),
        )

        conn.commit()

        # Round-trip every table.
        assert _scalar(conn, "SELECT count(*) FROM jobs") == 1
        assert _scalar(conn, "SELECT count(*) FROM plans") == 1
        assert _scalar(conn, "SELECT count(*) FROM tasks") == 1
        assert _scalar(conn, "SELECT count(*) FROM findings") == 1
        assert _scalar(conn, "SELECT count(*) FROM sources") == 1
        assert _scalar(conn, "SELECT count(*) FROM job_sources") == 1
        assert _scalar(conn, "SELECT count(*) FROM syntheses") == 1
        assert _scalar(conn, "SELECT count(*) FROM checkpoints") == 1
        assert _scalar(conn, "SELECT count(*) FROM events") == 1
        assert _scalar(conn, "SELECT count(*) FROM llm_calls") == 1

        row = conn.execute("SELECT goal, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["goal"] == "investigate widget co"
        assert row["status"] == "pending"
    finally:
        conn.close()


def test_foreign_keys_enforced(db_path: Path) -> None:
    conn = db.migrate(path=db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO plans (job_id, version, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                ("does-not-exist", 1, "{}", 0),
            )
    finally:
        conn.close()


def test_findings_fts_roundtrip(db_path: Path) -> None:
    conn = db.migrate(path=db_path)
    try:
        now = int(time.time())
        job_id = "fts-job"
        conn.execute(
            "INSERT INTO jobs (id, goal, status, intake_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, "g", "pending", "{}", now),
        )
        cur = conn.execute(
            """
            INSERT INTO findings
                (job_id, md_path, claim, confidence, source_ids, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                "findings/q.md",
                "quantum supremacy benchmark",
                0.5,
                "[]",
                now,
            ),
        )
        finding_id = cur.lastrowid

        # External-content FTS5: rebuild syncs the index from the content table.
        conn.execute("INSERT INTO findings_fts(findings_fts) VALUES('rebuild')")
        conn.commit()

        rows = conn.execute(
            "SELECT rowid FROM findings_fts WHERE findings_fts MATCH 'quantum'"
        ).fetchall()
        assert [r[0] for r in rows] == [finding_id]
    finally:
        conn.close()


def test_sources_fts_roundtrip(db_path: Path) -> None:
    conn = db.migrate(path=db_path)
    try:
        now = int(time.time())
        cur = conn.execute(
            """
            INSERT INTO sources (sha256, url, title, fetched_at, md_path, kind)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("b" * 64, "https://ex.com", "Annual Report 2025", now, "s.md", "pdf"),
        )
        source_id = cur.lastrowid

        conn.execute("INSERT INTO sources_fts(sources_fts) VALUES('rebuild')")
        conn.commit()

        rows = conn.execute(
            "SELECT rowid FROM sources_fts WHERE sources_fts MATCH 'annual'"
        ).fetchall()
        assert [r[0] for r in rows] == [source_id]
    finally:
        conn.close()


def test_checkpoint_helper_uses_synchronous_full(db_path: Path) -> None:
    db.migrate(path=db_path).close()
    conn = db.connect_for_checkpoints(path=db_path)
    try:
        # synchronous=FULL maps to integer 2
        assert _scalar(conn, "PRAGMA synchronous") == 2
        # WAL + foreign_keys must still hold for this connection.
        assert _scalar(conn, "PRAGMA journal_mode") == "wal"
        assert _scalar(conn, "PRAGMA foreign_keys") == 1
    finally:
        conn.close()


def test_connect_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "dir" / "index.sqlite"
    conn = db.connect(path=nested)
    try:
        assert nested.parent.is_dir()
        assert nested.exists()
    finally:
        conn.close()


def test_connect_rejects_invalid_synchronous(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        db.connect(path=tmp_path / "x.sqlite", synchronous="LOOSE")
