"""Long-running research process — drives the agent loop per job (impl guide §4, §5.1, §6).

Issue #32 shipped the spawn/PID-file lifecycle; issue #33 fills in the
research-loop body. ``spawn_daemon`` forks a detached child via
:func:`subprocess.Popen` with ``start_new_session=True`` so it survives
terminal exit, and ``is_daemon_alive`` checks the process behind
``jobs/<id>/daemon.pid``.

``run_daemon`` is the long-running async coroutine the child enters. It:

1. Loads the :class:`Job`, builds a :class:`Router`/:class:`BudgetTracker`
   from ``config/models.yaml``, runs the LM Studio + OpenRouter health
   checks (60 s deadline each), and flips ``status=running``.
2. Installs SIGTERM/SIGINT handlers via ``loop.add_signal_handler`` that
   set a single in-memory ``asyncio.Event`` *and* atomically touch
   ``jobs/<id>/STOP`` so the existing ``orchestrator.loop._should_stop``
   poll picks the request up.
3. Starts a 2 s ``STOP``-flag watcher task so a ``research stop --graceful``
   that drops the file externally also flips the event.
4. Calls :func:`research_agent.orchestrator.loop.run_loop`, then a
   best-effort :func:`research_agent.orchestrator.synth.final_synthesis`
   so ``report.md`` is always rewritten.
5. Sets ``status=completed``/``stopped``/``failed`` accordingly and exits
   with ``0`` on graceful shutdown, ``1`` on uncaught exception.

The PID file is written by the *parent* (spawn_daemon) and removed by the
*child* on shutdown via ``atexit``. Per §5.2 the daemon is the graceful
path — there is no separate "dirty" exit that leaves the file behind on a
normal signal.
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import httpx

from research_agent.config import get as cfg_get
from research_agent.observability.events import emit
from research_agent.storage import db
from research_agent.storage.jobs import (
    DEFAULT_JOBS_ROOT,
    INBOX_REPLAN_FILE,
    RESUME_REPLAN_FILE,
    Job,
)

logger = logging.getLogger(__name__)

INBOX_POLL_INTERVAL_S = 30.0


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


def spawn_daemon(
    job_id: str,
    *,
    jobs_root: Path | str = DEFAULT_JOBS_ROOT,
    env: dict[str, str] | None = None,
) -> int:
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
    child_env = None if env is None else {**os.environ, **env}
    with open(out_log, "ab") as out_fh, open(err_log, "ab") as err_fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "research_agent.daemon", job_id],
            stdout=out_fh,
            stderr=err_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            cwd=Path.cwd(),
            env=child_env,
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


# ---------------------------------------------------------------------------
# Health checks (acceptance criterion: 60 s retry deadline at startup)
# ---------------------------------------------------------------------------


_HEALTH_INITIAL_DELAY_S = 1.0
_HEALTH_MAX_DELAY_S = 10.0
_HEALTH_CLIENT_TIMEOUT_S = 10.0


async def _retry_until(
    job: Job | None,
    *,
    name: str,
    probe: Any,
    deadline_s: float,
) -> None:
    """Drive a single async ``probe()`` until it succeeds or ``deadline_s`` expires.

    Backoff doubles from ``_HEALTH_INITIAL_DELAY_S`` and clamps at
    ``_HEALTH_MAX_DELAY_S`` per the issue spec. The first failure emits a
    ``warning`` event so an operator tailing logs sees it before the full
    deadline expires; on deadline we emit ``error`` and re-raise.
    """
    start = time.monotonic()
    deadline = start + deadline_s
    delay = _HEALTH_INITIAL_DELAY_S
    last_exc: BaseException | None = None
    first_failure_emitted = False

    while True:
        try:
            await probe()
            return
        except BaseException as exc:  # noqa: BLE001 — health probes can raise anything
            last_exc = exc
            if not first_failure_emitted and job is not None:
                try:
                    emit(
                        job,
                        "WARN",
                        "daemon",
                        "warning",
                        {"stage": f"{name}_health", "error": str(exc)},
                    )
                except Exception:
                    pass
                first_failure_emitted = True
            logger.info(
                "%s health check failed (%s); retrying in %.1fs",
                name,
                exc,
                delay,
            )
        now = time.monotonic()
        if now + delay >= deadline:
            break
        await asyncio.sleep(delay)
        delay = min(delay * 2.0, _HEALTH_MAX_DELAY_S)

    msg = f"{name} not reachable within {deadline_s:.0f}s: {last_exc}"
    if job is not None:
        try:
            emit(
                job,
                "ERROR",
                "daemon",
                "error",
                {"stage": f"{name}_health", "error": str(last_exc)},
            )
        except Exception:
            pass
    raise RuntimeError(msg)


async def ensure_lm_studio_alive(router: Any, *, deadline_s: float = 60.0) -> None:
    """Block until LM Studio answers ``GET /models`` or 60 s expires.

    The router can't usefully dispatch local-tier calls without LM Studio,
    so failing fast at startup is friendlier than letting the first
    in-loop call time out per-tier.
    """
    base_url = (cfg_get("LMSTUDIO_BASE_URL") or "http://localhost:1234/v1").rstrip("/")
    url = f"{base_url}/models"

    async def _probe() -> None:
        async with httpx.AsyncClient(timeout=_HEALTH_CLIENT_TIMEOUT_S) as client:
            resp = await client.get(url)
            resp.raise_for_status()

    await _retry_until(
        getattr(router, "job", None),
        name="lm_studio",
        probe=_probe,
        deadline_s=deadline_s,
    )


async def ensure_openrouter_reachable(router: Any, *, deadline_s: float = 60.0) -> None:
    """Block until OpenRouter answers ``GET /models`` or 60 s expires.

    Authenticated request — a missing ``OPENROUTER_API_KEY`` should raise
    immediately rather than retry-loop for 60 s.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        msg = "OPENROUTER_API_KEY environment variable is required"
        job = getattr(router, "job", None)
        if job is not None:
            try:
                emit(
                    job,
                    "ERROR",
                    "daemon",
                    "error",
                    {"stage": "openrouter_health", "error": msg},
                )
            except Exception:
                pass
        raise RuntimeError(msg)

    url = "https://openrouter.ai/api/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}

    async def _probe() -> None:
        async with httpx.AsyncClient(timeout=_HEALTH_CLIENT_TIMEOUT_S) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()

    await _retry_until(
        getattr(router, "job", None),
        name="openrouter",
        probe=_probe,
        deadline_s=deadline_s,
    )


# ---------------------------------------------------------------------------
# Run-daemon: the §6.1 main coroutine
# ---------------------------------------------------------------------------


_STOP_POLL_INTERVAL_S = 2.0

# Per-job disk cap watcher (issue #38). Module-level so tests can compress
# the cadence to a fraction of a second.
DISK_CAP_POLL_INTERVAL_S = 300.0
DEFAULT_DISK_CAP_GB = 10.0

# Foreground progress bar (issue #41). 2 s matches `research status --watch`
# so the operator's two views advance in lockstep.
FOREGROUND_PROGRESS_INTERVAL_S = 2.0
ORPHAN_ARTIFACT_MAX_AGE_S = 300.0
_ORPHAN_ARTIFACT_DIRS = ("plan", "synthesis", "critique")
_ORPHAN_ARTIFACT_PATTERNS = ("*.partial.md", "*.tmp")


def _build_router(job: Job) -> Any:
    """Build a :class:`Router` + :class:`BudgetTracker` for ``job``.

    Imports are deferred so plain ``daemon.spawn_daemon`` callers don't pay
    the cost of pulling the LLM stack just to launch a child.
    """
    from research_agent.llm.budgets import BudgetTracker
    from research_agent.llm.router import (
        Router,
        load_models_config,
        resolve_models_config_path,
    )

    config = load_models_config(resolve_models_config_path())
    pricing = config.get("pricing") or {}
    intake = job.intake or {}
    cap_usd = intake.get("budget_cap_usd")

    budget = BudgetTracker(job.id, cap_usd, pricing=pricing, db_path=job.db_path)
    return Router(config, budget, job=job, db_path=job.db_path)


def _sweep_orphan_artifacts(
    job: Job,
    *,
    older_than_s: float = ORPHAN_ARTIFACT_MAX_AGE_S,
    now: float | None = None,
) -> list[Path]:
    """Remove stale partial/temporary artifacts left by interrupted writers."""
    now_ts = time.time() if now is None else now
    cleaned: list[Path] = []
    try:
        for dirname in _ORPHAN_ARTIFACT_DIRS:
            root = job.root / dirname
            if not root.is_dir():
                continue
            for pattern in _ORPHAN_ARTIFACT_PATTERNS:
                for path in root.glob(pattern):
                    try:
                        if not path.is_file():
                            continue
                        stat = path.stat()
                        age_s = max(0.0, now_ts - stat.st_mtime)
                        if age_s < older_than_s:
                            continue
                        path.unlink()
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        logger.warning(
                            "daemon: failed to clean orphan artifact %s: %s", path, exc
                        )
                        try:
                            emit(
                                job,
                                "WARN",
                                "daemon",
                                "warning",
                                {
                                    "stage": "orphan_artifact_sweep",
                                    "path": str(path),
                                    "error": str(exc),
                                },
                            )
                        except Exception:
                            pass
                        continue

                    cleaned.append(path)
                    try:
                        emit(
                            job,
                            "INFO",
                            "daemon",
                            "orphan_artifact_cleaned",
                            {
                                "path": str(path.relative_to(job.root)),
                                "age_seconds": round(age_s, 3),
                            },
                        )
                    except Exception:
                        logger.exception(
                            "daemon: failed to emit orphan cleanup event for %s", path
                        )
    except Exception as exc:  # noqa: BLE001 — startup cleanup must be best-effort
        logger.exception("daemon: orphan artifact sweep failed for job %s", job.id)
        try:
            emit(
                job,
                "WARN",
                "daemon",
                "warning",
                {"stage": "orphan_artifact_sweep", "error": str(exc)},
            )
        except Exception:
            pass
    return cleaned


def _resolve_db_path() -> Path | None:
    """Pull ``RESEARCH_DB_PATH`` from the env, if set, else None for the default."""
    val = os.environ.get("RESEARCH_DB_PATH")
    return Path(val) if val else None


def _coerce_positive_hours(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return None
    return hours if hours > 0 else None


def _load_time_cap_hours(job: Job, intake: dict[str, Any]) -> float | None:
    raw = intake.get("time_cap_hours")
    if raw is not None:
        return _coerce_positive_hours(raw)

    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            "SELECT time_cap_hours FROM jobs WHERE id = ?",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    return _coerce_positive_hours(row["time_cap_hours"] if row is not None else None)


def _consume_resume_replan_request(job: Job) -> dict[str, Any] | None:
    """Read and delete the one-shot resume replan sidecar, if present."""
    path = job.root / RESUME_REPLAN_FILE
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        payload = {}
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return payload if isinstance(payload, dict) else {}


async def _pre_loop_resume_replan(job: Job, router: Any, request: dict[str, Any]) -> None:
    """Run tactical_replan once before ``run_loop`` pulls the next queued task."""
    from research_agent.orchestrator import loop as _loop
    from research_agent.orchestrator import plan as _plan

    plan = _loop._load_latest_plan(job)
    if plan is None:
        return
    recent_results = _loop._load_recent_task_results(job)
    prior_attempts = _plan._compute_prior_attempts_for_subgoal(
        plan,
        _loop._load_all_task_attempts(job),
    )
    inconclusive_context = [
        item for item in prior_attempts.values() if item.get("prior_task_kinds")
    ]
    kwargs: dict[str, Any] = {}
    if inconclusive_context:
        kwargs["inconclusive_subgoals"] = inconclusive_context
    note = request.get("note")
    if isinstance(note, str) and note.strip():
        kwargs["user_note"] = note.strip()

    emit(
        job,
        "INFO",
        "daemon",
        "replan_triggered",
        {"stage": "resume", "has_note": "user_note" in kwargs},
    )
    await _plan.tactical_replan(
        job,
        plan,
        recent_results,
        router=router,
        findings=_loop._load_all_findings(job),
        synthesis_md=_loop._load_latest_synthesis_md(job),
        follow_up_questions=_loop._load_pending_follow_up_questions(recent_results),
        **kwargs,
    )


def _install_stop_signal_handlers(
    aloop: asyncio.AbstractEventLoop,
    job: Job,
    should_stop: asyncio.Event,
) -> None:
    """Wire SIGTERM/SIGINT through the asyncio loop into a graceful stop.

    The handler does two things: atomically writes ``jobs/<id>/STOP`` so
    ``orchestrator.loop._should_stop`` picks the request up between tasks,
    and sets the in-memory event so any non-loop awaiter (e.g. the watcher
    or future health-check loops) can also bail.
    """
    stop_path = job.root / "STOP"

    def _on_signal() -> None:
        try:
            tmp = stop_path.with_suffix(".tmp")
            tmp.write_text("", encoding="utf-8")
            os.replace(tmp, stop_path)
        except OSError:
            logger.exception("failed to write STOP flag for job %s", job.id)
        should_stop.set()

    aloop.add_signal_handler(signal.SIGTERM, _on_signal)
    aloop.add_signal_handler(signal.SIGINT, _on_signal)


async def _disk_cap_watcher(
    job: Job,
    cap_bytes: int,
    should_stop: asyncio.Event,
    *,
    interval_s: float | None = None,
) -> None:
    """Poll job-folder disk usage; prune lowest-relevance sources when over cap.

    Wakes every ``interval_s`` (default :data:`DISK_CAP_POLL_INTERVAL_S`,
    300 s per issue #38) using ``asyncio.wait_for`` against ``should_stop``
    so SIGTERM cancels the wait promptly. Each tick is wrapped in
    try/except + a ``warning`` event so a transient OSError can't kill
    the daemon.
    """
    from research_agent.storage.disk_cap import disk_usage_bytes, prune_to_target

    poll_s = interval_s if interval_s is not None else DISK_CAP_POLL_INTERVAL_S
    try:
        while not should_stop.is_set():
            try:
                usage = disk_usage_bytes(job.root)
                if usage > cap_bytes:
                    prune_to_target(job, cap_bytes=cap_bytes, db_path=job.db_path)
            except Exception as exc:  # noqa: BLE001 — never let the watcher die
                logger.exception("disk cap watcher tick failed for job %s", job.id)
                try:
                    emit(
                        job,
                        "WARN",
                        "disk_cap",
                        "warning",
                        {"stage": "disk_cap_tick", "error": str(exc)},
                    )
                except Exception:
                    pass
            try:
                await asyncio.wait_for(should_stop.wait(), timeout=poll_s)
            except TimeoutError:
                continue
    except asyncio.CancelledError:
        return


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inbox_topic_guess(path: Path) -> str:
    stem = re.sub(r"[_-]+", " ", path.stem).strip()
    return stem[:120] if stem else path.name[:120]


def _inbox_summary(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt", ".html", ".htm"}:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        text = " ".join(text.split())
        if text:
            return text[:240]
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return f"{path.suffix.lower().lstrip('.') or 'file'}; {size} bytes"


def _processed_inbox_path(inbox_dir: Path, sha: str, filename: str) -> Path:
    processed_dir = inbox_dir / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-") or "document"
    return processed_dir / f"{sha}-{safe_name}"


async def _inbox_watcher(
    job: Job,
    should_stop: asyncio.Event,
    *,
    interval_s: float | None = None,
) -> None:
    """Poll ``jobs/<id>/inbox`` for human-supplied documents."""
    from research_agent.tools import local_corpus

    poll_s = interval_s if interval_s is not None else INBOX_POLL_INTERVAL_S
    inbox_dir = job.root / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    (inbox_dir / "processed").mkdir(parents=True, exist_ok=True)

    try:
        while not should_stop.is_set():
            for file_path in sorted(inbox_dir.iterdir()):
                if should_stop.is_set():
                    break
                if not file_path.is_file():
                    continue
                if file_path.name.endswith(".tmp"):
                    continue
                try:
                    sha = _file_sha256(file_path)
                    summary = _inbox_summary(file_path)
                    topic_guess = _inbox_topic_guess(file_path)
                    indexed = local_corpus.index(file_path, job)
                    processed_path = _processed_inbox_path(inbox_dir, sha, file_path.name)
                    os.replace(file_path, processed_path)
                    note = (
                        f"user added {file_path.name} ({summary}); "
                        "identify NEW angles enabled by this evidence"
                    )
                    _atomic_write_text(
                        job.root / INBOX_REPLAN_FILE,
                        json.dumps(
                            {
                                "trigger": "inbox",
                                "filename": file_path.name,
                                "sha": sha,
                                "summary": summary,
                                "note": note,
                            },
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n",
                    )
                    emit(
                        job,
                        "INFO",
                        "daemon",
                        "corpus_doc_added",
                        {
                            "sha": sha,
                            "filename": file_path.name,
                            "processed_path": str(processed_path.relative_to(job.root)),
                            "topic_guess": topic_guess,
                            "summary_chars": len(summary),
                            **indexed,
                        },
                    )
                except Exception as exc:  # noqa: BLE001 — keep watcher alive
                    logger.exception("inbox watcher failed for %s", file_path)
                    try:
                        emit(
                            job,
                            "WARN",
                            "daemon",
                            "warning",
                            {
                                "stage": "inbox_watcher",
                                "path": str(file_path),
                                "error": str(exc),
                            },
                        )
                    except Exception:
                        pass
            try:
                await asyncio.wait_for(should_stop.wait(), timeout=poll_s)
            except TimeoutError:
                continue
    except asyncio.CancelledError:
        return


def _should_show_foreground_progress(stream: Any) -> bool:
    """True when the daemon should drive a Rich Progress bar on ``stream``.

    Active only when stdout is a real TTY (``spawn_daemon`` redirects to a
    log file, where this stays dormant) and the operator hasn't opted out
    via ``RESEARCH_DAEMON_PROGRESS=0``. Anything that isn't ``"0"`` is
    treated as opt-in.
    """
    if os.environ.get("RESEARCH_DAEMON_PROGRESS") == "0":
        return False
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except Exception:  # noqa: BLE001 — defensive: never let isatty crash the daemon
        return False


def _plan_completion(job: Job, plan_version: int | None) -> tuple[int, int]:
    """Return ``(done_count, total_count)`` for the latest plan version's tasks.

    ``plan_version`` is recomputed each tick because a mid-run replan
    advances ``plans.version`` and the operator wants the bar to track the
    *current* plan, not stale rows from a prior pass.
    """
    from research_agent.storage import db as _db

    conn = _db.connect(job.db_path)
    try:
        if plan_version is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM plans WHERE job_id = ?",
                (job.id,),
            ).fetchone()
            plan_version = int(row["v"]) if row is not None else 0

        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks"
            " WHERE job_id = ? AND plan_version = ? GROUP BY status",
            (job.id, plan_version),
        ).fetchall()
    finally:
        conn.close()

    counts = {str(r["status"]): int(r["n"]) for r in rows}
    done = counts.get("done", 0)
    total = (
        counts.get("pending", 0)
        + counts.get("running", 0)
        + counts.get("done", 0)
        + counts.get("failed", 0)
    )
    return done, total


async def _foreground_progress_task(
    job: Job,
    should_stop: asyncio.Event,
    *,
    interval_s: float | None = None,
    stream: Any | None = None,
    progress_factory: Any | None = None,
) -> None:
    """Drive a Rich ``Progress`` bar on stdout while the daemon runs.

    Only runs when the chosen ``stream`` is a TTY and the
    ``RESEARCH_DAEMON_PROGRESS`` env var is not ``"0"`` — under
    :func:`spawn_daemon` stdout is the per-job log file (non-TTY), so this
    coroutine returns immediately and never writes to disk. Tests inject a
    fake ``progress_factory`` to capture updates without a real terminal.
    """
    chosen_stream = stream if stream is not None else sys.stdout
    if not _should_show_foreground_progress(chosen_stream):
        return

    poll_s = interval_s if interval_s is not None else FOREGROUND_PROGRESS_INTERVAL_S

    if progress_factory is None:
        from rich.console import Console
        from rich.progress import (
            BarColumn,
            Progress,
            TaskProgressColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        def _factory() -> Any:
            console = Console(file=chosen_stream, force_terminal=True)
            return Progress(
                TextColumn(f"[bold]{job.id}[/bold]"),
                BarColumn(),
                TaskProgressColumn(),
                TextColumn("•"),
                TimeElapsedColumn(),
                console=console,
                transient=False,
            )

        progress = _factory()
    else:
        progress = progress_factory()

    bar_id = progress.add_task("plan", total=1, completed=0)

    try:
        with progress:
            while not should_stop.is_set():
                try:
                    done, total = _plan_completion(job, plan_version=None)
                except Exception:  # noqa: BLE001 — bar must not crash the daemon
                    logger.exception("foreground progress tick failed for job %s", job.id)
                    done, total = 0, 0
                progress.update(bar_id, total=max(1, total), completed=done)
                try:
                    await asyncio.wait_for(should_stop.wait(), timeout=poll_s)
                except TimeoutError:
                    continue
    except asyncio.CancelledError:
        return


async def _cancel_with_timeout(
    task: asyncio.Task,
    name: str,
    *,
    timeout: float = 5.0,
) -> None:
    """Cancel ``task`` and await its termination, bounded by ``timeout`` seconds.

    Without the timeout, a misbehaving watcher that ignores cancellation
    (e.g. a stale event-loop reference, a CancelledError swallowed in a
    too-broad except, or a synchronous I/O call blocking the loop) hangs
    daemon teardown forever. We log a warning and abandon the task on
    timeout — process exit will reap it.

    Uses :func:`asyncio.wait` (not :func:`asyncio.wait_for`) so the timeout
    fires regardless of whether the task responds to ``cancel()``. Critical
    for ``run_daemon``'s post-loop finally block.
    """
    if task.done():
        return
    task.cancel()
    try:
        _done, pending = await asyncio.wait({task}, timeout=timeout)
    except Exception:  # noqa: BLE001 — never let teardown raise
        logger.exception("daemon: %s raised on cancel/await", name)
        return
    if pending:
        logger.warning(
            "daemon: %s did not cancel within %.1fs; abandoning (process exit will reap)",
            name,
            timeout,
        )


async def _stop_flag_watcher(
    job: Job,
    should_stop: asyncio.Event,
    *,
    poll_interval_s: float = _STOP_POLL_INTERVAL_S,
) -> None:
    """Poll ``jobs/<id>/STOP`` every ``poll_interval_s`` seconds.

    The signal handler also touches the file so this loop is in fact
    redundant for the SIGTERM/SIGINT path — but ``research stop --graceful``
    drops the file from a sibling process, and that needs to flip the
    in-memory event without relying on a signal arriving.
    """
    stop_path = job.root / "STOP"
    try:
        while not should_stop.is_set():
            if stop_path.exists():
                should_stop.set()
                return
            try:
                await asyncio.wait_for(should_stop.wait(), timeout=poll_interval_s)
            except TimeoutError:
                continue
    except asyncio.CancelledError:
        return


async def run_daemon(
    job_id: str,
    *,
    jobs_root: Path | str = DEFAULT_JOBS_ROOT,
    db_path: Path | str | None = None,
) -> int:
    """Drive the agent loop for ``job_id`` until completion, stop, or failure.

    Returns the integer exit code: ``0`` on a graceful run (including
    SIGTERM/SIGINT or external STOP), ``1`` on an uncaught exception.

    ``db_path`` defaults to the project-relative ``data/index.sqlite`` (via
    :data:`research_agent.storage.db.DEFAULT_DB_PATH`); tests pass a
    ``tmp_path`` override so the suite never touches the real index.
    """
    from research_agent.orchestrator import loop as _loop
    from research_agent.orchestrator import plan as _plan
    from research_agent.orchestrator import synth as _synth
    from research_agent.storage import db as _db

    jobs_root_p = Path(jobs_root)
    db_path_p = Path(db_path) if db_path is not None else _db.DEFAULT_DB_PATH

    try:
        job = Job.load(job_id, jobs_root=jobs_root_p, db_path=db_path_p)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        logger.error("daemon: cannot load job %r: %s", job_id, exc)
        return 1

    # ``research start --fragments`` records ``intake.fragments`` and exports
    # RESEARCH_FRAGMENT_SYNTH for the freshly spawned daemon. ``research
    # resume`` (and any other restart path) spawns the daemon from a clean
    # shell that no longer carries that env var, so without this the resumed
    # daemon would silently revert a fragment job to legacy whole-report
    # synthesis. Persisted intake is the source of truth on restart; only
    # set (never clear) so an operator-wide opt-in still works.
    if (job.intake or {}).get("fragments") and not os.environ.get(
        "RESEARCH_FRAGMENT_SYNTH"
    ):
        os.environ["RESEARCH_FRAGMENT_SYNTH"] = "1"

    _sweep_orphan_artifacts(job)

    try:
        router = _build_router(job)
    except Exception as exc:
        logger.exception("daemon: failed to build router for job %s", job.id)
        try:
            emit(
                job,
                "ERROR",
                "daemon",
                "error",
                {
                    "stage": "router_init",
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )
        except Exception:
            pass
        try:
            job.set_status("failed")
        except Exception:
            pass
        return 1

    skip_health = os.environ.get("RESEARCH_DAEMON_SKIP_HEALTH_CHECKS") == "1"
    if not skip_health:
        try:
            await ensure_lm_studio_alive(router)
            await ensure_openrouter_reachable(router)
        except Exception as exc:
            logger.error("daemon: health checks failed for job %s: %s", job.id, exc)
            try:
                job.set_status("failed")
            except Exception:
                pass
            return 1

    job.set_status("running")

    if _loop._load_latest_plan(job) is None:
        try:
            await _plan.initial_plan(job, router=router)
        except Exception as exc:
            logger.exception("daemon: initial_plan failed for job %s", job.id)
            try:
                emit(
                    job,
                    "ERROR",
                    "daemon",
                    "error",
                    {
                        "stage": "initial_plan",
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                )
            except Exception:
                pass
            try:
                job.set_status("failed")
            except Exception:
                pass
            return 1

    resume_replan_request = _consume_resume_replan_request(job)
    if resume_replan_request is not None:
        try:
            await _pre_loop_resume_replan(job, router, resume_replan_request)
        except Exception as exc:  # noqa: BLE001 — preserve normal resume on replan failure
            logger.warning("daemon: resume replan failed for job %s: %s", job.id, exc)
            try:
                emit(
                    job,
                    "WARN",
                    "daemon",
                    "warning",
                    {"stage": "resume_replan", "error": str(exc)},
                )
            except Exception:
                pass

    should_stop = asyncio.Event()
    aloop = asyncio.get_running_loop()
    signals_installed = False
    try:
        _install_stop_signal_handlers(aloop, job, should_stop)
        signals_installed = True
    except (NotImplementedError, ValueError):
        # Windows / inside a thread: fall back to default signal handling.
        # The STOP-flag watcher still works, so callers using
        # ``research stop --graceful`` aren't broken.
        logger.warning("could not install asyncio signal handlers; relying on STOP flag only")

    watcher_task = aloop.create_task(_stop_flag_watcher(job, should_stop))

    intake = job.intake or {}
    disk_cap_gb_raw = intake.get("disk_cap_gb", DEFAULT_DISK_CAP_GB)
    try:
        disk_cap_gb = float(disk_cap_gb_raw) if disk_cap_gb_raw is not None else DEFAULT_DISK_CAP_GB
    except (TypeError, ValueError):
        disk_cap_gb = DEFAULT_DISK_CAP_GB
    cap_bytes = max(1, int(disk_cap_gb * 1024 * 1024 * 1024))
    disk_cap_task = aloop.create_task(_disk_cap_watcher(job, cap_bytes, should_stop))
    inbox_task: asyncio.Task | None = None
    if intake.get("inbox") is True:
        inbox_task = aloop.create_task(_inbox_watcher(job, should_stop))
    progress_task = aloop.create_task(_foreground_progress_task(job, should_stop))

    from research_agent.llm.budgets import BudgetExceeded

    exit_code = 0
    final_status = "stopped"
    completion_reason: str | None = None
    try:
        loop_result: dict[str, Any] | None = None
        budget_capped = False
        max_tasks_override = intake.get("max_tasks")
        loop_kwargs: dict[str, Any] = {}
        if isinstance(max_tasks_override, int) and max_tasks_override >= 1:
            loop_kwargs["max_tasks"] = max_tasks_override
        time_cap_override = _load_time_cap_hours(job, intake)
        if time_cap_override is not None:
            loop_kwargs["time_cap_hours"] = time_cap_override
        try:
            loop_result = await _loop.run_loop(job, router, **loop_kwargs)
        except BudgetExceeded as exc:
            logger.warning("daemon: budget cap reached mid-loop for job %s: %s", job.id, exc)
            budget_capped = True
            try:
                emit(
                    job,
                    "WARN",
                    "daemon",
                    "warning",
                    {
                        "stage": "run_loop_budget_cap",
                        "error": str(exc),
                        "spent": getattr(exc, "spent", None),
                        "cap": getattr(exc, "cap", None),
                    },
                )
            except Exception:
                pass
        except Exception as exc:
            logger.exception("daemon: run_loop crashed for job %s", job.id)
            try:
                emit(
                    job,
                    "ERROR",
                    "daemon",
                    "error",
                    {
                        "stage": "run_loop",
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                )
            except Exception:
                pass
            try:
                job.set_status("failed")
            except Exception:
                pass
            return 1

        plan = _loop._load_latest_plan(job)
        loop_time_capped = loop_result is not None and (
            loop_result.get("completion_reason") == "time_cap" or loop_result.get("time_cap_hit")
        )
        if budget_capped:
            try:
                if plan is not None:
                    await _synth.final_synthesis_after_cap(job, plan, router=router)
            except Exception as exc:
                logger.warning(
                    "daemon: post-cap final synthesis failed for job %s: %s", job.id, exc
                )
                try:
                    emit(
                        job,
                        "WARN",
                        "daemon",
                        "warning",
                        {"stage": "final_synthesis_after_cap", "error": str(exc)},
                    )
                except Exception:
                    pass
            final_status = "completed"
            completion_reason = "budget_cap"
        else:
            if not loop_time_capped:
                try:
                    if plan is not None:
                        await _synth.final_synthesis(job, plan, router=router)
                except Exception as exc:
                    logger.warning("daemon: final synthesis failed for job %s: %s", job.id, exc)
                    try:
                        emit(
                            job,
                            "WARN",
                            "daemon",
                            "warning",
                            {"stage": "final_synthesis", "error": str(exc)},
                        )
                    except Exception:
                        pass

            # Re-load the plan after any final synthesis path: that pass can
            # fire a synthesizer that calls update_subgoal_done, writing a new
            # plan version with done=True flags. Using the stale plan from
            # before final_synthesis would miss those closures and mis-classify
            # a clean finish as user_stopped (issue #160).
            plan = _loop._load_latest_plan(job)

            if _loop._should_stop(job):
                final_status = "stopped"
                completion_reason = "user_stopped"
            elif loop_time_capped:
                final_status = "completed"
                completion_reason = "time_cap"
            elif (
                loop_result is not None
                and loop_result.get("completion_reason") == "confirmed_gap"
            ):
                final_status = "completed"
                completion_reason = "confirmed_gap"
            elif loop_result is not None and loop_result.get("completion_reason") == "exhausted":
                final_status = "completed"
                completion_reason = "exhausted"
            elif loop_result is not None and loop_result.get("cap_hit"):
                final_status = "completed"
                completion_reason = "task_cap"
            elif plan is not None and _loop._is_goal_complete(job, plan):
                final_status = "completed"
                completion_reason = "goal_complete"
            elif plan is not None and plan.is_complete():
                final_status = "completed"
                completion_reason = "exhausted"
            else:
                final_status = "stopped"
                completion_reason = "user_stopped"

        try:
            job.set_status(final_status, completion_reason=completion_reason)
        except Exception:
            logger.exception("daemon: failed to write final status for job %s", job.id)
    finally:
        # Cancel each background task with a bounded timeout. Without the
        # timeout, a misbehaving watcher whose cancel doesn't propagate (e.g.
        # blocked in a synchronous I/O call inside its loop body) hangs the
        # whole daemon indefinitely after run_loop completes. Observed in the
        # wild: a 60-min Project 2025 run that hit max_tasks cap, fired final
        # synth, then sat idle for 48 min in this finally block.
        await _cancel_with_timeout(watcher_task, "stop_flag_watcher", timeout=5.0)
        await _cancel_with_timeout(disk_cap_task, "disk_cap_watcher", timeout=5.0)
        if inbox_task is not None:
            await _cancel_with_timeout(inbox_task, "inbox_watcher", timeout=5.0)
        await _cancel_with_timeout(progress_task, "foreground_progress_task", timeout=5.0)
        if signals_installed:
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    aloop.remove_signal_handler(sig)
                except (NotImplementedError, ValueError):
                    pass

    return exit_code


def _main(argv: list[str] | None = None) -> int:
    """Module entrypoint for ``python -m research_agent.daemon <job_id>``.

    Wires up the PID-file cleanup, then runs :func:`run_daemon` (which
    installs its own signal handlers via the asyncio loop). Surfaces the
    exit code from :func:`run_daemon`: ``0`` on graceful shutdown,
    ``1`` on an uncaught exception, ``2`` on usage error.
    """
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 2:
        print("usage: python -m research_agent.daemon <job_id>", file=sys.stderr)
        return 2

    job_id = argv[1]
    jobs_root_env = os.environ.get("RESEARCH_JOBS_ROOT")
    jobs_root = Path(jobs_root_env) if jobs_root_env else Path(DEFAULT_JOBS_ROOT)
    db_path = _resolve_db_path()

    atexit.register(_remove_pid_file, jobs_root, job_id)

    try:
        return asyncio.run(run_daemon(job_id, jobs_root=jobs_root, db_path=db_path))
    except KeyboardInterrupt:
        # The asyncio signal handler turns SIGINT into a graceful stop, but
        # if the handler couldn't be installed (e.g. running on Windows) the
        # default SIGINT behavior raises here. Treat as graceful so the PID
        # file is still cleaned up.
        return 0
    except Exception:
        logger.exception("daemon crashed for job %s", job_id)
        return 1


__all__ = [
    "DEFAULT_JOBS_ROOT",
    "ensure_lm_studio_alive",
    "ensure_openrouter_reachable",
    "is_daemon_alive",
    "run_daemon",
    "spawn_daemon",
]


if __name__ == "__main__":
    raise SystemExit(_main())
