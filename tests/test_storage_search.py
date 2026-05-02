"""Tests for `research_agent.storage.search` (issue #22)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from research_agent.storage import db
from research_agent.storage.search import search_fts

JOB_A = "2026-05-01-job-a"
JOB_B = "2026-05-02-job-b"


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """Build a tmp DB with two jobs, several findings, and two sources."""
    db_path = tmp_path / "data" / "index.sqlite"
    db.migrate(path=db_path).close()

    now = int(time.time())
    conn = db.connect(db_path)
    try:
        with conn:
            for jid in (JOB_A, JOB_B):
                conn.execute(
                    "INSERT INTO jobs (id, goal, status, intake_json, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (jid, "g", "pending", "{}", now),
                )

            conn.execute(
                "INSERT INTO findings"
                " (job_id, md_path, claim, confidence, source_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    JOB_A,
                    "findings/000001.md",
                    "quantum entanglement breakthrough at MIT lab",
                    0.9,
                    "[]",
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO findings"
                " (job_id, md_path, claim, confidence, source_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    JOB_B,
                    "findings/000001.md",
                    "quantum supremacy benchmark surpassed",
                    0.8,
                    "[]",
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO findings"
                " (job_id, md_path, claim, confidence, source_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    JOB_A,
                    "findings/000002.md",
                    "unrelated coffee research finding",
                    0.5,
                    "[]",
                    now,
                ),
            )

            cur1 = conn.execute(
                "INSERT INTO sources"
                " (sha256, url, title, fetched_at, md_path, kind)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "a" * 64,
                    "https://example.com/q",
                    "Quantum computing report",
                    now,
                    "sources/a.md",
                    "web",
                ),
            )
            sid1 = cur1.lastrowid
            cur2 = conn.execute(
                "INSERT INTO sources"
                " (sha256, url, title, fetched_at, md_path, kind)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "b" * 64,
                    "https://example.com/c",
                    "Coffee brewing techniques",
                    now,
                    "sources/b.md",
                    "web",
                ),
            )
            sid2 = cur2.lastrowid

            # sid1 shared by both jobs; sid2 only by JOB_A.
            conn.execute(
                "INSERT INTO job_sources (job_id, source_id) VALUES (?, ?)",
                (JOB_A, sid1),
            )
            conn.execute(
                "INSERT INTO job_sources (job_id, source_id) VALUES (?, ?)",
                (JOB_B, sid1),
            )
            conn.execute(
                "INSERT INTO job_sources (job_id, source_id) VALUES (?, ?)",
                (JOB_A, sid2),
            )

            conn.execute("INSERT INTO findings_fts(findings_fts) VALUES('rebuild')")
            conn.execute("INSERT INTO sources_fts(sources_fts) VALUES('rebuild')")
    finally:
        conn.close()

    return db_path


def test_empty_result(seeded_db: Path) -> None:
    assert search_fts("nonexistenttoken", job_id=None, kind="both", db_path=seeded_db) == []


def test_multi_job_hit(seeded_db: Path) -> None:
    results = search_fts("quantum", job_id=None, kind="findings", db_path=seeded_db)
    job_ids = {r["job_id"] for r in results}
    assert JOB_A in job_ids
    assert JOB_B in job_ids


def test_kind_findings_excludes_sources(seeded_db: Path) -> None:
    results = search_fts("quantum", job_id=None, kind="findings", db_path=seeded_db)
    assert results
    assert all(r["kind"] == "finding" for r in results)


def test_kind_sources_excludes_findings(seeded_db: Path) -> None:
    results = search_fts("quantum", job_id=None, kind="sources", db_path=seeded_db)
    assert results
    assert all(r["kind"] == "source" for r in results)


def test_kind_both_returns_mixed(seeded_db: Path) -> None:
    results = search_fts("quantum", job_id=None, kind="both", db_path=seeded_db)
    kinds = {r["kind"] for r in results}
    assert kinds == {"finding", "source"}


def test_job_filter_findings(seeded_db: Path) -> None:
    results = search_fts("quantum", job_id=JOB_A, kind="findings", db_path=seeded_db)
    assert results
    assert all(r["job_id"] == JOB_A for r in results)


def test_job_filter_sources_uses_job_sources(seeded_db: Path) -> None:
    # The "Coffee brewing" source is only linked to JOB_A.
    a_only = search_fts("coffee", job_id=JOB_A, kind="sources", db_path=seeded_db)
    assert a_only
    assert all(r["job_id"] == JOB_A for r in a_only)

    b_only = search_fts("coffee", job_id=JOB_B, kind="sources", db_path=seeded_db)
    assert b_only == []


def test_snippet_highlighting(seeded_db: Path) -> None:
    results = search_fts("quantum", job_id=None, kind="findings", db_path=seeded_db)
    assert results
    snippet = results[0]["snippet"]
    assert "[" in snippet and "]" in snippet


def test_results_sorted_by_score_ascending(seeded_db: Path) -> None:
    results = search_fts("quantum", job_id=None, kind="both", db_path=seeded_db)
    scores = [r["score"] for r in results]
    assert scores == sorted(scores)


def test_invalid_kind_raises(seeded_db: Path) -> None:
    with pytest.raises(ValueError):
        search_fts("quantum", job_id=None, kind="bogus", db_path=seeded_db)


def test_empty_query_raises(seeded_db: Path) -> None:
    with pytest.raises(ValueError):
        search_fts("   ", job_id=None, kind="both", db_path=seeded_db)


def test_result_dict_has_expected_keys(seeded_db: Path) -> None:
    results = search_fts("quantum", job_id=None, kind="both", db_path=seeded_db)
    assert results
    expected = {"kind", "score", "job_id", "snippet", "id", "md_path", "title_or_claim"}
    for row in results:
        assert expected <= row.keys()
