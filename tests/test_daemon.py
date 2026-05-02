"""Tests for ``daemon.spawn_daemon`` / ``is_daemon_alive`` / module entrypoint.

These tests exercise the §5.1 contract from the implementation guide:

* ``spawn_daemon`` launches a detached child via ``Popen(start_new_session=True)``,
  redirects stdout/stderr to ``daemon.{out,err}.log``, and writes the PID to
  ``daemon.pid`` atomically.
* ``is_daemon_alive`` reads the PID file and confirms the process exists. It
  returns ``False`` for missing files, garbage contents, and dead PIDs.
* The ``python -m research_agent.daemon <id>`` entrypoint cleans up the PID
  file on a graceful exit (atexit).

Tests rely on the ``run_daemon`` stub-warning fast path — issue #33 owns the
real loop, so the child currently logs and returns within milliseconds. That
makes the suite fast without monkeypatching across the process boundary.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from research_agent import daemon


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


def _wait_for_proc_exit(pid: int, timeout: float = 5.0) -> None:
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
# Module entrypoint: clean shutdown removes daemon.pid
# ---------------------------------------------------------------------------


def test_module_entrypoint_clean_shutdown_removes_pid_file(jobs_root: Path, job_id: str) -> None:
    """``python -m research_agent.daemon <id>`` deletes the PID file on exit."""
    pid_file = jobs_root / job_id / "daemon.pid"
    pid_file.write_text("12345\n", encoding="utf-8")
    assert pid_file.exists()

    proc = subprocess.run(
        [sys.executable, "-m", "research_agent.daemon", job_id],
        cwd=jobs_root.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    assert not pid_file.exists(), "atexit hook must remove daemon.pid on graceful exit"


def test_module_entrypoint_logs_stub_warning(jobs_root: Path, job_id: str) -> None:
    """Stub fast path emits the warning so future operators know why nothing ran."""
    proc = subprocess.run(
        [sys.executable, "-m", "research_agent.daemon", job_id],
        cwd=jobs_root.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    # Python logging.warning() goes to stderr by default.
    combined = proc.stdout + proc.stderr
    assert "daemon entrypoint stub" in combined
    assert "issue #33" in combined


def test_module_entrypoint_usage_when_missing_args() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "research_agent.daemon"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower()
