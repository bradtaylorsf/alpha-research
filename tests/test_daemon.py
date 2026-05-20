"""Tests for ``research_agent.daemon`` — spawn lifecycle + main loop (issues #32, #33).

These tests exercise the §5.1 + §6.1 contract from the implementation guide:

* ``spawn_daemon`` launches a detached child via ``Popen(start_new_session=True)``,
  redirects stdout/stderr to ``daemon.{out,err}.log``, and writes the PID to
  ``daemon.pid`` atomically.
* ``is_daemon_alive`` reads the PID file and confirms the process exists. It
  returns ``False`` for missing files, garbage contents, and dead PIDs.
* ``run_daemon`` is the long-running coroutine: loads the job, builds the
  router, runs the orchestrator loop with signal handlers + STOP watcher,
  calls a final synthesis pass, and writes ``status=completed/stopped/failed``
  on the way out.
* The ``python -m research_agent.daemon <id>`` entrypoint cleans up the PID
  file on exit (atexit) regardless of the path taken.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from research_agent import daemon
from research_agent.orchestrator.plan import Plan, Subgoal, TaskSpec
from research_agent.storage import db
from research_agent.storage.jobs import RESUME_REPLAN_FILE, Job
from research_agent.storage.markdown import assemble_report, write_fragment, write_plan

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def job_root(tmp_path: Path) -> Path:
    """Create a minimal ``jobs/<id>/`` skeleton at a known location."""
    jobs_root = tmp_path / "jobs"
    job_id = "2026-05-02-test-target"
    root = jobs_root / job_id
    root.mkdir(parents=True)
    return root


@pytest.fixture
def job_id(job_root: Path) -> str:
    return job_root.name


@pytest.fixture
def jobs_root(job_root: Path) -> Path:
    return job_root.parent


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Migrated SQLite at a known tmp path for the run-daemon tests."""
    path = tmp_path / "index.sqlite"
    db.migrate(path=path).close()
    return path


@pytest.fixture
def jobs_root_for_run(tmp_path: Path) -> Path:
    return tmp_path / "jobs"


@pytest.fixture
def seeded_job(jobs_root_for_run: Path, db_path: Path) -> Job:
    """Job + persisted plan, ready for ``run_daemon`` to drain."""
    job = Job.create(
        {"goal": "Investigate Widget Co", "budget_cap_usd": None},
        jobs_root=jobs_root_for_run,
        db_path=db_path,
    )
    plan = Plan(
        version=1,
        objective="Investigate the target",
        subgoals=[Subgoal(id=1, description="Gather", done=False)],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=3,
    )
    write_plan(job, plan.model_dump())
    return job


def _wait_for_proc_exit(pid: int, timeout: float = 10.0) -> None:
    """Wait for ``pid`` to exit and reap the zombie.

    ``spawn_daemon`` does not double-fork (per §5.1's "simple, portable" choice
    is plain Popen + ``start_new_session``), so the test process remains the
    daemon's parent — the child becomes a zombie on exit until reaped. Use
    :func:`os.waitpid` with ``WNOHANG`` to drain it. In production the user's
    shell or launchd reaps; here, we do.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            wpid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if wpid == pid:
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} did not exit within {timeout}s")


# ---------------------------------------------------------------------------
# spawn_daemon
# ---------------------------------------------------------------------------


def test_spawn_daemon_returns_positive_pid_and_writes_pid_file(
    jobs_root: Path, job_id: str
) -> None:
    pid = daemon.spawn_daemon(job_id, jobs_root=jobs_root)
    try:
        assert isinstance(pid, int)
        assert pid > 0

        pid_file = jobs_root / job_id / "daemon.pid"
        assert pid_file.exists(), "daemon.pid must be written before spawn_daemon returns"
        assert pid_file.read_text(encoding="utf-8").strip() == str(pid)
    finally:
        _wait_for_proc_exit(pid)


def test_spawn_daemon_creates_log_files(jobs_root: Path, job_id: str) -> None:
    pid = daemon.spawn_daemon(job_id, jobs_root=jobs_root)
    try:
        assert (jobs_root / job_id / "daemon.out.log").exists()
        assert (jobs_root / job_id / "daemon.err.log").exists()
    finally:
        _wait_for_proc_exit(pid)


def test_spawn_daemon_atomic_write_leaves_no_tmp(jobs_root: Path, job_id: str) -> None:
    """No half-written ``daemon.pid.tmp`` should be visible after the call."""
    pid = daemon.spawn_daemon(job_id, jobs_root=jobs_root)
    try:
        leftovers = list((jobs_root / job_id).glob("*.tmp"))
        assert leftovers == [], f"unexpected tmp files: {leftovers}"
    finally:
        _wait_for_proc_exit(pid)


def test_spawn_daemon_missing_job_folder_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        daemon.spawn_daemon("2026-05-02-no-such-job", jobs_root=tmp_path / "jobs")


def test_spawn_daemon_appends_to_existing_logs(jobs_root: Path, job_id: str) -> None:
    """A second spawn must not clobber prior log content."""
    out_log = jobs_root / job_id / "daemon.out.log"
    out_log.write_text("prior-line\n", encoding="utf-8")

    pid = daemon.spawn_daemon(job_id, jobs_root=jobs_root)
    try:
        _wait_for_proc_exit(pid)
        assert out_log.read_text(encoding="utf-8").startswith("prior-line\n")
    finally:
        # Already waited for exit above, but keep the cleanup path symmetric.
        pass


# ---------------------------------------------------------------------------
# is_daemon_alive
# ---------------------------------------------------------------------------


def test_is_daemon_alive_false_when_pid_file_missing(jobs_root: Path, job_id: str) -> None:
    assert daemon.is_daemon_alive(job_id, jobs_root=jobs_root) is False


def test_is_daemon_alive_false_for_non_integer_contents(jobs_root: Path, job_id: str) -> None:
    pid_file = jobs_root / job_id / "daemon.pid"
    pid_file.write_text("not-a-number\n", encoding="utf-8")
    assert daemon.is_daemon_alive(job_id, jobs_root=jobs_root) is False


def test_is_daemon_alive_false_for_blank_contents(jobs_root: Path, job_id: str) -> None:
    pid_file = jobs_root / job_id / "daemon.pid"
    pid_file.write_text("", encoding="utf-8")
    assert daemon.is_daemon_alive(job_id, jobs_root=jobs_root) is False


def test_is_daemon_alive_false_for_zero_or_negative(jobs_root: Path, job_id: str) -> None:
    pid_file = jobs_root / job_id / "daemon.pid"
    pid_file.write_text("0\n", encoding="utf-8")
    assert daemon.is_daemon_alive(job_id, jobs_root=jobs_root) is False
    pid_file.write_text("-7\n", encoding="utf-8")
    assert daemon.is_daemon_alive(job_id, jobs_root=jobs_root) is False


def test_is_daemon_alive_true_for_live_process_then_false_after_exit(
    jobs_root: Path, job_id: str
) -> None:
    """Spawn a long-running sleeper, point daemon.pid at it, verify alive→dead."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        pid_file = jobs_root / job_id / "daemon.pid"
        pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")

        # Linux is_daemon_alive additionally peeks /proc/<pid>/cmdline and
        # requires "research_agent.daemon" in it. The sleeper subprocess
        # won't match — skip the live-process assertion in that case and
        # only verify the post-exit branch.
        if not sys.platform.startswith("linux"):
            assert daemon.is_daemon_alive(job_id, jobs_root=jobs_root) is True

        proc.terminate()
        proc.wait(timeout=5)
        _wait_for_proc_exit(proc.pid)

        # PID file still on disk but the process is gone.
        assert daemon.is_daemon_alive(job_id, jobs_root=jobs_root) is False
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_is_daemon_alive_true_after_spawn_daemon(jobs_root: Path, job_id: str) -> None:
    """End-to-end: spawn_daemon writes a PID; alive check sees it."""
    pid = daemon.spawn_daemon(job_id, jobs_root=jobs_root)
    try:
        # Linux is_daemon_alive verifies /proc/<pid>/cmdline contains
        # 'research_agent.daemon'. The child *does* match but races with
        # interpreter startup; on macOS we can assert immediately.
        if not sys.platform.startswith("linux"):
            assert daemon.is_daemon_alive(job_id, jobs_root=jobs_root) is True
    finally:
        _wait_for_proc_exit(pid)

    # After the child has exited, the entrypoint's atexit hook unlinks the
    # PID file → is_daemon_alive returns False.
    assert daemon.is_daemon_alive(job_id, jobs_root=jobs_root) is False


# ---------------------------------------------------------------------------
# Module entrypoint
# ---------------------------------------------------------------------------


def test_module_entrypoint_unknown_job_exits_one_and_clears_pid(
    jobs_root: Path, job_id: str
) -> None:
    """An unknown job id is a fatal startup error: returncode 1, PID file removed."""
    pid_file = jobs_root / job_id / "daemon.pid"
    pid_file.write_text("12345\n", encoding="utf-8")
    assert pid_file.exists()

    proc = subprocess.run(
        [sys.executable, "-m", "research_agent.daemon", job_id],
        cwd=jobs_root.parent,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "RESEARCH_JOBS_ROOT": str(jobs_root)},
    )
    # Job folder lacks job.json → Job.load raises FileNotFoundError → exit 1.
    assert proc.returncode == 1, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    # atexit must still fire and unlink the (test-seeded) PID file.
    assert not pid_file.exists(), "atexit hook must remove daemon.pid even on exit 1"


def test_module_entrypoint_usage_when_missing_args() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "research_agent.daemon"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower()


# ---------------------------------------------------------------------------
# run_daemon: graceful path via STOP flag (no real LLM calls)
# ---------------------------------------------------------------------------


def _patch_run_daemon_for_in_process(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_loop_impl: Any,
) -> dict[str, list[tuple[Any, ...]]]:
    """Stub out the LLM-touching pieces of ``run_daemon`` for in-process tests.

    Returns a dict of recorded calls so each test can assert what fired.
    """
    monkeypatch.setenv("RESEARCH_DAEMON_SKIP_HEALTH_CHECKS", "1")

    calls: dict[str, list[tuple[Any, ...]]] = {"final_synthesis": [], "run_loop": []}

    async def _wrapped_run_loop(job: Job, router: Any, **kwargs: Any) -> Any:
        calls["run_loop"].append((job.id, kwargs))
        return await run_loop_impl(job, router, **kwargs)

    async def _final_synth_stub(job: Job, plan: Plan, *, router: Any) -> None:
        calls["final_synthesis"].append((job.id, plan.version))
        return None

    monkeypatch.setattr(
        "research_agent.orchestrator.loop.run_loop",
        _wrapped_run_loop,
    )
    monkeypatch.setattr(
        "research_agent.orchestrator.synth.final_synthesis",
        _final_synth_stub,
    )
    return calls


@pytest.mark.asyncio
async def test_run_daemon_stops_on_stop_flag(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An externally-dropped STOP flag triggers a clean shutdown with status=stopped."""

    async def _waiting_run_loop(job: Job, router: Any, **kwargs: Any) -> dict[str, Any]:
        # Mimic the real run_loop's STOP poll: spin until the flag exists.
        for _ in range(200):
            if (job.root / "STOP").exists():
                return {"tasks_done": 0, "stopped": True, "completed": False, "cap_hit": False}
            await asyncio.sleep(0.02)
        return {"tasks_done": 0, "stopped": False, "completed": False, "cap_hit": False}

    calls = _patch_run_daemon_for_in_process(monkeypatch, run_loop_impl=_waiting_run_loop)

    async def _drop_stop() -> None:
        await asyncio.sleep(0.1)
        (seeded_job.root / "STOP").write_text("", encoding="utf-8")

    drop_task = asyncio.create_task(_drop_stop())
    exit_code = await daemon.run_daemon(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    await drop_task

    assert exit_code == 0
    assert calls["run_loop"], "run_loop must have been called"
    assert calls["final_synthesis"], "final_synthesis must run after a stop"

    refreshed = Job.load(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert refreshed.status == "stopped"


@pytest.mark.asyncio
async def test_run_daemon_sweeps_stale_orphan_artifacts_on_startup(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_partial = seeded_job.root / "synthesis" / "0004.partial.md"
    old_tmp = seeded_job.root / "critique" / "0002.md.tmp"
    young_partial = seeded_job.root / "plan" / "0003.partial.md"
    old_partial.write_text("", encoding="utf-8")
    old_tmp.write_text("half-written critique", encoding="utf-8")
    young_partial.write_text("still being written", encoding="utf-8")

    now = time.time()
    old_mtime = now - daemon.ORPHAN_ARTIFACT_MAX_AGE_S - 1
    os.utime(old_partial, (old_mtime, old_mtime))
    os.utime(old_tmp, (old_mtime, old_mtime))
    os.utime(young_partial, (now, now))

    async def _instant_run_loop(job: Job, router: Any, **kwargs: Any) -> dict[str, Any]:
        return {"tasks_done": 0, "stopped": True, "completed": False, "cap_hit": False}

    _patch_run_daemon_for_in_process(monkeypatch, run_loop_impl=_instant_run_loop)

    exit_code = await daemon.run_daemon(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )

    assert exit_code == 0
    assert not old_partial.exists()
    assert not old_tmp.exists()
    assert young_partial.exists()

    conn = db.connect(seeded_job.db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM events"
            " WHERE job_id = ? AND kind = 'orphan_artifact_cleaned'"
            " ORDER BY id ASC",
            (seeded_job.id,),
        ).fetchall()
    finally:
        conn.close()

    cleaned_paths = {json.loads(row["payload_json"])["path"] for row in rows}
    assert cleaned_paths == {"synthesis/0004.partial.md", "critique/0002.md.tmp"}
    for row in rows:
        payload = json.loads(row["payload_json"])
        assert payload["age_seconds"] >= daemon.ORPHAN_ARTIFACT_MAX_AGE_S


@pytest.mark.asyncio
async def test_run_daemon_sigterm_routes_through_same_graceful_path(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM must flip the in-memory event AND touch STOP so the loop bails."""

    async def _waiting_run_loop(job: Job, router: Any, **kwargs: Any) -> dict[str, Any]:
        for _ in range(200):
            if (job.root / "STOP").exists():
                return {"tasks_done": 0, "stopped": True, "completed": False, "cap_hit": False}
            await asyncio.sleep(0.02)
        return {"tasks_done": 0, "stopped": False, "completed": False, "cap_hit": False}

    calls = _patch_run_daemon_for_in_process(monkeypatch, run_loop_impl=_waiting_run_loop)

    async def _send_sigterm() -> None:
        await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)

    sig_task = asyncio.create_task(_send_sigterm())
    exit_code = await daemon.run_daemon(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    await sig_task

    assert exit_code == 0
    assert (seeded_job.root / "STOP").exists(), "signal handler must touch STOP"
    assert calls["final_synthesis"], "final_synthesis must run after SIGTERM"

    refreshed = Job.load(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert refreshed.status == "stopped"


@pytest.mark.asyncio
async def test_run_daemon_completed_status_when_plan_is_complete(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When run_loop returns and plan.is_complete(), status flips to completed."""

    async def _instant_run_loop(job: Job, router: Any, **kwargs: Any) -> dict[str, Any]:
        # Mark every subgoal done so the post-loop ``plan.is_complete()`` is True.
        plan_dump = {
            "version": 2,
            "objective": "Investigate the target",
            "subgoals": [{"id": 1, "description": "Gather", "done": True}],
            "task_template": [{"kind": "web_search"}],
            "expected_iterations": 3,
        }
        write_plan(job, plan_dump)
        return {"tasks_done": 1, "stopped": False, "completed": True, "cap_hit": False}

    _patch_run_daemon_for_in_process(monkeypatch, run_loop_impl=_instant_run_loop)

    exit_code = await daemon.run_daemon(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert exit_code == 0

    refreshed = Job.load(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert refreshed.status == "completed"


@pytest.mark.asyncio
async def test_run_daemon_passes_time_cap_and_marks_time_cap_completion(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persisted ``time_cap_hours`` is forwarded to run_loop and classified distinctly."""
    intake = dict(seeded_job.intake)
    intake["time_cap_hours"] = 1
    conn = db.connect(seeded_job.db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE jobs SET intake_json = ?, time_cap_hours = ? WHERE id = ?",
                (json.dumps(intake, sort_keys=True), 1, seeded_job.id),
            )
    finally:
        conn.close()

    async def _time_capped_run_loop(job: Job, router: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "tasks_done": 1,
            "stopped": False,
            "completed": False,
            "cap_hit": False,
            "time_cap_hit": True,
            "completion_reason": "time_cap",
        }

    calls = _patch_run_daemon_for_in_process(monkeypatch, run_loop_impl=_time_capped_run_loop)

    exit_code = await daemon.run_daemon(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert exit_code == 0
    assert calls["run_loop"][0][1]["time_cap_hours"] == 1.0

    refreshed = Job.load(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert refreshed.status == "completed"
    assert refreshed.completion_reason == "time_cap"


@pytest.mark.asyncio
async def test_run_daemon_persists_exhausted_completion_reason(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loop-level exhausted signal wins over goal_complete classification."""

    async def _exhausted_run_loop(job: Job, router: Any, **kwargs: Any) -> dict[str, Any]:
        plan_dump = {
            "version": 2,
            "objective": "Investigate the target",
            "subgoals": [{"id": 1, "description": "Gather", "done": True}],
            "task_template": [],
            "expected_iterations": 3,
        }
        write_plan(job, plan_dump)
        return {
            "tasks_done": 1,
            "stopped": False,
            "completed": True,
            "cap_hit": False,
            "time_cap_hit": False,
            "completion_reason": "exhausted",
        }

    _patch_run_daemon_for_in_process(monkeypatch, run_loop_impl=_exhausted_run_loop)

    exit_code = await daemon.run_daemon(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert exit_code == 0

    refreshed = Job.load(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert refreshed.status == "completed"
    assert refreshed.completion_reason == "exhausted"


@pytest.mark.asyncio
async def test_run_daemon_persists_confirmed_gap_completion_reason(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Complete-with-gaps runs must persist their terminal reason."""

    async def _confirmed_gap_run_loop(job: Job, router: Any, **kwargs: Any) -> dict[str, Any]:
        plan_dump = {
            "version": 2,
            "objective": "Investigate the target",
            "subgoals": [{"id": 1, "description": "Gather", "done": True}],
            "task_template": [],
            "expected_iterations": 3,
        }
        write_plan(job, plan_dump)
        return {
            "tasks_done": 1,
            "stopped": False,
            "completed": False,
            "cap_hit": False,
            "time_cap_hit": False,
            "completion_reason": "confirmed_gap",
        }

    _patch_run_daemon_for_in_process(monkeypatch, run_loop_impl=_confirmed_gap_run_loop)

    exit_code = await daemon.run_daemon(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert exit_code == 0

    refreshed = Job.load(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert refreshed.status == "completed"
    assert refreshed.completion_reason == "confirmed_gap"
    meta = json.loads((seeded_job.root / "job.json").read_text(encoding="utf-8"))
    assert meta["completion_reason"] == "confirmed_gap"


@pytest.mark.asyncio
async def test_run_daemon_resume_replan_runs_before_loop(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resume sidecar triggers tactical_replan before run_loop pulls work."""
    monkeypatch.setenv("RESEARCH_DAEMON_SKIP_HEALTH_CHECKS", "1")
    (seeded_job.root / RESUME_REPLAN_FILE).write_text(
        json.dumps({"note": "user added FOIA response"}),
        encoding="utf-8",
    )
    sequence: list[str] = []
    captured: dict[str, Any] = {}

    async def _resume_replan(
        job: Job,
        plan: Plan,
        recent_results: list[dict[str, Any]],
        *,
        router: Any,
        findings: list[dict[str, Any]] | None = None,
        synthesis_md: str | None = None,
        follow_up_questions: list[str] | None = None,
        inconclusive_subgoals: list[dict[str, Any]] | None = None,
        user_note: str | None = None,
    ) -> Plan:
        sequence.append("replan")
        captured["user_note"] = user_note
        new = plan.model_copy(update={"version": plan.version + 1})
        write_plan(job, new.model_dump())
        return new

    async def _run_loop(job: Job, router: Any, **kwargs: Any) -> dict[str, Any]:
        sequence.append("run_loop")
        plan_dump = {
            "version": 3,
            "objective": "Investigate the target",
            "subgoals": [{"id": 1, "description": "Gather", "done": True}],
            "task_template": [],
            "expected_iterations": 3,
        }
        write_plan(job, plan_dump)
        return {"tasks_done": 0, "stopped": False, "completed": True, "cap_hit": False}

    async def _final_synth_stub(job: Job, plan: Plan, *, router: Any) -> None:
        return None

    monkeypatch.setattr("research_agent.orchestrator.plan.tactical_replan", _resume_replan)
    monkeypatch.setattr("research_agent.orchestrator.loop.run_loop", _run_loop)
    monkeypatch.setattr(
        "research_agent.orchestrator.synth.final_synthesis",
        _final_synth_stub,
    )

    exit_code = await daemon.run_daemon(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )

    assert exit_code == 0
    assert sequence == ["replan", "run_loop"]
    assert captured["user_note"] == "user added FOIA response"
    assert not (seeded_job.root / RESUME_REPLAN_FILE).exists()


@pytest.mark.asyncio
async def test_run_daemon_without_resume_replan_skips_tactical_replan(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normal resume behavior still goes straight into run_loop."""
    monkeypatch.setenv("RESEARCH_DAEMON_SKIP_HEALTH_CHECKS", "1")
    calls = {"replan": 0}

    async def _resume_replan(*args: Any, **kwargs: Any) -> Plan:
        calls["replan"] += 1
        raise AssertionError("tactical_replan should not run without sidecar")

    async def _run_loop(job: Job, router: Any, **kwargs: Any) -> dict[str, Any]:
        plan_dump = {
            "version": 2,
            "objective": "Investigate the target",
            "subgoals": [{"id": 1, "description": "Gather", "done": True}],
            "task_template": [],
            "expected_iterations": 3,
        }
        write_plan(job, plan_dump)
        return {"tasks_done": 0, "stopped": False, "completed": True, "cap_hit": False}

    async def _final_synth_stub(job: Job, plan: Plan, *, router: Any) -> None:
        return None

    monkeypatch.setattr("research_agent.orchestrator.plan.tactical_replan", _resume_replan)
    monkeypatch.setattr("research_agent.orchestrator.loop.run_loop", _run_loop)
    monkeypatch.setattr(
        "research_agent.orchestrator.synth.final_synthesis",
        _final_synth_stub,
    )

    exit_code = await daemon.run_daemon(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )

    assert exit_code == 0
    assert calls["replan"] == 0


@pytest.mark.asyncio
async def test_run_daemon_fragment_resume_reassembles_latest_fragments(
    seeded_job: Job,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARCH_DAEMON_SKIP_HEALTH_CHECKS", "1")
    monkeypatch.setenv("RESEARCH_FRAGMENT_SYNTH", "1")
    write_fragment(
        seeded_job,
        "timeline",
        "## Timeline\n\n- Persisted before restart.",
        source_finding_ids=[],
    )

    class _Router:
        tiers = {"frontier": {"provider": "openrouter", "model": "fragment-model"}}
        budget = None

    async def _run_loop(job: Job, router: Any, **kwargs: Any) -> dict[str, Any]:
        plan_dump = {
            "version": 2,
            "objective": "Investigation complete",
            "subgoals": [{"id": 1, "description": "Gather", "done": True}],
            "task_template": [],
            "expected_iterations": 3,
        }
        write_plan(job, plan_dump)
        return {"tasks_done": 0, "stopped": False, "completed": True, "cap_hit": False}

    monkeypatch.setattr(daemon, "_build_router", lambda _job: _Router())
    monkeypatch.setattr("research_agent.orchestrator.loop.run_loop", _run_loop)

    exit_code = await daemon.run_daemon(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )

    assert exit_code == 0
    assert (seeded_job.root / "report.md").read_text(encoding="utf-8") == assemble_report(
        seeded_job
    )
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT model FROM syntheses WHERE job_id = ? ORDER BY version DESC LIMIT 1",
            (seeded_job.id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["model"] == "fragment_assembly"

    events = [
        json.loads(line)
        for line in (seeded_job.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    mode_events = [event for event in events if event["kind"] == "synthesis_mode"]
    assert mode_events[-1]["payload"]["mode"] == "fragments"


@pytest.mark.asyncio
async def test_run_daemon_resume_honors_persisted_fragments_intake_without_env(
    jobs_root_for_run: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume from a clean shell must keep fragment mode (regression).

    ``research resume`` spawns the daemon from a shell that no longer carries
    RESEARCH_FRAGMENT_SYNTH. The persisted ``intake.fragments`` flag is the
    source of truth on restart, so the daemon must reassemble from fragments
    rather than silently reverting to legacy whole-report synthesis.
    """
    monkeypatch.setenv("RESEARCH_DAEMON_SKIP_HEALTH_CHECKS", "1")
    monkeypatch.delenv("RESEARCH_FRAGMENT_SYNTH", raising=False)
    job = Job.create(
        {"goal": "Investigate Widget Co", "budget_cap_usd": None, "fragments": True},
        jobs_root=jobs_root_for_run,
        db_path=db_path,
    )
    write_plan(
        job,
        Plan(
            version=1,
            objective="Investigate the target",
            subgoals=[Subgoal(id=1, description="Gather", done=False)],
            task_template=[TaskSpec(kind="web_search")],
            expected_iterations=3,
        ).model_dump(),
    )
    write_fragment(
        job,
        "timeline",
        "## Timeline\n\n- Persisted before restart.",
        source_finding_ids=[],
    )

    class _Router:
        tiers = {"frontier": {"provider": "openrouter", "model": "fragment-model"}}
        budget = None

    async def _run_loop(job: Job, router: Any, **kwargs: Any) -> dict[str, Any]:
        write_plan(
            job,
            {
                "version": 2,
                "objective": "Investigation complete",
                "subgoals": [{"id": 1, "description": "Gather", "done": True}],
                "task_template": [],
                "expected_iterations": 3,
            },
        )
        return {"tasks_done": 0, "stopped": False, "completed": True, "cap_hit": False}

    monkeypatch.setattr(daemon, "_build_router", lambda _job: _Router())
    monkeypatch.setattr("research_agent.orchestrator.loop.run_loop", _run_loop)

    try:
        # ``run_daemon`` mutates ``os.environ`` directly (mirroring the
        # spawned-daemon contract); monkeypatch will not revert that, so the
        # finally below prevents leakage into sibling daemon tests.
        exit_code = await daemon.run_daemon(
            job.id,
            jobs_root=job.root.parent,
            db_path=job.db_path,
        )

        assert exit_code == 0
        assert os.environ.get("RESEARCH_FRAGMENT_SYNTH") == "1"
        conn = db.connect(db_path)
        try:
            row = conn.execute(
                "SELECT model FROM syntheses WHERE job_id = ? ORDER BY version DESC LIMIT 1",
                (job.id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["model"] == "fragment_assembly"
        events = [
            json.loads(line)
            for line in (job.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        mode_events = [event for event in events if event["kind"] == "synthesis_mode"]
        assert mode_events[-1]["payload"]["mode"] == "fragments"
    finally:
        os.environ.pop("RESEARCH_FRAGMENT_SYNTH", None)


@pytest.mark.asyncio
async def test_run_daemon_classifies_goal_complete_when_final_synthesis_closes_subgoals(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If post-loop final_synthesis closes subgoals, classify as goal_complete (issue #160).

    Reproducer: ``run_loop`` returns with the plan still open, then the post-loop
    ``final_synthesis`` pass fires a synthesizer that calls ``update_subgoal_done``
    and writes a new plan version with ``done=True`` for every subgoal. The
    daemon must re-load the plan AFTER ``final_synthesis`` so ``plan.is_complete()``
    sees the closures; otherwise the stale plan falls through to the ``user_stopped``
    branch even though the run finished cleanly.
    """
    monkeypatch.setenv("RESEARCH_DAEMON_SKIP_HEALTH_CHECKS", "1")

    async def _open_run_loop(job: Job, router: Any, **kwargs: Any) -> dict[str, Any]:
        # Loop returns without flipping any subgoal — simulates the case where
        # closure happens later, in the post-loop synthesis pass.
        return {"tasks_done": 1, "stopped": False, "completed": False, "cap_hit": False}

    async def _closing_final_synth(job: Job, plan: Plan, *, router: Any) -> None:
        # Mimic synth.update_subgoal_done writing a NEW plan version with
        # every subgoal closed.
        plan_dump = {
            "version": plan.version + 1,
            "objective": plan.objective,
            "subgoals": [{"id": 1, "description": "Gather", "done": True}],
            "task_template": [{"kind": "web_search"}],
            "expected_iterations": 3,
        }
        write_plan(job, plan_dump)
        return None

    monkeypatch.setattr(
        "research_agent.orchestrator.loop.run_loop",
        _open_run_loop,
    )
    monkeypatch.setattr(
        "research_agent.orchestrator.synth.final_synthesis",
        _closing_final_synth,
    )

    exit_code = await daemon.run_daemon(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert exit_code == 0

    refreshed = Job.load(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert refreshed.status == "completed"
    assert refreshed.completion_reason == "goal_complete"


@pytest.mark.asyncio
async def test_run_daemon_handles_budget_exceeded_mid_loop(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``BudgetExceeded`` raised mid-loop turns into status=completed, reason=budget_cap."""
    from research_agent.llm.budgets import BudgetExceeded
    from research_agent.orchestrator import synth as _synth

    async def _capping_run_loop(job: Job, router: Any, **kwargs: Any) -> dict[str, Any]:
        raise BudgetExceeded(job.id, spent=10.0, cap=5.0)

    captured: dict[str, Any] = {}

    async def _post_cap_synth(job: Job, plan: Plan, *, router: Any):
        captured["called"] = True
        captured["plan_version"] = plan.version
        # Simulate the helper writing a report.
        (job.root / "report.md").write_text("# Report (post-cap)\n\nstub\n", encoding="utf-8")
        return None

    monkeypatch.setenv("RESEARCH_DAEMON_SKIP_HEALTH_CHECKS", "1")
    monkeypatch.setattr(
        "research_agent.orchestrator.loop.run_loop",
        _capping_run_loop,
    )
    monkeypatch.setattr(_synth, "final_synthesis_after_cap", _post_cap_synth)

    exit_code = await daemon.run_daemon(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert exit_code == 0
    assert captured.get("called"), "final_synthesis_after_cap must run after a cap"

    refreshed = Job.load(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert refreshed.status == "completed"
    assert refreshed.completion_reason == "budget_cap"
    assert (seeded_job.root / "report.md").exists()

    # A WARN event with stage=run_loop_budget_cap was emitted.
    conn = db.connect(seeded_job.db_path)
    try:
        rows = conn.execute(
            "SELECT level, kind, payload_json FROM events WHERE job_id = ? AND kind = 'warning'",
            (seeded_job.id,),
        ).fetchall()
    finally:
        conn.close()
    assert any('"stage": "run_loop_budget_cap"' in r["payload_json"] for r in rows), (
        "expected a run_loop_budget_cap warning event"
    )


@pytest.mark.asyncio
async def test_run_daemon_uncaught_exception_marks_failed(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bubbling exceptions from run_loop produce status=failed + an error event."""

    async def _crashing_run_loop(job: Job, router: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("planner explosion")

    _patch_run_daemon_for_in_process(monkeypatch, run_loop_impl=_crashing_run_loop)

    exit_code = await daemon.run_daemon(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert exit_code == 1

    refreshed = Job.load(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert refreshed.status == "failed"

    conn = db.connect(seeded_job.db_path)
    try:
        rows = conn.execute(
            "SELECT level, kind, payload_json FROM events WHERE job_id = ? AND kind = 'error'",
            (seeded_job.id,),
        ).fetchall()
    finally:
        conn.close()
    assert rows, "an error event must be emitted on uncaught exception"
    payload = rows[-1]["payload_json"]
    assert "planner explosion" in payload
    assert "Traceback" in payload, "traceback must be captured in the error payload"


@pytest.mark.asyncio
async def test_run_daemon_health_check_failure_exits_one(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable LM Studio at startup → status=failed, exit 1."""
    monkeypatch.delenv("RESEARCH_DAEMON_SKIP_HEALTH_CHECKS", raising=False)

    async def _fast_fail_lm(router: Any, *, deadline_s: float = 60.0) -> None:
        raise RuntimeError("lm_studio not reachable: connection refused")

    async def _ok_openrouter(router: Any, *, deadline_s: float = 60.0) -> None:
        return None

    monkeypatch.setattr(daemon, "ensure_lm_studio_alive", _fast_fail_lm)
    monkeypatch.setattr(daemon, "ensure_openrouter_reachable", _ok_openrouter)

    exit_code = await daemon.run_daemon(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert exit_code == 1

    refreshed = Job.load(
        seeded_job.id,
        jobs_root=seeded_job.root.parent,
        db_path=seeded_job.db_path,
    )
    assert refreshed.status == "failed"


# ---------------------------------------------------------------------------
# Health-check helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_lm_studio_alive_succeeds_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One successful probe must short-circuit the retry loop."""

    class _OkResp:
        def raise_for_status(self) -> None:
            return None

    class _OkClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _OkClient:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> _OkResp:
            return _OkResp()

    monkeypatch.setattr(daemon.httpx, "AsyncClient", _OkClient)

    class _StubRouter:
        job = None

    await daemon.ensure_lm_studio_alive(_StubRouter(), deadline_s=1.0)


@pytest.mark.asyncio
async def test_ensure_lm_studio_alive_raises_on_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent failures must raise RuntimeError once the deadline expires."""

    class _BadClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _BadClient:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> Any:
            raise ConnectionError("nope")

    monkeypatch.setattr(daemon.httpx, "AsyncClient", _BadClient)
    # Compress the backoff so the test finishes fast.
    monkeypatch.setattr(daemon, "_HEALTH_INITIAL_DELAY_S", 0.01)
    monkeypatch.setattr(daemon, "_HEALTH_MAX_DELAY_S", 0.02)

    class _StubRouter:
        job = None

    with pytest.raises(RuntimeError, match="lm_studio not reachable"):
        await daemon.ensure_lm_studio_alive(_StubRouter(), deadline_s=0.05)


@pytest.mark.asyncio
async def test_ensure_openrouter_reachable_requires_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    class _StubRouter:
        job = None

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        await daemon.ensure_openrouter_reachable(_StubRouter(), deadline_s=1.0)


# ---------------------------------------------------------------------------
# Foreground progress bar (issue #41)
# ---------------------------------------------------------------------------


class _FakeProgress:
    """In-memory stand-in for ``rich.progress.Progress`` used by daemon tests."""

    def __init__(self) -> None:
        self.tasks: dict[int, dict[str, Any]] = {}
        self._next_id = 1
        self.updates: list[tuple[int, dict[str, Any]]] = []
        self.entered = False
        self.exited = False

    def __enter__(self) -> _FakeProgress:
        self.entered = True
        return self

    def __exit__(self, *exc: Any) -> None:
        self.exited = True

    def add_task(self, name: str, *, total: int | None = None, completed: int = 0) -> int:
        tid = self._next_id
        self._next_id += 1
        self.tasks[tid] = {"name": name, "total": total, "completed": completed}
        return tid

    def update(self, task_id: int, **kwargs: Any) -> None:
        self.tasks[task_id].update(kwargs)
        self.updates.append((task_id, dict(kwargs)))


class _TtyStream:
    """Stand-in stdout that reports ``isatty() == True``."""

    def isatty(self) -> bool:
        return True

    def write(self, _: str) -> int:
        return 0

    def flush(self) -> None:
        return None


class _NonTtyStream:
    def isatty(self) -> bool:
        return False


def test_should_show_foreground_progress_respects_tty_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RESEARCH_DAEMON_PROGRESS", raising=False)
    assert daemon._should_show_foreground_progress(_TtyStream()) is True
    assert daemon._should_show_foreground_progress(_NonTtyStream()) is False

    monkeypatch.setenv("RESEARCH_DAEMON_PROGRESS", "0")
    assert daemon._should_show_foreground_progress(_TtyStream()) is False


@pytest.mark.asyncio
async def test_foreground_progress_dormant_for_non_tty(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-TTY stdout (the spawn_daemon path) → no Progress is ever created."""
    monkeypatch.delenv("RESEARCH_DAEMON_PROGRESS", raising=False)
    factory_calls = {"n": 0}

    def _factory() -> _FakeProgress:
        factory_calls["n"] += 1
        return _FakeProgress()

    should_stop = asyncio.Event()
    should_stop.set()  # ensure the task exits even if it somehow enters the loop

    await daemon._foreground_progress_task(
        seeded_job,
        should_stop,
        stream=_NonTtyStream(),
        progress_factory=_factory,
    )
    assert factory_calls["n"] == 0


@pytest.mark.asyncio
async def test_foreground_progress_dormant_when_env_zero(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator opt-out via RESEARCH_DAEMON_PROGRESS=0 keeps the bar quiet."""
    monkeypatch.setenv("RESEARCH_DAEMON_PROGRESS", "0")
    factory_calls = {"n": 0}

    def _factory() -> _FakeProgress:
        factory_calls["n"] += 1
        return _FakeProgress()

    should_stop = asyncio.Event()
    should_stop.set()

    await daemon._foreground_progress_task(
        seeded_job,
        should_stop,
        stream=_TtyStream(),
        progress_factory=_factory,
    )
    assert factory_calls["n"] == 0


@pytest.mark.asyncio
async def test_foreground_progress_updates_as_tasks_transition(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When stdout is a TTY, the bar tracks done/total of the latest plan version."""
    monkeypatch.delenv("RESEARCH_DAEMON_PROGRESS", raising=False)
    fake = _FakeProgress()

    # Seed two pending and one done task on the seeded plan version (1).
    conn = db.connect(seeded_job.db_path)
    try:
        with conn:
            now = int(time.time())
            conn.execute(
                "INSERT INTO tasks"
                " (job_id, plan_version, kind, payload_json, status,"
                " started_at, finished_at, retry_count)"
                " VALUES (?, 1, 'web_search', '{}', 'done', ?, ?, 0)",
                (seeded_job.id, now - 30, now - 10),
            )
            conn.execute(
                "INSERT INTO tasks"
                " (job_id, plan_version, kind, payload_json, status, retry_count)"
                " VALUES (?, 1, 'web_search', '{}', 'pending', 0)",
                (seeded_job.id,),
            )
            conn.execute(
                "INSERT INTO tasks"
                " (job_id, plan_version, kind, payload_json, status, retry_count)"
                " VALUES (?, 1, 'web_search', '{}', 'pending', 0)",
                (seeded_job.id,),
            )
    finally:
        conn.close()

    should_stop = asyncio.Event()

    async def _flip_after_first_tick() -> None:
        # Wait for the initial tick + a follow-up; the wait_for(timeout=)
        # branch lets the task settle into a steady state.
        await asyncio.sleep(0.05)
        # Mark another task done.
        conn = db.connect(seeded_job.db_path)
        try:
            with conn:
                row = conn.execute(
                    "SELECT id FROM tasks WHERE job_id = ? AND status = 'pending'"
                    " ORDER BY id LIMIT 1",
                    (seeded_job.id,),
                ).fetchone()
                if row is not None:
                    conn.execute(
                        "UPDATE tasks SET status = 'done',"
                        " started_at = ?, finished_at = ? WHERE id = ?",
                        (int(time.time()) - 5, int(time.time()), int(row["id"])),
                    )
        finally:
            conn.close()
        await asyncio.sleep(0.05)
        should_stop.set()

    flip = asyncio.create_task(_flip_after_first_tick())
    await daemon._foreground_progress_task(
        seeded_job,
        should_stop,
        stream=_TtyStream(),
        progress_factory=lambda: fake,
        interval_s=0.02,
    )
    await flip

    assert fake.entered and fake.exited
    assert len(fake.tasks) == 1
    # Initial state: 1 done out of 3.
    first_update = fake.updates[0]
    assert first_update[1]["total"] == 3
    assert first_update[1]["completed"] == 1
    # By the time should_stop fires, the bar saw the second done transition.
    assert any(u[1]["completed"] >= 2 for u in fake.updates)
    final = fake.updates[-1]
    assert final[1]["completed"] == 2
    assert final[1]["total"] == 3


@pytest.mark.asyncio
async def test_inbox_watcher_indexes_moves_and_requests_replan(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = seeded_job.root / "inbox"
    inbox.mkdir(exist_ok=True)
    doc = inbox / "foia-response.md"
    doc.write_text("# FOIA\n\nContract award file from the clerk.\n", encoding="utf-8")
    calls: list[Path] = []

    def _fake_index(
        path: Path, job: Job, *, per_page: bool = False
    ) -> dict[str, int]:
        calls.append(path)
        return {
            "files_indexed": 1,
            "files_skipped": 0,
            "chunks_indexed": 1,
            "chunks_skipped": 0,
            "embed_dim": 1024,
        }

    monkeypatch.setattr("research_agent.tools.local_corpus.index", _fake_index)
    should_stop = asyncio.Event()
    task = asyncio.create_task(
        daemon._inbox_watcher(seeded_job, should_stop, interval_s=0.02)
    )
    try:
        for _ in range(100):
            if (seeded_job.root / "INBOX_REPLAN.json").exists():
                should_stop.set()
                break
            await asyncio.sleep(0.02)
        await asyncio.wait_for(task, timeout=1.0)
    finally:
        should_stop.set()
        if not task.done():
            task.cancel()

    assert calls == [doc]
    assert not doc.exists()
    processed = list((inbox / "processed").glob("*-foia-response.md"))
    assert len(processed) == 1

    sidecar = json.loads((seeded_job.root / "INBOX_REPLAN.json").read_text(encoding="utf-8"))
    assert sidecar["trigger"] == "inbox"
    assert sidecar["filename"] == "foia-response.md"
    assert "identify NEW angles" in sidecar["note"]

    conn = db.connect(seeded_job.db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM events"
            " WHERE job_id = ? AND kind = 'corpus_doc_added'",
            (seeded_job.id,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["filename"] == "foia-response.md"
    assert payload["files_indexed"] == 1
    assert payload["chunks_indexed"] == 1


@pytest.mark.asyncio
async def test_inbox_watcher_respects_corpus_dossier_intake_flag(
    seeded_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """intake.corpus_dossier=True routes inbox indexing through per_page=True."""
    seeded_job.intake = {**(seeded_job.intake or {}), "corpus_dossier": True}
    (seeded_job.root / "intake.json").write_text(
        json.dumps(seeded_job.intake), encoding="utf-8"
    )

    inbox = seeded_job.root / "inbox"
    inbox.mkdir(exist_ok=True)
    doc = inbox / "dossier-evidence.md"
    doc.write_text("# Dossier evidence\n\nbody body body\n", encoding="utf-8")
    per_page_calls: list[bool] = []

    def _fake_index(
        path: Path, job: Job, *, per_page: bool = False
    ) -> dict[str, int]:
        per_page_calls.append(per_page)
        return {
            "files_indexed": 1,
            "files_skipped": 0,
            "chunks_indexed": 1,
            "chunks_skipped": 0,
            "pages_indexed": 1,
            "pages_skipped": 0,
            "per_page": per_page,
            "embed_dim": 1024,
        }

    monkeypatch.setattr("research_agent.tools.local_corpus.index", _fake_index)
    should_stop = asyncio.Event()
    task = asyncio.create_task(
        daemon._inbox_watcher(seeded_job, should_stop, interval_s=0.02)
    )
    try:
        for _ in range(100):
            if (seeded_job.root / "INBOX_REPLAN.json").exists():
                should_stop.set()
                break
            await asyncio.sleep(0.02)
        await asyncio.wait_for(task, timeout=1.0)
    finally:
        should_stop.set()
        if not task.done():
            task.cancel()

    assert per_page_calls == [True]


# ---------------------------------------------------------------------------
# _cancel_with_timeout — bounds daemon teardown so a hung watcher can't block exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_with_timeout_returns_promptly_when_task_obeys_cancel() -> None:
    async def _well_behaved() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return

    task = asyncio.create_task(_well_behaved())
    start = time.monotonic()
    await daemon._cancel_with_timeout(task, "well_behaved", timeout=2.0)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"cancel should return immediately, took {elapsed:.2f}s"
    assert task.done()


@pytest.mark.asyncio
async def test_cancel_with_timeout_abandons_task_that_ignores_cancel() -> None:
    """Regression for the post-cap-hit hang: if cancel() can't reach the
    awaited operation, the daemon must NOT block forever waiting. The helper
    returns within the timeout and logs a warning so process exit can reap
    the task. Simulate by stubbing ``Task.cancel`` to a no-op so the
    underlying sleep keeps running past the helper's timeout window."""

    async def _slow() -> None:
        await asyncio.sleep(60)

    task = asyncio.create_task(_slow())
    # Stub: cancel() becomes a no-op so the sleep keeps running. This models
    # the real-world failure mode where cancellation can't propagate (e.g.
    # synchronous I/O block) and the helper's timeout is the only guard.
    task.cancel = lambda *a, **kw: False  # type: ignore[method-assign]

    start = time.monotonic()
    await daemon._cancel_with_timeout(task, "ignores_cancel", timeout=0.3)
    elapsed = time.monotonic() - start
    assert 0.25 <= elapsed < 1.5, f"timeout should fire ~0.3s; took {elapsed:.2f}s"
    # Cleanup the orphan via the real cancel path (bypass our stub).
    type(task).cancel(task)
    try:
        await asyncio.wait_for(task, timeout=0.5)
    except (asyncio.CancelledError, TimeoutError):
        pass


@pytest.mark.asyncio
async def test_cancel_with_timeout_no_op_for_already_done_task() -> None:
    async def _quick() -> None:
        return

    task = asyncio.create_task(_quick())
    await asyncio.sleep(0.01)
    assert task.done()
    # Should be a no-op — neither raise nor block.
    await daemon._cancel_with_timeout(task, "already_done", timeout=2.0)
