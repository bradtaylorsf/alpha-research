"""Tests for `research_agent.storage.sources`."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from research_agent.storage import db
from research_agent.storage.jobs import Job
from research_agent.storage.sources import clean_content, content_sha256, write_source

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
def job1(jobs_root: Path, db_path: Path) -> Job:
    return Job.create(
        {"goal": "first investigation"},
        jobs_root=jobs_root,
        db_path=db_path,
        today=date(2026, 5, 1),
    )


@pytest.fixture
def job2(jobs_root: Path, db_path: Path) -> Job:
    return Job.create(
        {"goal": "second investigation"},
        jobs_root=jobs_root,
        db_path=db_path,
        today=date(2026, 5, 2),
    )


# ---------------------------------------------------------------------------
# clean_content
# ---------------------------------------------------------------------------


def test_clean_content_strips_and_normalizes_endings() -> None:
    out = clean_content("\r\n  hello world  \r\n")
    assert out == "hello world"


def test_clean_content_collapses_horizontal_whitespace() -> None:
    assert clean_content("foo    \t   bar") == "foo bar"


def test_clean_content_collapses_blank_line_runs() -> None:
    assert clean_content("a\n\n\n\nb") == "a\n\nb"


def test_clean_content_idempotent() -> None:
    once = clean_content("  hello\n\n\nworld  ")
    twice = clean_content(once)
    assert once == twice


def test_clean_content_rejects_non_string() -> None:
    with pytest.raises(ValueError):
        clean_content(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# content_sha256
# ---------------------------------------------------------------------------


def test_content_sha256_matches_hashlib() -> None:
    text = "hello"
    assert content_sha256(text) == hashlib.sha256(b"hello").hexdigest()


def test_content_sha256_is_lowercase_hex() -> None:
    digest = content_sha256("anything")
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(c in "0123456789abcdef" for c in digest)


# ---------------------------------------------------------------------------
# write_source
# ---------------------------------------------------------------------------


def test_write_source_creates_md_json_and_db_row(job1: Job) -> None:
    sid = write_source(
        job1,
        url="https://example.com/page",
        title="Example",
        raw_content="Hello world.\n",
        kind="html",
        archive_url="https://web.archive.org/example",
        fetched_at=1700000000,
    )
    assert sid >= 1

    cleaned = clean_content("Hello world.\n")
    sha = content_sha256(cleaned)

    md_path = job1.root / "sources" / f"{sha}.md"
    json_path = job1.root / "sources" / f"{sha}.json"
    assert md_path.exists()
    assert json_path.exists()
    assert md_path.read_text().rstrip("\n") == cleaned

    sidecar = json.loads(json_path.read_text())
    assert sidecar["sha256"] == sha
    assert sidecar["url"] == "https://example.com/page"
    assert sidecar["title"] == "Example"
    assert sidecar["fetched_at"] == 1700000000
    assert sidecar["archive_url"] == "https://web.archive.org/example"
    assert sidecar["kind"] == "html"
    assert sidecar["md_path"] == f"sources/{sha}.md"

    conn = db.connect(job1.db_path)
    try:
        srow = conn.execute(
            "SELECT id, sha256, url, title, fetched_at, archive_url, md_path, kind"
            " FROM sources WHERE id = ?",
            (sid,),
        ).fetchone()
        jrow = conn.execute(
            "SELECT job_id, source_id FROM job_sources WHERE source_id = ?", (sid,)
        ).fetchall()
    finally:
        conn.close()

    assert srow["sha256"] == sha
    assert srow["md_path"] == f"sources/{sha}.md"
    assert len(jrow) == 1
    assert jrow[0]["job_id"] == job1.id


def test_write_source_archive_url_defaults_to_empty_string(job1: Job) -> None:
    sid = write_source(
        job1,
        url="https://example.com",
        title="t",
        raw_content="content",
        kind="html",
    )
    sha = content_sha256(clean_content("content"))
    sidecar = json.loads((job1.root / "sources" / f"{sha}.json").read_text())
    assert sidecar["archive_url"] == ""

    conn = db.connect(job1.db_path)
    try:
        row = conn.execute("SELECT archive_url FROM sources WHERE id = ?", (sid,)).fetchone()
    finally:
        conn.close()
    # DB column allows NULL when not provided.
    assert row["archive_url"] is None


def test_write_source_dedups_across_jobs(job1: Job, job2: Job) -> None:
    raw = "shared content for two jobs"
    sid1 = write_source(
        job1,
        url="https://a.example.com",
        title="A",
        raw_content=raw,
        kind="html",
    )
    sid2 = write_source(
        job2,
        url="https://b.example.com",
        title="B",
        raw_content=raw,
        kind="html",
    )
    assert sid1 == sid2

    sha = content_sha256(clean_content(raw))

    # Canonical file lives under the first writer's job folder only.
    assert (job1.root / "sources" / f"{sha}.md").exists()
    assert not (job2.root / "sources" / f"{sha}.md").exists()
    assert not (job2.root / "sources" / f"{sha}.json").exists()

    conn = db.connect(job1.db_path)
    try:
        sources_count = conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE sha256 = ?", (sha,)
        ).fetchone()["n"]
        link_rows = conn.execute(
            "SELECT job_id FROM job_sources WHERE source_id = ? ORDER BY job_id", (sid1,)
        ).fetchall()
    finally:
        conn.close()

    assert sources_count == 1
    assert sorted(r["job_id"] for r in link_rows) == sorted([job1.id, job2.id])


def test_write_source_dedups_on_whitespace_variation(job1: Job, job2: Job) -> None:
    sid1 = write_source(job1, url=None, title=None, raw_content="payload", kind=None)
    sid2 = write_source(
        job2,
        url=None,
        title=None,
        raw_content="  payload\n\n",
        kind=None,
    )
    assert sid1 == sid2


def test_write_source_same_job_link_idempotent(job1: Job) -> None:
    sid_a = write_source(job1, url=None, title=None, raw_content="dup", kind=None)
    sid_b = write_source(job1, url=None, title=None, raw_content="dup", kind=None)
    assert sid_a == sid_b

    conn = db.connect(job1.db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM job_sources WHERE source_id = ?", (sid_a,)
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 1


def test_write_source_rejects_empty_content(job1: Job) -> None:
    with pytest.raises(ValueError):
        write_source(job1, url=None, title=None, raw_content="", kind=None)
    with pytest.raises(ValueError):
        write_source(job1, url=None, title=None, raw_content="   \n  ", kind=None)


def test_write_source_leaves_no_tmp_files(job1: Job) -> None:
    write_source(job1, url=None, title=None, raw_content="some content", kind=None)
    leftover = list((job1.root / "sources").glob("*.tmp"))
    assert leftover == []


def test_write_source_fetched_at_defaults_to_now(job1: Job) -> None:
    import time

    before = int(time.time())
    sid = write_source(job1, url=None, title=None, raw_content="content", kind=None)
    after = int(time.time())

    conn = db.connect(job1.db_path)
    try:
        row = conn.execute("SELECT fetched_at FROM sources WHERE id = ?", (sid,)).fetchone()
    finally:
        conn.close()

    assert before <= row["fetched_at"] <= after
