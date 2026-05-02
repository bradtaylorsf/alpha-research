"""Tests for `research_agent.storage.export` (issue #42)."""

from __future__ import annotations

import time
import zipfile
from datetime import date
from pathlib import Path

import pytest

from research_agent.storage import db
from research_agent.storage.export import export_md_bundle, export_zip
from research_agent.storage.jobs import Job


@pytest.fixture
def jobs_root(tmp_path: Path) -> Path:
    return tmp_path / "jobs"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.sqlite"
    db.migrate(path=path).close()
    return path


@pytest.fixture
def seeded_job(jobs_root: Path, db_path: Path) -> Job:
    """Build a job with a report, two findings, one source, and a history file."""
    job = Job.create(
        {
            "goal": "Investigate exports",
            "domain": "general",
            "time_cap_hours": 4,
            "budget_cap_usd": 7.5,
        },
        jobs_root=jobs_root,
        db_path=db_path,
        today=date(2026, 5, 2),
    )

    (job.root / "report.md").write_text("# Final Report\n\nReport body.\n", encoding="utf-8")
    (job.root / "findings" / "000001.md").write_text(
        "# Finding 000001\n\nFirst claim.\n", encoding="utf-8"
    )
    (job.root / "findings" / "000002.md").write_text(
        "# Finding 000002\n\nSecond claim.\n", encoding="utf-8"
    )
    (job.root / "report.history").mkdir(exist_ok=True)
    (job.root / "report.history" / "20260501T000000Z.md").write_text(
        "# Prior Report\n\nArchived body.\n", encoding="utf-8"
    )

    now = int(time.time())
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO findings"
                " (job_id, md_path, claim, confidence, source_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (job.id, "findings/000001.md", "First claim.", 0.9, "[1]", now),
            )
            conn.execute(
                "INSERT INTO findings"
                " (job_id, md_path, claim, confidence, source_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (job.id, "findings/000002.md", "Second claim.", 0.7, "[1]", now),
            )
            cur = conn.execute(
                "INSERT INTO sources"
                " (sha256, url, title, fetched_at, archive_url, md_path, kind)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "a" * 64,
                    "https://example.com/article",
                    "Example article",
                    now,
                    "https://web.archive.org/web/2026/example",
                    "sources/aaa.md",
                    "web",
                ),
            )
            sid = cur.lastrowid
            conn.execute(
                "INSERT INTO job_sources (job_id, source_id) VALUES (?, ?)",
                (job.id, sid),
            )
    finally:
        conn.close()

    return job


# ---------------------------------------------------------------------------
# export_zip
# ---------------------------------------------------------------------------


def test_export_zip_includes_expected_paths(seeded_job: Job, tmp_path: Path) -> None:
    out = tmp_path / "out.zip"
    written = export_zip(seeded_job, out, include_history=False)
    assert written == out
    assert out.exists()

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())

    assert f"{seeded_job.id}/job.json" in names
    assert f"{seeded_job.id}/report.md" in names
    assert f"{seeded_job.id}/findings/000001.md" in names
    assert f"{seeded_job.id}/findings/000002.md" in names


def test_export_zip_excludes_history_by_default(seeded_job: Job, tmp_path: Path) -> None:
    out = tmp_path / "out.zip"
    export_zip(seeded_job, out, include_history=False)

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()

    assert not any("report.history/" in n for n in names)


def test_export_zip_includes_history_when_requested(seeded_job: Job, tmp_path: Path) -> None:
    out = tmp_path / "out.zip"
    export_zip(seeded_job, out, include_history=True)

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())

    assert f"{seeded_job.id}/report.history/20260501T000000Z.md" in names


def test_export_zip_does_not_leave_tmp_file(seeded_job: Job, tmp_path: Path) -> None:
    out = tmp_path / "out.zip"
    export_zip(seeded_job, out, include_history=False)
    siblings = sorted(p.name for p in tmp_path.iterdir())
    assert "out.zip" in siblings
    assert not any(s.endswith(".tmp") for s in siblings)


# ---------------------------------------------------------------------------
# export_md_bundle
# ---------------------------------------------------------------------------


def test_export_md_bundle_renders_expected_structure(seeded_job: Job, tmp_path: Path) -> None:
    out = tmp_path / "bundle.md"
    written = export_md_bundle(seeded_job, out, include_history=False)
    assert written == out

    body = out.read_text(encoding="utf-8")
    assert body.startswith(f"# {seeded_job.id}\n")
    assert "## Report" in body
    assert "Report body." in body
    assert "## Findings" in body
    assert "### Finding 000001" in body
    assert "### Finding 000002" in body
    assert "First claim." in body
    assert "Second claim." in body
    assert "## Sources" in body


def test_export_md_bundle_includes_archive_url(seeded_job: Job, tmp_path: Path) -> None:
    out = tmp_path / "bundle.md"
    export_md_bundle(seeded_job, out, include_history=False)
    body = out.read_text(encoding="utf-8")
    assert "https://web.archive.org/web/2026/example" in body
    assert "https://example.com/article" in body


def test_export_md_bundle_history_gated_by_flag(seeded_job: Job, tmp_path: Path) -> None:
    no_hist = tmp_path / "no_hist.md"
    export_md_bundle(seeded_job, no_hist, include_history=False)
    assert "## Report History" not in no_hist.read_text(encoding="utf-8")

    with_hist = tmp_path / "with_hist.md"
    export_md_bundle(seeded_job, with_hist, include_history=True)
    body = with_hist.read_text(encoding="utf-8")
    assert "## Report History" in body
    assert "### 20260501T000000Z" in body
    assert "Archived body." in body


def test_export_md_bundle_handles_missing_report(jobs_root: Path, db_path: Path) -> None:
    job = Job.create(
        {"goal": "no report yet"},
        jobs_root=jobs_root,
        db_path=db_path,
        today=date(2026, 5, 3),
    )
    out = jobs_root.parent / "bundle.md"
    export_md_bundle(job, out, include_history=False)
    body = out.read_text(encoding="utf-8")
    assert "## Report" in body
    assert "(no report.md present)" in body
    assert "(no findings recorded)" in body
    assert "(no sources recorded)" in body


def test_export_md_bundle_intake_block_includes_caps(seeded_job: Job, tmp_path: Path) -> None:
    out = tmp_path / "bundle.md"
    export_md_bundle(seeded_job, out, include_history=False)
    body = out.read_text(encoding="utf-8")
    assert "goal: Investigate exports" in body
    assert "time_cap_hours: 4" in body
    assert "budget_cap_usd: 7.5" in body
