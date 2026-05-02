"""Tests for `research_agent.storage.markdown`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_agent.storage import db
from research_agent.storage.jobs import Job
from research_agent.storage.markdown import (
    write_finding,
    write_plan,
    write_report,
    write_synthesis,
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
        {"goal": "Test markdown writers"},
        jobs_root=jobs_root,
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# write_finding
# ---------------------------------------------------------------------------


def test_write_finding_writes_md_json_and_db_row(job: Job) -> None:
    fid = write_finding(
        job,
        claim="Widget Co revenue grew 12% YoY in Q4 2025.",
        confidence=0.85,
        source_ids=[1, 2],
        tags=["finance", "q4"],
    )
    assert fid == 1

    md_path = job.root / "findings" / "000001.md"
    json_path = job.root / "findings" / "000001.json"
    assert md_path.exists()
    assert json_path.exists()

    md_text = md_path.read_text()
    assert "Finding 000001" in md_text
    assert "Widget Co revenue grew 12% YoY" in md_text
    assert "0.85" in md_text

    sidecar = json.loads(json_path.read_text())
    assert sidecar["id"] == 1
    assert sidecar["claim"] == "Widget Co revenue grew 12% YoY in Q4 2025."
    assert sidecar["confidence"] == 0.85
    assert sidecar["source_ids"] == [1, 2]
    assert sidecar["tags"] == ["finance", "q4"]
    assert sidecar["contradicts"] is None
    assert sidecar["md_path"] == "findings/000001.md"

    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            "SELECT id, job_id, md_path, claim, confidence, source_ids,"
            " contradicts, tags FROM findings WHERE id = ?",
            (fid,),
        ).fetchone()
    finally:
        conn.close()

    assert row["id"] == 1
    assert row["job_id"] == job.id
    assert row["md_path"] == "findings/000001.md"
    assert row["claim"] == "Widget Co revenue grew 12% YoY in Q4 2025."
    assert row["confidence"] == 0.85
    assert json.loads(row["source_ids"]) == [1, 2]
    assert row["contradicts"] is None
    assert json.loads(row["tags"]) == ["finance", "q4"]


def test_write_finding_zero_pads_to_six_digits(job: Job) -> None:
    fid = write_finding(job, claim="x", confidence=0.5, source_ids=[1])
    assert (job.root / "findings" / "000001.md").exists()
    assert (job.root / "findings" / "000001.json").exists()
    assert fid == 1


def test_write_finding_monotonic_ids(job: Job) -> None:
    a = write_finding(job, claim="claim a", confidence=0.5, source_ids=[1])
    b = write_finding(job, claim="claim b", confidence=0.5, source_ids=[1])
    assert a == 1
    assert b == 2
    assert (job.root / "findings" / "000002.md").exists()


def test_write_finding_with_contradicts(job: Job) -> None:
    first = write_finding(job, claim="claim a", confidence=0.6, source_ids=[1])
    second = write_finding(
        job,
        claim="claim b",
        confidence=0.7,
        source_ids=[2],
        contradicts=[first],
    )
    sidecar = json.loads((job.root / "findings" / f"{second:06d}.json").read_text())
    assert sidecar["contradicts"] == [first]


@pytest.mark.parametrize("bad_conf", [-0.1, 1.5, 2.0, -1.0])
def test_write_finding_rejects_out_of_range_confidence(job: Job, bad_conf: float) -> None:
    with pytest.raises(ValueError):
        write_finding(job, claim="x", confidence=bad_conf, source_ids=[1])


def test_write_finding_rejects_non_numeric_confidence(job: Job) -> None:
    with pytest.raises(ValueError):
        write_finding(job, claim="x", confidence="0.5", source_ids=[1])  # type: ignore[arg-type]


def test_write_finding_rejects_empty_source_ids(job: Job) -> None:
    with pytest.raises(ValueError):
        write_finding(job, claim="x", confidence=0.5, source_ids=[])


def test_write_finding_rejects_non_int_source_ids(job: Job) -> None:
    with pytest.raises(ValueError):
        write_finding(job, claim="x", confidence=0.5, source_ids=["1"])  # type: ignore[list-item]


def test_write_finding_rejects_empty_claim(job: Job) -> None:
    with pytest.raises(ValueError):
        write_finding(job, claim="   ", confidence=0.5, source_ids=[1])


def test_write_finding_leaves_no_tmp_files(job: Job) -> None:
    write_finding(job, claim="x", confidence=0.5, source_ids=[1])
    leftover = list((job.root / "findings").glob("*.tmp"))
    assert leftover == []


# ---------------------------------------------------------------------------
# write_plan
# ---------------------------------------------------------------------------


def test_write_plan_versions_monotonically(job: Job) -> None:
    v1 = write_plan(job, {"steps": ["s1", "s2"]})
    v2 = write_plan(job, {"steps": ["s1", "s2", "s3"]})
    assert v1 == 1
    assert v2 == 2
    assert (job.root / "plan" / "0001.md").exists()
    assert (job.root / "plan" / "0001.json").exists()
    assert (job.root / "plan" / "0002.md").exists()
    assert (job.root / "plan" / "0002.json").exists()


def test_write_plan_json_sidecar_is_raw_payload(job: Job) -> None:
    payload = {"steps": ["alpha", "beta"], "budget": 5.0}
    write_plan(job, payload)
    on_disk = json.loads((job.root / "plan" / "0001.json").read_text())
    assert on_disk == payload


def test_write_plan_md_contains_pretty_payload(job: Job) -> None:
    payload = {"steps": ["alpha"], "budget": 5.0}
    write_plan(job, payload)
    md = (job.root / "plan" / "0001.md").read_text()
    assert "Plan v0001" in md
    assert '"alpha"' in md
    assert '"budget"' in md


def test_write_plan_inserts_db_row(job: Job) -> None:
    payload = {"steps": ["s1"]}
    version = write_plan(job, payload)

    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            "SELECT job_id, version, payload_json FROM plans WHERE job_id = ? AND version = ?",
            (job.id, version),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["job_id"] == job.id
    assert row["version"] == 1
    assert json.loads(row["payload_json"]) == payload


def test_write_plan_rejects_non_dict(job: Job) -> None:
    with pytest.raises(ValueError):
        write_plan(job, ["not", "a", "dict"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# write_synthesis
# ---------------------------------------------------------------------------


def test_write_synthesis_versions_monotonically(job: Job) -> None:
    v1 = write_synthesis(job, content="first pass", model="opus-4")
    v2 = write_synthesis(job, content="second pass", model="opus-4", cost_usd=0.42)
    assert v1 == 1
    assert v2 == 2
    assert (job.root / "synthesis" / "0001.md").exists()
    assert (job.root / "synthesis" / "0002.md").exists()


def test_write_synthesis_sidecar_records_metadata(job: Job) -> None:
    write_synthesis(job, content="report body", model="opus-4", cost_usd=0.5)
    sidecar = json.loads((job.root / "synthesis" / "0001.json").read_text())
    assert sidecar["version"] == 1
    assert sidecar["model"] == "opus-4"
    assert sidecar["cost_usd"] == 0.5
    assert "created_at" in sidecar


def test_write_synthesis_cost_optional(job: Job) -> None:
    write_synthesis(job, content="x", model="opus-4")
    sidecar = json.loads((job.root / "synthesis" / "0001.json").read_text())
    assert sidecar["cost_usd"] is None


def test_write_synthesis_inserts_db_row(job: Job) -> None:
    write_synthesis(job, content="report body", model="opus-4", cost_usd=0.5)
    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            "SELECT version, md_path, model, cost_usd FROM syntheses WHERE job_id = ?",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["version"] == 1
    assert row["md_path"] == "synthesis/0001.md"
    assert row["model"] == "opus-4"
    assert row["cost_usd"] == 0.5


def test_write_synthesis_rejects_empty(job: Job) -> None:
    with pytest.raises(ValueError):
        write_synthesis(job, content="", model="opus-4")
    with pytest.raises(ValueError):
        write_synthesis(job, content="content", model="")


# ---------------------------------------------------------------------------
# write_report
# ---------------------------------------------------------------------------


def test_write_report_writes_to_report_md(job: Job) -> None:
    path = write_report(job, "first report\n")
    assert path == job.root / "report.md"
    assert path.read_text() == "first report\n"


def test_write_report_appends_trailing_newline(job: Job) -> None:
    write_report(job, "no newline")
    assert (job.root / "report.md").read_text() == "no newline\n"


def test_write_report_rotates_prior_to_history(job: Job) -> None:
    write_report(job, "first")
    write_report(job, "second")

    assert (job.root / "report.md").read_text() == "second\n"

    history_files = sorted((job.root / "report.history").glob("*.md"))
    assert len(history_files) == 1
    assert history_files[0].read_text() == "first\n"
    assert history_files[0].name.endswith(".md")
    # Filename like 20260502T123456Z.md or 20260502T123456Z-1.md
    assert "T" in history_files[0].stem
    assert history_files[0].stem.split("-")[0].endswith("Z")


def test_write_report_rotation_handles_same_second(job: Job) -> None:
    write_report(job, "a")
    write_report(job, "b")
    write_report(job, "c")
    history_files = list((job.root / "report.history").glob("*.md"))
    assert len(history_files) == 2


def test_write_report_no_rotation_when_no_prior(job: Job) -> None:
    write_report(job, "only one")
    history_files = list((job.root / "report.history").glob("*.md"))
    assert history_files == []


def test_write_report_leaves_no_tmp_files(job: Job) -> None:
    write_report(job, "x")
    write_report(job, "y")
    leftover = list(job.root.rglob("*.tmp"))
    assert leftover == []


def test_write_report_rejects_non_string(job: Job) -> None:
    with pytest.raises(ValueError):
        write_report(job, 123)  # type: ignore[arg-type]
