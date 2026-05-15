"""Per-job folder management and the ``jobs`` row contract.

Implements the load-bearing convention from §4 of the implementation guide:
every job is a self-contained folder under ``jobs/<job-id>/`` with a fixed
sidecar layout (``job.json``, ``intake.json``, ``goal.md``, the ``plan/``,
``findings/``, ``sources/``, ``synthesis/``, ``critique/``, ``report.history/``
subfolders, and an append-only ``events.jsonl``). Transient control files
live next to those sidecars: ``STOP`` requests graceful shutdown and
``RESUME_REPLAN.json`` asks the daemon to run one tactical replan before
resuming the queue. The cross-job ``jobs`` table in
:mod:`research_agent.storage.db` mirrors the canonical metadata so the future
UI and the ``research list`` CLI can query without scanning disk.

Job IDs are deterministic ``YYYY-MM-DD-<slug>`` strings derived from the
intake goal. The slug is normalized aggressively (lowercased, non-alphanum
collapsed to ``-``, capped at 60 chars) and rejects any sequence that could
escape the jobs root via path traversal.

All on-disk writes go through the atomic ``*.tmp`` + :func:`os.replace`
pattern from §16 so a crashed process never leaves half-written sidecars
that a tail-watching reader could pick up.
"""

from __future__ import annotations

import json
import os
import re
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from research_agent.storage import db

DEFAULT_JOBS_ROOT = Path("jobs")
RESUME_REPLAN_FILE = "RESUME_REPLAN.json"
INBOX_REPLAN_FILE = "INBOX_REPLAN.json"

# Daemon kill escalation window. Module-level so tests can monkeypatch it
# down from 10s to keep the suite fast.
KILL_ESCALATION_SECONDS = 10.0
KILL_POLL_INTERVAL_SECONDS = 0.5

_SUBDIRS = (
    "plan",
    "findings",
    "sources",
    "synthesis",
    "critique",
    "report.history",
    "archive",
    "inbox",
    "inbox/processed",
    "artifacts",
)
# Subdirs that get wiped on a soft reset; ``archive`` is preserved on purpose
# so prior reports stay around for ``research compare``.
_RESETTABLE_SUBDIRS = ("plan", "findings", "sources", "synthesis", "critique", "report.history")
_JOB_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]{0,59}$")
_SLUG_FORBIDDEN = ("/", "\\", "..")

# Allowed values for ``completion_reason`` per issue #39 §9.
ALLOWED_COMPLETION_REASONS = frozenset(
    {"goal_complete", "time_cap", "budget_cap", "task_cap", "user_stopped", "exhausted"}
)


def _slugify(text: str, max_len: int = 60) -> str:
    """Normalize ``text`` into a safe slug for a job folder name.

    Lowercases, collapses runs of non-alphanumeric characters into ``-``,
    strips leading/trailing dashes, truncates to ``max_len``. Raises
    :class:`ValueError` if the input normalizes to an empty string.
    """
    if not isinstance(text, str):
        raise ValueError(f"slug input must be a string; got {type(text).__name__}")

    lowered = text.strip().lower()
    collapsed = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not collapsed:
        raise ValueError(f"slug input normalized to empty string: {text!r}")

    truncated = collapsed[:max_len].rstrip("-")
    if not truncated:
        raise ValueError(f"slug input collapsed to empty after truncation: {text!r}")
    for bad in _SLUG_FORBIDDEN:
        if bad in truncated:
            raise ValueError(f"slug output contains forbidden sequence {bad!r}: {truncated!r}")
    return truncated


def _atomic_write_text(path: Path, data: str) -> None:
    """Write ``data`` to ``path`` via a sibling ``*.tmp`` + :func:`os.replace`.

    Per §16 anti-patterns: a tailing reader (editor, future UI) must never
    see a half-written file. The temp file is colocated with the target so
    ``os.replace`` is a same-filesystem atomic rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_json(path: Path, obj: Any) -> None:
    _atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True) + "\n")


def _now_epoch() -> int:
    return int(time.time())


def _validate_job_id(job_id: str) -> None:
    if not isinstance(job_id, str) or not _JOB_ID_RE.match(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")


@dataclass
class Job:
    """A single research job — its folder, its DB row, and lifecycle ops."""

    id: str
    root: Path
    goal: str
    domain: str | None
    status: str
    intake: dict[str, Any]
    created_at: int
    db_path: Path = field(default=db.DEFAULT_DB_PATH)
    completion_reason: str | None = None

    # ---- Construction --------------------------------------------------

    @classmethod
    def create(
        cls,
        intake: dict[str, Any],
        *,
        jobs_root: Path | str = DEFAULT_JOBS_ROOT,
        db_path: Path | str = db.DEFAULT_DB_PATH,
        today: date | None = None,
    ) -> Job:
        """Materialize a new job: folder + sidecars + ``jobs`` row.

        ``intake`` must include ``goal``. Optional keys: ``domain``,
        ``time_cap_hours``, ``budget_cap_usd``, ``aggressiveness``.
        """
        if not isinstance(intake, dict):
            raise TypeError(f"intake must be a dict; got {type(intake).__name__}")
        goal = intake.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("intake['goal'] must be a non-empty string")

        slug = _slugify(goal)
        day = today or datetime.now(UTC).date()
        job_id = f"{day:%Y-%m-%d}-{slug}"
        # Re-validate composed id — defensive against future slug rules.
        _validate_job_id(job_id)

        jobs_root_p = Path(jobs_root)
        db_path_p = Path(db_path)
        root = jobs_root_p / job_id

        if root.exists():
            raise FileExistsError(f"job folder already exists: {root}")

        root.mkdir(parents=True, exist_ok=False)
        for sub in _SUBDIRS:
            (root / sub).mkdir()

        now = _now_epoch()
        domain = intake.get("domain")
        status = "pending"

        job_meta = {
            "id": job_id,
            "goal": goal,
            "domain": domain,
            "status": status,
            "created_at": now,
            "intake": intake,
        }
        _atomic_write_json(root / "job.json", job_meta)
        _atomic_write_json(root / "intake.json", intake)
        _atomic_write_text(root / "goal.md", goal.strip() + "\n")
        # Touch events.jsonl so tail-followers don't error on a missing file.
        _atomic_write_text(root / "events.jsonl", "")

        intake_json = json.dumps(intake, sort_keys=True)
        conn = db.connect(db_path_p)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        id, goal, domain, status, intake_json,
                        time_cap_hours, budget_cap_usd, aggressiveness,
                        created_at, last_activity_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        goal,
                        domain,
                        status,
                        intake_json,
                        intake.get("time_cap_hours"),
                        intake.get("budget_cap_usd"),
                        intake.get("aggressiveness"),
                        now,
                        now,
                    ),
                )
        finally:
            conn.close()

        return cls(
            id=job_id,
            root=root,
            goal=goal,
            domain=domain,
            status=status,
            intake=intake,
            created_at=now,
            db_path=db_path_p,
        )

    @classmethod
    def load(
        cls,
        job_id: str,
        *,
        jobs_root: Path | str = DEFAULT_JOBS_ROOT,
        db_path: Path | str = db.DEFAULT_DB_PATH,
    ) -> Job:
        """Rehydrate a job from disk + DB. Raises if either is missing."""
        _validate_job_id(job_id)

        jobs_root_p = Path(jobs_root)
        db_path_p = Path(db_path)
        root = jobs_root_p / job_id

        job_json = root / "job.json"
        if not job_json.exists():
            raise FileNotFoundError(f"job folder missing: {root}")

        meta = json.loads(job_json.read_text(encoding="utf-8"))
        if meta.get("id") != job_id:
            raise ValueError(f"job.json id mismatch: folder={job_id!r} meta={meta.get('id')!r}")

        conn = db.connect(db_path_p)
        try:
            row = conn.execute(
                "SELECT goal, domain, status, intake_json, created_at, completion_reason"
                " FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            raise KeyError(f"job row missing in DB: {job_id}")

        return cls(
            id=job_id,
            root=root,
            goal=row["goal"],
            domain=row["domain"],
            status=row["status"],
            intake=json.loads(row["intake_json"]),
            created_at=int(row["created_at"]),
            db_path=db_path_p,
            completion_reason=row["completion_reason"],
        )

    # ---- Lifecycle ops -------------------------------------------------

    def set_status(self, state: str, completion_reason: str | None = None) -> None:
        """Update ``status`` + ``last_activity_at`` in the DB and mirror to ``job.json``.

        When ``completion_reason`` is provided it is written alongside the
        status in a single UPDATE and mirrored into ``job.json`` so disk-only
        consumers see the same value. Allowed reasons live in
        :data:`ALLOWED_COMPLETION_REASONS`.
        """
        if not isinstance(state, str) or not state:
            raise ValueError(f"status must be a non-empty string; got {state!r}")
        if completion_reason is not None and completion_reason not in ALLOWED_COMPLETION_REASONS:
            raise ValueError(
                f"completion_reason must be one of {sorted(ALLOWED_COMPLETION_REASONS)};"
                f" got {completion_reason!r}"
            )

        now = _now_epoch()
        conn = db.connect(self.db_path)
        try:
            with conn:
                if completion_reason is not None:
                    conn.execute(
                        "UPDATE jobs SET status = ?, last_activity_at = ?,"
                        " completion_reason = ? WHERE id = ?",
                        (state, now, completion_reason, self.id),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET status = ?, last_activity_at = ? WHERE id = ?",
                        (state, now, self.id),
                    )
        finally:
            conn.close()

        self.status = state
        # Mirror into job.json so a disk-only consumer sees the same state.
        job_json = self.root / "job.json"
        meta = json.loads(job_json.read_text(encoding="utf-8"))
        meta["status"] = state
        meta["last_activity_at"] = now
        if completion_reason is not None:
            meta["completion_reason"] = completion_reason
            self.completion_reason = completion_reason
        _atomic_write_json(job_json, meta)

    def request_stop(self) -> None:
        """Drop a ``STOP`` flag the daemon polls between tasks."""
        _atomic_write_text(self.root / "STOP", "")

    def kill(self) -> None:
        """SIGTERM the daemon PID; escalate to SIGKILL after the grace window."""
        pid_file = self.root / "daemon.pid"
        if not pid_file.exists():
            raise FileNotFoundError(f"no PID file at {pid_file}")
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except ValueError as e:
            raise ValueError(f"PID file at {pid_file} is not an integer") from e

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return  # already dead

        deadline = time.monotonic() + KILL_ESCALATION_SECONDS
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(KILL_POLL_INTERVAL_SECONDS)

        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return


    # ---- Archive + soft reset (issue #210) ----------------------------

    def archive_and_soft_reset(self) -> Path | None:
        """Archive ``report.md`` and clear runtime state without deleting the folder.

        Symmetric counterpart to ``_reset-job`` (which is destructive): a re-run
        of the same goal preserves the prior report under ``archive/`` so the
        operator can ``research compare`` the runs, then wipes the per-job DB
        rows and resettable subfolders, and finally re-inserts the canonical
        ``jobs`` row from the on-disk ``job.json`` / ``intake.json`` so the
        daemon can run as if this were a fresh job.

        Returns the path to the archived report, or ``None`` if ``report.md``
        did not exist.
        """
        from research_agent.storage.markdown import _rotate_report_to

        archive_dir = self.root / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.root / "report.md"
        archived = _rotate_report_to(archive_dir, report_path, prefix="report-")

        # Wipe DB rows. Order matches cli._reset-job to satisfy FK constraints.
        conn = db.connect(self.db_path)
        try:
            with conn:
                for tbl in (
                    "job_sources",
                    "tasks",
                    "critiques",
                    "findings",
                    "events",
                    "hypotheses",
                    "checkpoints",
                    "syntheses",
                    "llm_calls",
                    "plans",
                ):
                    conn.execute(f"DELETE FROM {tbl} WHERE job_id = ?", (self.id,))
                conn.execute(
                    "DELETE FROM sources WHERE id NOT IN"
                    " (SELECT source_id FROM job_sources)"
                )
                conn.execute("DELETE FROM jobs WHERE id = ?", (self.id,))
        finally:
            conn.close()

        # Wipe resettable subfolders, leaving archive/ intact.
        import shutil

        for sub in _RESETTABLE_SUBDIRS:
            sub_path = self.root / sub
            if sub_path.exists():
                shutil.rmtree(sub_path)
            sub_path.mkdir()

        # Wipe transient sidecars: events.jsonl, STOP flag, daemon.pid.
        for sidecar in ("events.jsonl", "STOP", "daemon.pid"):
            try:
                (self.root / sidecar).unlink()
            except FileNotFoundError:
                pass
        _atomic_write_text(self.root / "events.jsonl", "")

        # Re-insert jobs row from on-disk metadata.
        intake = json.loads((self.root / "intake.json").read_text(encoding="utf-8"))
        meta = json.loads((self.root / "job.json").read_text(encoding="utf-8"))

        now = _now_epoch()
        new_status = "pending"
        new_completion_reason: str | None = None
        domain = meta.get("domain") or intake.get("domain")
        intake_json = json.dumps(intake, sort_keys=True)

        conn = db.connect(self.db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        id, goal, domain, status, intake_json,
                        time_cap_hours, budget_cap_usd, aggressiveness,
                        created_at, last_activity_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.id,
                        self.goal,
                        domain,
                        new_status,
                        intake_json,
                        intake.get("time_cap_hours"),
                        intake.get("budget_cap_usd"),
                        intake.get("aggressiveness"),
                        self.created_at,
                        now,
                    ),
                )
        finally:
            conn.close()

        # Mirror reset state into job.json so disk-only consumers see it.
        meta["status"] = new_status
        meta["last_activity_at"] = now
        meta.pop("completion_reason", None)
        _atomic_write_json(self.root / "job.json", meta)

        self.status = new_status
        self.completion_reason = new_completion_reason
        self.intake = intake

        return archived

    @classmethod
    def find_by_goal_slug(
        cls,
        goal: str,
        *,
        jobs_root: Path | str = DEFAULT_JOBS_ROOT,
        db_path: Path | str = db.DEFAULT_DB_PATH,
    ) -> Job | None:
        """Resolve ``goal`` to the newest existing job folder with that slug.

        Uses the same ``_slugify`` rule as :meth:`Job.create`. Folder-scans
        ``jobs_root`` for any ``YYYY-MM-DD-<slug>`` whose slug exactly matches
        and returns the newest one (by ``created_at`` from its ``job.json``).
        Returns ``None`` if nothing matches or the jobs root is missing.
        """
        if not isinstance(goal, str) or not goal.strip():
            return None
        try:
            slug = _slugify(goal)
        except ValueError:
            return None

        jobs_root_p = Path(jobs_root)
        if not jobs_root_p.is_dir():
            return None

        candidates: list[tuple[int, str]] = []
        for child in jobs_root_p.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if not _JOB_ID_RE.match(name):
                continue
            # Folder name format is YYYY-MM-DD-<slug>.
            if name[11:] != slug:
                continue
            job_json = child / "job.json"
            if not job_json.exists():
                continue
            try:
                meta = json.loads(job_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            created_at = int(meta.get("created_at") or 0)
            candidates.append((created_at, name))

        if not candidates:
            return None

        candidates.sort(reverse=True)
        _, newest_id = candidates[0]
        try:
            return cls.load(newest_id, jobs_root=jobs_root_p, db_path=db_path)
        except (FileNotFoundError, KeyError, ValueError):
            return None


def list_jobs(
    status: str | None = None,
    *,
    db_path: Path | str = db.DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Return job summaries from the ``jobs`` table (DB-only, never folder-scan).

    Per the §4 contract the DB is canonical for cross-job queries; the folders
    can lag (e.g. a folder was archived) and a folder scan would race the
    daemon's writes. Newest first.
    """
    db_path_p = Path(db_path)
    conn = db.connect(db_path_p)
    try:
        sql = (
            "SELECT id, goal, domain, status, created_at, last_activity_at,"
            " cost_so_far_usd, completion_reason FROM jobs"
        )
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY created_at DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    return [dict(row) for row in rows]


__all__ = [
    "DEFAULT_JOBS_ROOT",
    "INBOX_REPLAN_FILE",
    "KILL_ESCALATION_SECONDS",
    "KILL_POLL_INTERVAL_SECONDS",
    "Job",
    "list_jobs",
]
