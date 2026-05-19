"""Tests for `research_agent.storage.sources`."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from research_agent.storage import db
from research_agent.storage.jobs import Job
from research_agent.storage.sources import (
    clean_content,
    content_sha256,
    read_source_metadata,
    read_source_sidecar,
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

    # Both job folders carry the markdown — the original "first writer
    # only" behavior was fragile to job-folder deletion (the orphaned
    # row would point at a path that no longer existed). Each job is now
    # self-contained on disk, while the SQLite ``sources`` table stays
    # deduplicated by sha256.
    assert (job1.root / "sources" / f"{sha}.md").exists()
    assert (job2.root / "sources" / f"{sha}.md").exists()
    assert (job2.root / "sources" / f"{sha}.json").exists()

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


# ---------------------------------------------------------------------------
# metadata round-trip (issue #353 — dossier mode, epic #359)
# ---------------------------------------------------------------------------


def test_metadata_round_trip_single_page(job1: Job) -> None:
    """write_source(metadata=...) survives a sidecar reload as the same dict."""
    metadata = {
        "parent_file": "file:///x.pdf",
        "page_no": 3,
        "page_chunk": None,
    }
    write_source(
        job1,
        url="file:///x.pdf",
        title="x.pdf",
        raw_content="page three body alpha alpha alpha",
        kind="local",
        metadata=metadata,
    )

    sha = content_sha256(clean_content("page three body alpha alpha alpha"))
    sidecar = read_source_sidecar(job1, sha)
    assert isinstance(sidecar, dict)
    assert sidecar["sha256"] == sha

    round_tripped = sidecar["metadata"]
    assert isinstance(round_tripped, dict)
    assert round_tripped == metadata

    # The convenience reader returns just the metadata dict.
    assert read_source_metadata(job1, sha) == metadata


def test_metadata_round_trip_multiple_pages_same_parent(job1: Job) -> None:
    """Same parent_file with different page_no values all round-trip cleanly."""
    parent = "file:///filing.pdf"
    pages = [1, 5, 12]
    shas: list[tuple[int, str]] = []
    for page_no in pages:
        body = f"page {page_no} unique content for round-trip test {page_no}"
        write_source(
            job1,
            url=parent,
            title="filing.pdf",
            raw_content=body,
            kind="local",
            metadata={
                "parent_file": parent,
                "page_no": page_no,
                "page_chunk": None,
            },
        )
        shas.append((page_no, content_sha256(clean_content(body))))

    seen_page_nos: list[int] = []
    for page_no, sha in shas:
        meta = read_source_metadata(job1, sha)
        assert meta["parent_file"] == parent
        assert meta["page_no"] == page_no
        assert meta["page_chunk"] is None
        seen_page_nos.append(meta["page_no"])

    assert seen_page_nos == pages


def test_metadata_round_trip_page_chunk_subindex(job1: Job) -> None:
    """A page sub-chunked inside the page keeps distinct page_chunk values."""
    parent = "file:///big.pdf"
    page_no = 7
    sub_chunks = [1, 2, 3]
    seen: list[int] = []
    for chunk_idx in sub_chunks:
        body = f"page seven sub chunk {chunk_idx} long body " + "blah " * 20
        write_source(
            job1,
            url=parent,
            title="big.pdf",
            raw_content=body,
            kind="local",
            metadata={
                "parent_file": parent,
                "page_no": page_no,
                "page_chunk": chunk_idx,
            },
        )
        sha = content_sha256(clean_content(body))
        meta = read_source_metadata(job1, sha)
        assert meta["page_no"] == page_no
        seen.append(meta["page_chunk"])

    assert seen == sub_chunks


def test_metadata_round_trip_html_page_no_is_null(job1: Job) -> None:
    """HTML sources stamp parent_file but leave page_no/page_chunk as null."""
    parent = "file:///page.html"
    write_source(
        job1,
        url=parent,
        title="page.html",
        raw_content="full html body extracted into one chunk for round-trip test",
        kind="local",
        metadata={
            "parent_file": parent,
            "page_no": None,
            "page_chunk": None,
        },
    )

    sha = content_sha256(
        clean_content(
            "full html body extracted into one chunk for round-trip test"
        )
    )
    meta = read_source_metadata(job1, sha)
    assert meta["parent_file"] == parent
    assert meta["page_no"] is None
    assert meta["page_chunk"] is None


def test_metadata_returns_dict_not_string(job1: Job) -> None:
    """metadata must come back as a dict, not the raw JSON string."""
    write_source(
        job1,
        url=None,
        title=None,
        raw_content="raw return shape body",
        kind=None,
        metadata={"parent_file": "file:///shape.pdf", "page_no": 1, "page_chunk": None},
    )
    sha = content_sha256(clean_content("raw return shape body"))
    meta = read_source_metadata(job1, sha)
    assert isinstance(meta, dict)
    assert not isinstance(meta, str)
    # Belt + suspenders: confirm we didn't accidentally re-serialise.
    sidecar_text = (job1.root / "sources" / f"{sha}.json").read_text(encoding="utf-8")
    parsed = json.loads(sidecar_text)
    assert isinstance(parsed["metadata"], dict)


def test_metadata_default_is_empty_dict(job1: Job) -> None:
    """write_source without metadata persists {} on disk and returns {} on read."""
    write_source(
        job1,
        url=None,
        title=None,
        raw_content="no metadata supplied body",
        kind=None,
    )
    sha = content_sha256(clean_content("no metadata supplied body"))
    sidecar = read_source_sidecar(job1, sha)
    assert sidecar["metadata"] == {}
    assert read_source_metadata(job1, sha) == {}


def test_metadata_last_writer_wins_within_same_job(job1: Job) -> None:
    """Re-writing the same content under the same job replaces the sidecar metadata."""
    body = "same content different metadata round"
    write_source(
        job1,
        url=None,
        title=None,
        raw_content=body,
        kind=None,
        metadata={"page_no": 1},
    )
    write_source(
        job1,
        url=None,
        title=None,
        raw_content=body,
        kind=None,
        metadata={"page_no": 99},
    )
    sha = content_sha256(clean_content(body))
    meta = read_source_metadata(job1, sha)
    assert meta == {"page_no": 99}


def test_metadata_per_job_independence_on_dedup_hit(job1: Job, job2: Job) -> None:
    """Same content written under two jobs keeps each job's sidecar metadata."""
    body = "shared body across two jobs for metadata isolation"
    write_source(
        job1,
        url="file:///doc.pdf",
        title="doc.pdf",
        raw_content=body,
        kind="local",
        metadata={"parent_file": "file:///doc.pdf", "page_no": 1, "page_chunk": None},
    )
    write_source(
        job2,
        url="file:///doc.pdf",
        title="doc.pdf",
        raw_content=body,
        kind="local",
        metadata={"parent_file": "file:///doc.pdf", "page_no": 42, "page_chunk": None},
    )

    sha = content_sha256(clean_content(body))
    meta1 = read_source_metadata(job1, sha)
    meta2 = read_source_metadata(job2, sha)
    assert meta1["page_no"] == 1
    assert meta2["page_no"] == 42
    # Both job folders carry their own sidecar (self-contained on disk).
    assert (job1.root / "sources" / f"{sha}.json").exists()
    assert (job2.root / "sources" / f"{sha}.json").exists()


def test_metadata_round_trip_leaves_no_tmp_files(job1: Job) -> None:
    """Atomic-write contract: round-trip must not leak *.tmp sidecars."""
    write_source(
        job1,
        url=None,
        title=None,
        raw_content="atomic write contract body",
        kind=None,
        metadata={"page_no": 4},
    )
    leftover = list((job1.root / "sources").glob("*.tmp"))
    assert leftover == []


def test_write_source_rejects_non_dict_metadata(job1: Job) -> None:
    """write_source surfaces a clear error when metadata is not a dict."""
    with pytest.raises(ValueError):
        write_source(
            job1,
            url=None,
            title=None,
            raw_content="content",
            kind=None,
            metadata="not a dict",  # type: ignore[arg-type]
        )


def test_read_source_sidecar_missing_raises(job1: Job) -> None:
    with pytest.raises(FileNotFoundError):
        read_source_sidecar(job1, "0" * 64)


def test_read_source_metadata_handles_legacy_missing_key(
    job1: Job, tmp_path: Path
) -> None:
    """Sidecars written before metadata existed surface as an empty dict, not a crash."""
    sha = content_sha256(clean_content("legacy body"))
    write_source(
        job1,
        url=None,
        title=None,
        raw_content="legacy body",
        kind=None,
    )
    sidecar_path = job1.root / "sources" / f"{sha}.json"
    raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
    raw.pop("metadata", None)
    sidecar_path.write_text(json.dumps(raw), encoding="utf-8")

    assert read_source_metadata(job1, sha) == {}
    sidecar = read_source_sidecar(job1, sha)
    assert sidecar["metadata"] == {}


def test_read_source_sidecar_rejects_non_dict_metadata(job1: Job) -> None:
    """A sidecar with a non-dict metadata value is a clear, recoverable error."""
    sha = content_sha256(clean_content("malformed metadata body"))
    write_source(
        job1,
        url=None,
        title=None,
        raw_content="malformed metadata body",
        kind=None,
    )
    sidecar_path = job1.root / "sources" / f"{sha}.json"
    raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
    raw["metadata"] = "this should be a dict"
    sidecar_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError):
        read_source_sidecar(job1, sha)
