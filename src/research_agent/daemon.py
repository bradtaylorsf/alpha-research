"""Long-running research process — drives the agent loop per job (impl guide §4, §5.1, §6).

Issue #32 ships the spawn/PID-file lifecycle: ``spawn_daemon`` forks a
detached child via :func:`subprocess.Popen` with ``start_new_session=True``
so it survives terminal exit, and ``is_daemon_alive`` checks the process
behind ``jobs/<id>/daemon.pid``. The actual research loop wired into
``run_daemon`` is owned by issue #33; this module ships a thin stub that
delegates to ``orchestrator.loop.run_daemon`` once that lands.

The PID file is written by the *parent* (spawn_daemon) and removed by the
*child* on clean shutdown (atexit). Both SIGTERM and SIGINT route through
the same path: raise :class:`KeyboardInterrupt`, the asyncio loop unwinds,
atexit fires, ``daemon.pid`` is deleted. Per §5.2 the daemon is the
graceful path — there is no separate "dirty" exit that leaves the file
behind on a normal signal.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import FrameType
from typing import Any

from research_agent.storage.jobs import DEFAULT_JOBS_ROOT

logger = logging.getLogger(__name__)


def _atomic_write_text(path: Path, data: str) -> None:
    """Atomic ``*.tmp`` + :func:`os.replace` write.

    Mirrors :func:`research_agent.storage.jobs._atomic_write_text` rather
    than importing it — keeping daemon.py free of a back-edge into
    storage.jobs avoids a circular import at the module level (jobs.py
    already references daemon.pid by path).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def spawn_daemon(job_id: str, *, jobs_root: Path | str = DEFAULT_JOBS_ROOT) -> int:
    """Fork off the daemon for ``job_id``; return the child PID.

    Per §5.1: a plain :func:`subprocess.Popen` with ``start_new_session=True``
    is the simple, portable choice. The new session detaches the child from
    the controlling terminal so the daemon survives shell exit (HUP).
    Stdout/stderr are appended to ``jobs/<id>/daemon.{out,err}.log`` so the
    user can ``tail -f`` them, and stdin is wired to ``/dev/null`` so the
    child cannot block on stdin reads.

    The PID is written to ``jobs/<id>/daemon.pid`` via the atomic ``*.tmp``
    + ``os.replace`` pattern so a tailing reader (CLI, future UI) never
    observes the file in a half-written state.
    """
    jobs_root_p = Path(jobs_root)
    job_root = jobs_root_p / job_id
    if not job_root.is_dir():
        raise FileNotFoundError(f"job folder missing: {job_root}")

    out_log = job_root / "daemon.out.log"
    err_log = job_root / "daemon.err.log"

    # Append-mode: re-launching the daemon must not clobber prior log lines.
    # The subprocess dups these fds into its own stdout/stderr; the parent's
    # copies are closed when the with-block exits.
    with open(out_log, "ab") as out_fh, open(err_log, "ab") as err_fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "research_agent.daemon", job_id],
            stdout=out_fh,
            stderr=err_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            cwd=Path.cwd(),
        )

    _atomic_write_text(job_root / "daemon.pid", f"{proc.pid}\n")
    return proc.pid


def is_daemon_alive(job_id: str, *, jobs_root: Path | str = DEFAULT_JOBS_ROOT) -> bool:
    """Return True iff ``jobs/<id>/daemon.pid`` points at a live daemon.

    Strategy:

    * Missing/invalid PID file → ``False``.
    * ``os.kill(pid, 0)`` works on macOS and Linux: succeeds when the
      process exists, raises :class:`ProcessLookupError` when it doesn't,
      and raises :class:`PermissionError` when the process exists but is
      owned by another user (we count that as alive).
    * On Linux, additionally peek ``/proc/<pid>/cmdline`` so a recycled PID
      that now belongs to an unrelated process doesn't false-positive. The
      cmdline check is best-effort: if ``/proc`` is unavailable or the file
      can't be read, we fall back to the ``kill -0`` result.
    """
    jobs_root_p = Path(jobs_root)
    pid_file = jobs_root_p / job_id / "daemon.pid"
    if not pid_file.exists():
        return False
    try:
        pid_text = pid_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    try:
        pid = int(pid_text)
    except ValueError:
        return False
    if pid <= 0:
        return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # EPERM means the process exists but we don't have signal rights.
        # A recycled PID owned by a different user still counts as "alive"
        # to the kill -0 check; the Linux /proc check below will catch the
        # recycle case where /proc is mounted.
        pass

    if sys.platform.startswith("linux"):
        cmdline_path = Path("/proc") / str(pid) / "cmdline"
        try:
            cmdline = cmdline_path.read_bytes().decode("utf-8", errors="replace")
        except FileNotFoundError:
            # /proc/<pid> disappeared between kill(0) and the read — racy
            # exit; treat as dead.
            return False
        except OSError:
            # /proc unavailable for some other reason — fall back to the
            # kill -0 result we already validated.
            return True
        if "research_agent.daemon" not in cmdline:
            return False

    return True


def _remove_pid_file(jobs_root: Path, job_id: str) -> None:
    """Best-effort unlink of ``jobs/<id>/daemon.pid``."""
    pid_file = Path(jobs_root) / job_id / "daemon.pid"
    try:
        pid_file.unlink()
    except FileNotFoundError:
        return
    except OSError:
        # Another process raced us, or the disk is unhappy. Don't escalate
        # from atexit — we've already done what we can.
        logger.exception("failed to remove %s", pid_file)


async def run_daemon(job_id: str, *, jobs_root: Path | str = DEFAULT_JOBS_ROOT) -> None:
    """Drive the agent loop for ``job_id`` until completion or stop.

    Stub: issue #33 owns the real implementation. This delegates to
    ``orchestrator.loop.run_daemon`` if it has shipped, otherwise it logs
    a warning and returns cleanly so the PID-file lifecycle (write on
    spawn, remove on shutdown) is exercised end-to-end.
    """
    _ = jobs_root  # kept on the signature for future use; loop.run_daemon owns root resolution
    from research_agent.orchestrator import loop as _loop

    real = getattr(_loop, "run_daemon", None)
    if real is None:
        logger.warning("daemon entrypoint stub — run_daemon not implemented yet (issue #33)")
        return
    await real(job_id)


def _make_signal_handler() -> Any:
    """Build a SIGTERM/SIGINT handler that routes through KeyboardInterrupt.

    Raising :class:`KeyboardInterrupt` from the handler unwinds the
    :func:`asyncio.run` call cleanly; ``_main`` catches it and exits with
    status 0 so atexit fires and the PID file is removed.
    """

    def _on_signal(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        raise KeyboardInterrupt

    return _on_signal


def _main(argv: list[str] | None = None) -> int:
    """Module entrypoint for ``python -m research_agent.daemon <job_id>``.

    Wires up the PID-file cleanup, signal handlers, and then runs the
    research loop via :func:`run_daemon`. Exits 0 on clean shutdown
    (including SIGTERM/SIGINT), 1 on an unhandled exception, 2 on usage.
    """
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 2:
        print("usage: python -m research_agent.daemon <job_id>", file=sys.stderr)
        return 2

    job_id = argv[1]
    jobs_root = Path(DEFAULT_JOBS_ROOT)

    atexit.register(_remove_pid_file, jobs_root, job_id)

    handler = _make_signal_handler()
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    try:
        asyncio.run(run_daemon(job_id, jobs_root=jobs_root))
    except KeyboardInterrupt:
        # SIGTERM/SIGINT handlers re-raise as KeyboardInterrupt; that's a
        # graceful shutdown per §5.2.
        return 0
    except Exception:
        logger.exception("daemon crashed for job %s", job_id)
        return 1
    return 0


__all__ = [
    "DEFAULT_JOBS_ROOT",
    "is_daemon_alive",
    "run_daemon",
    "spawn_daemon",
]


if __name__ == "__main__":
    raise SystemExit(_main())
