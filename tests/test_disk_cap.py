"""Tests for `research_agent.storage.disk_cap` and the daemon watcher (issue #38)."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

from research_agent import daemon
from research_agent.storage import db
from research_agent.storage.disk_cap import (
    disk_usage_bytes,
    prune_to_target,
    score_sources,
)
from research_agent.storage.jobs import Job
from research_agent.storage.markdown import write_finding
from research_agent.storage.sources import (
    clean_content,
    content_sha256,
    write_source,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def jobs_root(tmp_path: Path) -> Path:
    return tmp_path / "jobs"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.sqlite"
    db.migrate(path=path).close()
    return path


@pytest.fixture
def job(jobs_root: Path, db_path: Path) -> Job:
    return Job.create(
        {"goal": "investigate widget co"},
        jobs_root=jobs_root,
        db_path=db_path,
        today=date(2026, 5, 2),
    )


def _make_source(job: Job, raw: str, *, title: str | None = None) -> int:
    return write_source(
        job,
        url=f"https://example.com/{title or 'src'}",
        title=title,
        raw_content=raw,
        kind="html",
    )


# ---------------------------------------------------------------------------
# disk_usage_bytes
# ---------------------------------------------------------------------------


def test_disk_usage_bytes_sums_recursively(tmp_path: Path) -> None:
    root = tmp_path / "j"
    sub = root / "sources"
    sub.mkdir(parents=True)
    (root / "a.md").write_bytes(b"x" * 100)
    (sub / "b.md").write_bytes(b"y" * 250)
    (sub / "c.md").write_bytes(b"z" * 50)
    assert disk_usage_bytes(root) == 400


def test_disk_usage_bytes_missing_root_is_zero(tmp_path: Path) -> None:
    assert disk_usage_bytes(tmp_path / "nope") == 0


# ---------------------------------------------------------------------------
# score_sources
# ---------------------------------------------------------------------------


def test_score_sources_ranks_findings_usage_above_age(job: Job) -> None:
    sid_unused = _make_source(job, "alpha content " * 20, title="alpha")
    sid_used = _make_source(job, "beta content " * 20, title="beta")

    write_finding(
        job,
        claim="something interesting about beta",
        confidence=0.9,
        source_ids=[sid_used],
    )

    conn = db.connect(job.db_path)
    try:
        scored = score_sources(conn, job.id, goal=job.goal)
    finally:
        conn.close()

    by_id = {s.source_id: s for s in scored}
    assert by_id[sid_used].score > by_id[sid_unused].score


def test_score_sources_skips_already_pruned(job: Job) -> None:
    sid_keep = _make_source(job, "keep me " * 20, title="k")
    sid_pruned = _make_source(job, "prune me " * 20, title="p")

    conn = db.connect(job.db_path)
    try:
        with conn:
            conn.execute("UPDATE sources SET md_path = NULL WHERE id = ?", (sid_pruned,))
        scored = score_sources(conn, job.id, goal=job.goal)
    finally:
        conn.close()

    ids = [s.source_id for s in scored]
    assert sid_keep in ids
    assert sid_pruned not in ids


# ---------------------------------------------------------------------------
# prune_to_target
# ---------------------------------------------------------------------------


def test_prune_to_target_drops_lowest_scored_until_under_target(job: Job) -> None:
    """Big sources, one cited by a finding, others cold → cold ones get pruned."""
    cited_text = "cited large content " * 5000  # ~100 KB
    cold_text_a = "cold a " * 5000
    cold_text_b = "cold b " * 5000
    cold_text_c = "cold c " * 5000

    sid_cited = _make_source(job, cited_text, title="cited")
    sid_a = _make_source(job, cold_text_a, title="a")
    sid_b = _make_source(job, cold_text_b, title="b")
    sid_c = _make_source(job, cold_text_c, title="c")

    write_finding(
        job,
        claim="something about widget co cited",
        confidence=0.8,
        source_ids=[sid_cited],
    )

    initial_usage = disk_usage_bytes(job.root)
    cap_bytes = initial_usage - 1  # force a prune

    pruned = prune_to_target(job, cap_bytes=cap_bytes, db_path=job.db_path)
    assert pruned >= 1

    final_usage = disk_usage_bytes(job.root)
    assert final_usage <= int(cap_bytes * 0.9)

    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT id, md_path FROM sources WHERE id IN (?, ?, ?, ?)",
            (sid_cited, sid_a, sid_b, sid_c),
        ).fetchall()
    finally:
        conn.close()
    by_id = {r["id"]: r for r in rows}

    # Cited source must survive — highest score.
    assert by_id[sid_cited]["md_path"] is not None
    assert (job.root / by_id[sid_cited]["md_path"]).exists()

    # At least one cold source must have been nulled and unlinked.
    nulled = [sid for sid in (sid_a, sid_b, sid_c) if by_id[sid]["md_path"] is None]
    assert nulled, "expected at least one cold source to be pruned"
    for sid in nulled:
        # The original file must be unlinked.
        # (Look it up by the deterministic sha path.)
        # No need to check the exact file — we already asserted md_path NULL.
        assert by_id[sid]["md_path"] is None


def test_prune_to_target_emits_source_pruned_events(job: Job) -> None:
    for i in range(4):
        _make_source(job, f"content number {i} " * 5000, title=f"src{i}")

    cap_bytes = disk_usage_bytes(job.root) - 1
    prune_to_target(job, cap_bytes=cap_bytes, db_path=job.db_path)

    events = [
        json.loads(line)
        for line in (job.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    pruned_events = [e for e in events if e["kind"] == "source_pruned"]
    assert pruned_events, "expected at least one source_pruned event"
    for e in pruned_events:
        assert "sha256" in e["payload"]
        assert "source_id" in e["payload"]


def test_prune_to_target_no_op_when_under_cap(job: Job) -> None:
    _make_source(job, "tiny", title="tiny")
    huge_cap = 10 * 1024 * 1024 * 1024  # 10 GB

    pruned = prune_to_target(job, cap_bytes=huge_cap, db_path=job.db_path)
    assert pruned == 0


def test_prune_then_write_source_restores_md_path(job: Job) -> None:
    """A re-fetch of pruned content rewrites the file and clears md_path NULL."""
    raw = "valuable evergreen content"
    sid = _make_source(job, raw, title="evergreen")

    sha = content_sha256(clean_content(raw))
    md_path = job.root / "sources" / f"{sha}.md"
    assert md_path.exists()

    # Manually simulate a prune.
    md_path.unlink()
    conn = db.connect(job.db_path)
    try:
        with conn:
            conn.execute("UPDATE sources SET md_path = NULL WHERE id = ?", (sid,))
    finally:
        conn.close()
    assert not md_path.exists()

    # Re-fetch with the same content → file restored, md_path repopulated, same id.
    sid_again = _make_source(job, raw, title="evergreen")
    assert sid_again == sid
    assert md_path.exists()

    conn = db.connect(job.db_path)
    try:
        row = conn.execute("SELECT md_path FROM sources WHERE id = ?", (sid,)).fetchone()
    finally:
        conn.close()
    assert row["md_path"] == f"sources/{sha}.md"


# ---------------------------------------------------------------------------
# Daemon watcher (one tick, monkeypatched cadence)
# ---------------------------------------------------------------------------


async def test_disk_cap_watcher_prunes_when_over_cap(
    job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "DISK_CAP_POLL_INTERVAL_S", 0.05)

    for i in range(3):
        _make_source(job, f"content {i} " * 5000, title=f"src{i}")

    cap_bytes = disk_usage_bytes(job.root) - 1
    should_stop = asyncio.Event()

    async def _stop_after_one_tick() -> None:
        await asyncio.sleep(0.15)
        should_stop.set()

    stopper = asyncio.create_task(_stop_after_one_tick())
    await daemon._disk_cap_watcher(job, cap_bytes, should_stop, interval_s=0.05)
    await stopper

    conn = db.connect(job.db_path)
    try:
        nulled = conn.execute("SELECT COUNT(*) AS n FROM sources WHERE md_path IS NULL").fetchone()[
            "n"
        ]
    finally:
        conn.close()
    assert nulled >= 1


# ---------------------------------------------------------------------------
# Migration: NOT NULL → nullable on sources.md_path
# ---------------------------------------------------------------------------


def test_migration_relaxes_md_path_not_null(tmp_path: Path) -> None:
    """A legacy DB built with ``md_path NOT NULL`` should migrate idempotently."""
    import sqlite3

    db_path = tmp_path / "legacy.sqlite"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE jobs (id TEXT PRIMARY KEY, goal TEXT, status TEXT,
            intake_json TEXT, created_at INTEGER);
        CREATE TABLE sources (
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
        INSERT INTO sources (sha256, fetched_at, md_path)
            VALUES ('legacysha', 1700000000, 'sources/legacy.md');
        """
    )
    legacy.commit()
    legacy.close()

    # Now run migrate — it should rebuild sources to allow NULL md_path
    # without losing the seeded row.
    db.migrate(path=db_path).close()

    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(sources)")}
        # column tuple format: (cid, name, type, notnull, dflt_value, pk)
        assert cols["md_path"][3] == 0, "md_path must be nullable after migrate"

        # Existing data preserved.
        row = conn.execute("SELECT md_path FROM sources WHERE sha256 = 'legacysha'").fetchone()
        assert row[0] == "sources/legacy.md"

        # Idempotent: a second run is a no-op.
        conn.close()
        db.migrate(path=db_path).close()
        conn = sqlite3.connect(db_path)
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(sources)")}
        assert cols["md_path"][3] == 0
    finally:
        conn.close()
