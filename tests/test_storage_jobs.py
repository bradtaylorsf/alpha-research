"""Tests for `research_agent.storage.jobs`."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pytest

from research_agent.storage import db, jobs
from research_agent.storage.jobs import (
    DEFAULT_JOBS_ROOT,
    Job,
    _atomic_write_text,
    _slugify,
    list_jobs,
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
def sample_intake() -> dict:
    return {
        "goal": "Investigate Widget Co financials",
        "domain": "corporate",
        "time_cap_hours": 24,
        "budget_cap_usd": 25.0,
        "aggressiveness": "balanced",
    }


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------


def test_slugify_basic_spaces_and_punctuation() -> None:
    assert _slugify("Investigate Widget Co!") == "investigate-widget-co"


def test_slugify_collapses_runs() -> None:
    assert _slugify("foo   ---   bar") == "foo-bar"


def test_slugify_strips_leading_trailing_dashes() -> None:
    assert _slugify("---hello world---") == "hello-world"


def test_slugify_unicode_drops_to_ascii_alnum() -> None:
    # Non-ascii letters are non-[a-z0-9]; they collapse to a separator.
    assert _slugify("café münchen 2026") == "caf-m-nchen-2026"


def test_slugify_max_60_chars_default() -> None:
    long = "a" * 200
    assert len(_slugify(long)) == 60


def test_slugify_truncation_strips_trailing_dash() -> None:
    # Build text that hits max_len exactly on a dash.
    text = ("ab-" * 30)[:65]
    out = _slugify(text)
    assert len(out) <= 60
    assert not out.endswith("-")


def test_slugify_rejects_empty() -> None:
    with pytest.raises(ValueError):
        _slugify("")
    with pytest.raises(ValueError):
        _slugify("   ")
    with pytest.raises(ValueError):
        _slugify("!!!---!!!")


@pytest.mark.parametrize("traversal", ["foo/bar", "foo\\bar", "../etc/passwd", ".."])
def test_slugify_rejects_path_traversal(traversal: str) -> None:
    with pytest.raises(ValueError):
        _slugify(traversal)


# ---------------------------------------------------------------------------
# _atomic_write_text
# ---------------------------------------------------------------------------


def test_atomic_write_text_writes_through_tmp(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "out.json"
    captured: list[Path] = []

    real_replace = os.replace

    def spy_replace(src, dst):
        captured.append(Path(src))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)
    _atomic_write_text(target, "hello")

    assert target.read_text() == "hello"
    assert captured == [target.with_suffix(target.suffix + ".tmp")]
    # No leftover .tmp file
    assert not (target.with_suffix(target.suffix + ".tmp")).exists()


def test_atomic_write_text_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "out.txt"
    _atomic_write_text(target, "x")
    assert target.read_text() == "x"


# ---------------------------------------------------------------------------
# Job.create
# ---------------------------------------------------------------------------


def test_create_writes_full_folder_layout(
    sample_intake: dict, jobs_root: Path, db_path: Path
) -> None:
    job = Job.create(
        sample_intake,
        jobs_root=jobs_root,
        db_path=db_path,
        today=date(2026, 5, 2),
    )

    assert job.id == "2026-05-02-investigate-widget-co-financials"
    assert job.root == jobs_root / job.id
    assert job.status == "pending"
    assert job.goal == sample_intake["goal"]
    assert job.domain == "corporate"

    # All required subdirs exist.
    for sub in ("plan", "findings", "sources", "synthesis", "critique", "report.history"):
        assert (job.root / sub).is_dir(), sub

    # Sidecar files.
    assert (job.root / "events.jsonl").exists()
    assert (job.root / "events.jsonl").read_text() == ""

    goal_md = (job.root / "goal.md").read_text()
    assert goal_md.strip() == sample_intake["goal"].strip()

    intake_disk = json.loads((job.root / "intake.json").read_text())
    assert intake_disk == sample_intake

    job_meta = json.loads((job.root / "job.json").read_text())
    assert job_meta["id"] == job.id
    assert job_meta["goal"] == sample_intake["goal"]
    assert job_meta["status"] == "pending"
    assert job_meta["intake"] == sample_intake


def test_create_inserts_db_row(sample_intake: dict, jobs_root: Path, db_path: Path) -> None:
    job = Job.create(sample_intake, jobs_root=jobs_root, db_path=db_path)

    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, goal, domain, status, intake_json, time_cap_hours,"
            " budget_cap_usd, aggressiveness, created_at, last_activity_at"
            " FROM jobs WHERE id = ?",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["id"] == job.id
    assert row["goal"] == sample_intake["goal"]
    assert row["status"] == "pending"
    assert row["time_cap_hours"] == 24
    assert row["budget_cap_usd"] == 25.0
    assert row["aggressiveness"] == "balanced"
    assert row["created_at"] == row["last_activity_at"]
    assert json.loads(row["intake_json"]) == sample_intake


def test_create_rejects_missing_goal(jobs_root: Path, db_path: Path) -> None:
    with pytest.raises(ValueError):
        Job.create({}, jobs_root=jobs_root, db_path=db_path)
    with pytest.raises(ValueError):
        Job.create({"goal": "   "}, jobs_root=jobs_root, db_path=db_path)


def test_create_refuses_duplicate_folder(
    sample_intake: dict, jobs_root: Path, db_path: Path
) -> None:
    Job.create(sample_intake, jobs_root=jobs_root, db_path=db_path, today=date(2026, 5, 2))
    with pytest.raises(FileExistsError):
        Job.create(sample_intake, jobs_root=jobs_root, db_path=db_path, today=date(2026, 5, 2))


# ---------------------------------------------------------------------------
# Job.load
# ---------------------------------------------------------------------------


def test_load_round_trip(sample_intake: dict, jobs_root: Path, db_path: Path) -> None:
    created = Job.create(
        sample_intake, jobs_root=jobs_root, db_path=db_path, today=date(2026, 5, 2)
    )
    loaded = Job.load(created.id, jobs_root=jobs_root, db_path=db_path)

    assert loaded.id == created.id
    assert loaded.root == created.root
    assert loaded.goal == created.goal
    assert loaded.status == "pending"
    assert loaded.intake == sample_intake
    assert loaded.created_at == created.created_at


def test_load_raises_when_folder_missing(jobs_root: Path, db_path: Path) -> None:
    # Insert DB row directly so only the folder is missing.
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO jobs (id, goal, status, intake_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("2026-05-02-orphan-row", "g", "pending", "{}", 0),
            )
    finally:
        conn.close()

    with pytest.raises(FileNotFoundError):
        Job.load("2026-05-02-orphan-row", jobs_root=jobs_root, db_path=db_path)


def test_load_raises_when_db_row_missing(
    sample_intake: dict, jobs_root: Path, db_path: Path
) -> None:
    job = Job.create(sample_intake, jobs_root=jobs_root, db_path=db_path, today=date(2026, 5, 2))
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job.id,))
    finally:
        conn.close()

    with pytest.raises(KeyError):
        Job.load(job.id, jobs_root=jobs_root, db_path=db_path)


def test_load_rejects_invalid_job_id(jobs_root: Path, db_path: Path) -> None:
    with pytest.raises(ValueError):
        Job.load("../etc/passwd", jobs_root=jobs_root, db_path=db_path)
    with pytest.raises(ValueError):
        Job.load("not-a-job-id", jobs_root=jobs_root, db_path=db_path)
    with pytest.raises(ValueError):
        Job.load("2026/05/02-foo", jobs_root=jobs_root, db_path=db_path)


# ---------------------------------------------------------------------------
# list_jobs
# ---------------------------------------------------------------------------


def test_list_jobs_returns_db_rows_newest_first(jobs_root: Path, db_path: Path) -> None:
    j1 = Job.create({"goal": "alpha"}, jobs_root=jobs_root, db_path=db_path, today=date(2026, 5, 1))
    # Force a clock tick so created_at differs.
    time.sleep(1.05)
    j2 = Job.create({"goal": "beta"}, jobs_root=jobs_root, db_path=db_path, today=date(2026, 5, 2))

    rows = list_jobs(db_path=db_path)
    ids = [r["id"] for r in rows]
    assert ids == [j2.id, j1.id]
    assert {"id", "goal", "status", "created_at", "last_activity_at", "cost_so_far_usd"} <= set(
        rows[0].keys()
    )


def test_list_jobs_filters_by_status(jobs_root: Path, db_path: Path) -> None:
    j1 = Job.create({"goal": "one"}, jobs_root=jobs_root, db_path=db_path, today=date(2026, 5, 1))
    j2 = Job.create({"goal": "two"}, jobs_root=jobs_root, db_path=db_path, today=date(2026, 5, 2))
    j2.set_status("running")

    pending = [r["id"] for r in list_jobs(status="pending", db_path=db_path)]
    running = [r["id"] for r in list_jobs(status="running", db_path=db_path)]

    assert pending == [j1.id]
    assert running == [j2.id]


def test_list_jobs_uses_db_not_disk(sample_intake: dict, jobs_root: Path, db_path: Path) -> None:
    job = Job.create(sample_intake, jobs_root=jobs_root, db_path=db_path)
    # Nuke the folder; DB row remains.
    shutil.rmtree(job.root)
    rows = list_jobs(db_path=db_path)
    assert [r["id"] for r in rows] == [job.id]


# ---------------------------------------------------------------------------
# Job.set_status
# ---------------------------------------------------------------------------


def test_set_status_updates_db_and_job_json(
    sample_intake: dict, jobs_root: Path, db_path: Path
) -> None:
    job = Job.create(sample_intake, jobs_root=jobs_root, db_path=db_path)
    before_meta = json.loads((job.root / "job.json").read_text())
    before_activity = before_meta.get("last_activity_at")

    time.sleep(1.05)
    job.set_status("running")

    # In-memory mirror.
    assert job.status == "running"

    # job.json mirror.
    after_meta = json.loads((job.root / "job.json").read_text())
    assert after_meta["status"] == "running"
    assert "last_activity_at" in after_meta
    if before_activity is not None:
        assert after_meta["last_activity_at"] >= before_activity

    # DB.
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, last_activity_at, created_at FROM jobs WHERE id = ?",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "running"
    assert row["last_activity_at"] > row["created_at"]


def test_set_status_rejects_empty(sample_intake: dict, jobs_root: Path, db_path: Path) -> None:
    job = Job.create(sample_intake, jobs_root=jobs_root, db_path=db_path)
    with pytest.raises(ValueError):
        job.set_status("")


# ---------------------------------------------------------------------------
# Job.request_stop
# ---------------------------------------------------------------------------


def test_request_stop_creates_flag_file(
    sample_intake: dict, jobs_root: Path, db_path: Path
) -> None:
    job = Job.create(sample_intake, jobs_root=jobs_root, db_path=db_path)
    assert not (job.root / "STOP").exists()
    job.request_stop()
    assert (job.root / "STOP").exists()
    # Idempotent.
    job.request_stop()
    assert (job.root / "STOP").exists()


# ---------------------------------------------------------------------------
# Job.kill
# ---------------------------------------------------------------------------


def _spawn_sleeper(seconds: float = 30.0) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def test_kill_sigterm_path(sample_intake: dict, jobs_root: Path, db_path: Path) -> None:
    job = Job.create(sample_intake, jobs_root=jobs_root, db_path=db_path)
    proc = _spawn_sleeper(30.0)
    try:
        (job.root / "daemon.pid").write_text(str(proc.pid))
        job.kill()
        # Popen.wait reaps the zombie so we can confirm termination cleanly.
        rc = proc.wait(timeout=5)
        # SIGTERM yields a negative return code on POSIX (Python convention).
        assert rc == -signal.SIGTERM, f"expected SIGTERM exit, got {rc}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_kill_escalates_to_sigkill(
    sample_intake: dict, jobs_root: Path, db_path: Path, monkeypatch
) -> None:
    # Child ignores SIGTERM so kill() must escalate to SIGKILL after the window.
    code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    proc = subprocess.Popen([sys.executable, "-c", code])
    try:
        (job_root := (jobs_root / "fake")).mkdir(parents=True)
        (job_root / "daemon.pid").write_text(str(proc.pid))

        # In-memory Job pointing at a folder; no DB row is needed for kill().
        job = Job(
            id="2026-05-02-fake",
            root=job_root,
            goal="g",
            domain=None,
            status="running",
            intake={"goal": "g"},
            created_at=0,
            db_path=db_path,
        )

        # Shrink the 10s escalation window to keep the test fast.
        monkeypatch.setattr(jobs, "KILL_ESCALATION_SECONDS", 0.5)
        monkeypatch.setattr(jobs, "KILL_POLL_INTERVAL_SECONDS", 0.1)

        # Give the child time to install its SIGTERM handler.
        time.sleep(0.3)
        job.kill()
        rc = proc.wait(timeout=5)
        assert rc == -signal.SIGKILL, f"expected SIGKILL exit, got {rc}"
    finally:
        if proc.poll() is None:
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)


def test_kill_handles_already_dead_pid(sample_intake: dict, jobs_root: Path, db_path: Path) -> None:
    job = Job.create(sample_intake, jobs_root=jobs_root, db_path=db_path)
    proc = _spawn_sleeper(0.01)
    proc.wait()
    # After wait() the kernel may recycle the pid eventually, but in the
    # short interval until then os.kill() will hit ProcessLookupError —
    # which kill() must swallow.
    (job.root / "daemon.pid").write_text(str(proc.pid))
    job.kill()


def test_kill_raises_when_no_pid_file(sample_intake: dict, jobs_root: Path, db_path: Path) -> None:
    job = Job.create(sample_intake, jobs_root=jobs_root, db_path=db_path)
    with pytest.raises(FileNotFoundError):
        job.kill()


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_default_jobs_root_is_jobs() -> None:
    assert DEFAULT_JOBS_ROOT == Path("jobs")
