"""End-to-end tests for the `research` CLI surface."""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from research_agent import __version__, cli, config
from research_agent.storage import db
from research_agent.storage.jobs import Job
from research_agent.ui import render


@pytest.fixture(autouse=True)
def _reset_env_loader(monkeypatch):
    """Force env discovery to start clean for each invocation."""
    for key in (
        "OPENROUTER_API_KEY",
        "RESEARCH_USER_AGENT",
        "RESEARCH_HEADFUL",
        "LMSTUDIO_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    config.reset_for_tests()
    yield
    config.reset_for_tests()


@pytest.fixture
def isolated_repo(tmp_path, monkeypatch):
    """Run the CLI from a tmp dir that contains a minimal valid layout."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("tiers: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # Ensure run_all_checks treats this tmp dir as the repo root.
    monkeypatch.setattr(cli, "_LOADED_ENV_FILES", [])
    return tmp_path


def test_version_flag_prints_version_and_exits_zero():
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_version_short_flag_works():
    runner = CliRunner()
    result = runner.invoke(cli.app, ["-V"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_no_args_shows_help():
    runner = CliRunner()
    result = runner.invoke(cli.app, [])
    # Typer's no_args_is_help convention exits 2 with help on stderr/stdout.
    assert "Usage" in result.stdout or "Usage" in (result.stderr or "")


def test_doctor_json_returns_valid_json_with_required_keys(isolated_repo, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-abcdef0123456789")
    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert {"checks", "loaded_env_files", "ok"} <= payload.keys()
    assert payload["ok"] is True


def test_doctor_json_exit_one_on_required_failure(isolated_repo):
    # No OPENROUTER_API_KEY in env → required env-key check fails.
    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    by_name = {c["name"]: c for c in payload["checks"]}
    assert by_name["env:OPENROUTER_API_KEY"]["status"] == "fail"


def test_doctor_table_does_not_leak_secret(isolated_repo, monkeypatch):
    secret = "sk-or-abcdef0123456789"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0, result.stdout
    assert secret not in result.stdout
    assert "abcdef" not in result.stdout
    # Masked suffix should appear.
    assert "...6789" in result.stdout


def test_doctor_json_does_not_leak_secret(isolated_repo, monkeypatch):
    secret = "sk-or-abcdef0123456789"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout
    assert secret not in result.stdout
    assert "abcdef" not in result.stdout


def test_doctor_optional_missing_does_not_fail(isolated_repo, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-abcdef0123456789")
    # All optional keys remain unset (per autouse fixture).
    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    by_name = {c["name"]: c for c in payload["checks"]}
    assert by_name["env:RESEARCH_HEADFUL"]["status"] == "skip"
    assert by_name["env:RESEARCH_HEADFUL"]["required"] is False


def test_doctor_help_lists_command():
    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor", "--help"])
    assert result.exit_code == 0
    assert "--json" in result.stdout


# ---------------------------------------------------------------------------
# Phase 1 verbs: start / list / status / view / logs
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_jobs_repo(tmp_path, monkeypatch):
    """Tmp cwd with a migrated DB so jobs can be created via the default paths."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_LOADED_ENV_FILES", [])
    db_path = tmp_path / "data" / "index.sqlite"
    db.migrate(path=db_path).close()
    (tmp_path / "jobs").mkdir(exist_ok=True)
    return tmp_path


def _make_synthetic_job(
    repo: Path,
    *,
    goal: str = "Synthetic test goal",
    today: date = date(2026, 5, 2),
    status: str = "pending",
    cost: float = 1.23,
    plan_version: int | None = None,
    finding_text: str | None = None,
    event_lines: list[dict] | None = None,
) -> Job:
    """Hand-create a job, optionally seed plan/finding/events for richer tests."""
    job = Job.create(
        {"goal": goal, "domain": "general"},
        jobs_root=repo / "jobs",
        db_path=repo / "data" / "index.sqlite",
        today=today,
    )
    if status != "pending":
        job.set_status(status)

    conn = db.connect(repo / "data" / "index.sqlite")
    try:
        with conn:
            if cost is not None:
                conn.execute("UPDATE jobs SET cost_so_far_usd = ? WHERE id = ?", (cost, job.id))
            if plan_version is not None:
                conn.execute(
                    "INSERT INTO plans (job_id, version, payload_json, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (job.id, plan_version, "{}", int(time.time())),
                )
    finally:
        conn.close()

    if finding_text is not None:
        finding_path = job.root / "findings" / "000001.md"
        finding_path.write_text(finding_text, encoding="utf-8")

    if event_lines is not None:
        path = job.root / "events.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for ev in event_lines:
                f.write(json.dumps(ev) + "\n")

    return job


def test_start_skip_intake_creates_job(isolated_jobs_repo: Path, monkeypatch):
    captured: dict[str, str] = {}

    def _fake_spawn(job_id: str) -> int:
        captured["job_id"] = job_id
        return 12345

    monkeypatch.setattr(cli.daemon, "spawn_daemon", _fake_spawn)

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["start", "--skip-intake", "--goal", "Investigate widgets", "--budget-usd", "5.0"],
    )
    assert result.exit_code == 0, result.stdout
    assert "Started job" in result.stdout
    assert "(daemon pid 12345)" in result.stdout
    assert "Tail logs with: research logs " in result.stdout

    # Folder + sidecars exist.
    today = time.strftime("%Y-%m-%d")
    job_id = f"{today}-investigate-widgets"
    job_root = isolated_jobs_repo / "jobs" / job_id
    assert (job_root / "job.json").exists()
    assert (job_root / "events.jsonl").exists()

    # spawn_daemon was called with the job we just created.
    assert captured["job_id"] == job_id

    # DB row exists with status=pending.
    conn = db.connect(isolated_jobs_repo / "data" / "index.sqlite")
    try:
        row = conn.execute(
            "SELECT status, budget_cap_usd FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["status"] == "pending"
    assert row["budget_cap_usd"] == 5.0


def test_start_runs_intake_when_not_skipped(isolated_jobs_repo: Path, monkeypatch):
    canned = {
        "goal": "Investigate widgets",
        "goal_one_sentence": "a sourced overview of widget governance",
        "domain": "Corporate / financial",
        "time_cap": 12,
        "budget_usd": 25.0,
        "output_orientation": "internal brief",
        "aggressiveness": "balanced",
        "corpus_path": None,
        "followup_qa": [],
    }

    captured: dict[str, object] = {}

    def _fake_run_intake(*, corpus=None, budget_usd=None, time_cap=None):
        captured["corpus"] = corpus
        captured["budget_usd"] = budget_usd
        captured["time_cap"] = time_cap
        return canned

    monkeypatch.setattr(cli.intake, "run_intake", _fake_run_intake)
    monkeypatch.setattr(cli.daemon, "spawn_daemon", lambda _job_id: 99999)

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["start", "--budget-usd", "25.0", "--time-cap", "12"],
    )
    assert result.exit_code == 0, result.stdout
    assert "Started job" in result.stdout
    assert "(daemon pid 99999)" in result.stdout

    today = time.strftime("%Y-%m-%d")
    job_id = f"{today}-investigate-widgets"
    conn = db.connect(isolated_jobs_repo / "data" / "index.sqlite")
    try:
        row = conn.execute(
            "SELECT status, budget_cap_usd, time_cap_hours FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["status"] == "pending"
    assert row["budget_cap_usd"] == 25.0
    assert row["time_cap_hours"] == 12

    # CLI flags must be forwarded to the intake helper as defaults.
    assert captured["budget_usd"] == 25.0
    assert captured["time_cap"] == 12


def test_start_skip_intake_requires_goal(isolated_jobs_repo: Path):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["start", "--skip-intake"])
    assert result.exit_code != 0


def test_list_emits_json_when_not_a_tty(isolated_jobs_repo: Path):
    _make_synthetic_job(isolated_jobs_repo, goal="alpha", today=date(2026, 5, 1))
    _make_synthetic_job(isolated_jobs_repo, goal="beta", today=date(2026, 5, 2))

    runner = CliRunner()
    # CliRunner stdin/stdout are not TTYs → list should emit JSON implicitly.
    result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    ids = {r["id"] for r in payload}
    assert ids == {"2026-05-02-beta", "2026-05-01-alpha"}


def test_list_explicit_json_flag(isolated_jobs_repo: Path):
    _make_synthetic_job(isolated_jobs_repo)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["list", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert payload[0]["status"] == "pending"


def test_list_status_filter(isolated_jobs_repo: Path):
    j1 = _make_synthetic_job(isolated_jobs_repo, goal="alpha", today=date(2026, 5, 1))
    j2 = _make_synthetic_job(isolated_jobs_repo, goal="beta", today=date(2026, 5, 2))
    j2.set_status("running")

    runner = CliRunner()
    pending = json.loads(runner.invoke(cli.app, ["list", "--status", "pending", "--json"]).stdout)
    running = json.loads(runner.invoke(cli.app, ["list", "--status", "running", "--json"]).stdout)
    assert [r["id"] for r in pending] == [j1.id]
    assert [r["id"] for r in running] == [j2.id]


def test_list_table_renders_for_tty(isolated_jobs_repo: Path):
    """Direct check on the table renderer (TTY branch is hard to simulate)."""
    _make_synthetic_job(isolated_jobs_repo, goal="alpha", today=date(2026, 5, 1))
    from research_agent.storage.jobs import list_jobs

    table = render.render_jobs_table(list_jobs())
    # Rich Table has a row for every job.
    assert table.row_count == 1


def test_status_renders_panel_for_pending_job(isolated_jobs_repo: Path):
    job = _make_synthetic_job(isolated_jobs_repo, goal="Investigate Y", cost=2.5)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["status", job.id])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert job.id in out
    assert "Investigate Y" in out
    assert "pending" in out
    # Plan version 0, no tasks.
    assert "Plan version" in out
    assert "0" in out
    assert "$2.50" in out
    # Phase-7 polish (#41): the panel always exposes ETA + current-task lines.
    assert "ETA" in out
    assert "Current" in out


def _seed_tasks_for_status(
    db_path: Path,
    job_id: str,
    *,
    plan_version: int = 1,
    done_specs: list[tuple[int, int]] | None = None,
    running_kind: str | None = None,
    running_started_at: int | None = None,
    pending_count: int = 0,
) -> None:
    """Insert synthetic ``tasks`` rows so ``load_status_data`` has stuff to chew."""
    conn = db.connect(db_path)
    try:
        with conn:
            for started_at, finished_at in done_specs or []:
                conn.execute(
                    "INSERT INTO tasks"
                    " (job_id, plan_version, kind, payload_json, status,"
                    " started_at, finished_at, retry_count)"
                    " VALUES (?, ?, ?, ?, 'done', ?, ?, 0)",
                    (job_id, plan_version, "web_search", "{}", started_at, finished_at),
                )
            if running_kind is not None:
                conn.execute(
                    "INSERT INTO tasks"
                    " (job_id, plan_version, kind, payload_json, status, started_at, retry_count)"
                    " VALUES (?, ?, ?, ?, 'running', ?, 0)",
                    (job_id, plan_version, running_kind, "{}", running_started_at),
                )
            for _ in range(pending_count):
                conn.execute(
                    "INSERT INTO tasks"
                    " (job_id, plan_version, kind, payload_json, status, retry_count)"
                    " VALUES (?, ?, ?, ?, 'pending', 0)",
                    (job_id, plan_version, "web_search", "{}"),
                )
    finally:
        conn.close()


def test_status_renders_eta_and_current_task(isolated_jobs_repo: Path):
    """ETA derived from finished-task durations × pending count, plus current-task line."""
    job = _make_synthetic_job(isolated_jobs_repo, goal="ETA target", cost=0.0)
    db_path = isolated_jobs_repo / "data" / "index.sqlite"

    now = int(time.time())
    # Three completed tasks at 30s each → avg 30s; two pending → ETA ~60s = "1m".
    done_specs = [
        (now - 1000, now - 970),
        (now - 900, now - 870),
        (now - 800, now - 770),
    ]
    _seed_tasks_for_status(
        db_path,
        job.id,
        done_specs=done_specs,
        running_kind="web_fetch",
        running_started_at=now - 18,
        pending_count=2,
    )

    data = render.load_status_data(job, db_path=db_path)
    assert data["eta_seconds"] == pytest.approx(60.0)
    assert data["current_task"] is not None
    assert data["current_task"]["kind"] == "web_fetch"

    runner = CliRunner()
    result = runner.invoke(cli.app, ["status", job.id])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    # ETA renders as "~1m" via _humanize_duration; current task as the
    # task-kind plus an age suffix.
    assert "~1m" in out
    assert "web_fetch" in out
    # Counter row uses the canonical pending/running/done/failed shape.
    assert "pending=2" in out
    assert "done=3" in out


def test_status_idle_when_no_running_task(isolated_jobs_repo: Path):
    """No running rows → panel renders the explicit ``(idle)`` line."""
    job = _make_synthetic_job(isolated_jobs_repo, goal="idle target")

    runner = CliRunner()
    result = runner.invoke(cli.app, ["status", job.id])
    assert result.exit_code == 0, result.stdout
    assert "(idle)" in result.stdout


def test_status_color_theme_matches_list(isolated_jobs_repo: Path):
    """The status badge uses the same _STATUS_STYLE markup as `research list`."""
    import io

    from rich.console import Console

    job = _make_synthetic_job(isolated_jobs_repo, goal="theme target", status="running")

    data = render.load_status_data(job)
    panel = render.render_status_panel(
        job,
        plan_version=data["plan_version"],
        task_counts=data["task_counts"],
        cost=data["cost"],
        recent_events=data["recent_events"],
        budget_cap=data["budget_cap"],
        eta_seconds=data["eta_seconds"],
        current_task=data["current_task"],
    )
    # Render through a forced-terminal Console so SGR escape codes hit the buffer.
    buf = io.StringIO()
    Console(file=buf, force_terminal=True, color_system="truecolor", width=200).print(panel)
    rendered = buf.getvalue()
    assert "running" in rendered
    # _STATUS_STYLE['running'] = 'green' → the ANSI 32 sequence must appear,
    # matching the same theme `research list` uses for running jobs.
    assert "\x1b[32m" in rendered


def test_status_unknown_job_errors(isolated_jobs_repo: Path):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["status", "2026-05-02-does-not-exist"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "job not found" in combined
    # No Python traceback should leak to the user.
    assert "Traceback" not in combined


def test_view_report_prints_content(isolated_jobs_repo: Path):
    job = _make_synthetic_job(isolated_jobs_repo)
    (job.root / "report.md").write_text("# Final Report\n\nfinding A\n", encoding="utf-8")

    runner = CliRunner()
    # Don't let a real $EDITOR path leak in.
    result = runner.invoke(cli.app, ["view", job.id, "--report"], env={"EDITOR": ""})
    assert result.exit_code == 0, result.stdout
    assert "Final Report" in result.stdout


def test_view_report_default_when_no_flag(isolated_jobs_repo: Path):
    job = _make_synthetic_job(isolated_jobs_repo)
    (job.root / "report.md").write_text("default-report-body", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli.app, ["view", job.id], env={"EDITOR": ""})
    assert result.exit_code == 0, result.stdout
    assert "default-report-body" in result.stdout


def test_view_report_missing_fails_clearly(isolated_jobs_repo: Path):
    job = _make_synthetic_job(isolated_jobs_repo)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["view", job.id, "--report"], env={"EDITOR": ""})
    assert result.exit_code != 0
    assert "report.md" in (result.stdout + (result.stderr or ""))


def test_view_findings_prints_latest(isolated_jobs_repo: Path):
    job = _make_synthetic_job(
        isolated_jobs_repo,
        finding_text="# Finding 1\n\nclaim about widgets\n",
    )
    runner = CliRunner()
    result = runner.invoke(cli.app, ["view", job.id, "--findings"], env={"EDITOR": ""})
    assert result.exit_code == 0, result.stdout
    assert "claim about widgets" in result.stdout


def test_view_findings_when_none_fails(isolated_jobs_repo: Path):
    job = _make_synthetic_job(isolated_jobs_repo)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["view", job.id, "--findings"], env={"EDITOR": ""})
    assert result.exit_code != 0


def test_logs_prints_existing_events(isolated_jobs_repo: Path):
    events = [
        {"ts": 1700000000, "level": "INFO", "kind": "job_started", "actor": "daemon"},
        {"ts": 1700000010, "level": "ERROR", "kind": "fetch_failed", "url": "http://x"},
    ]
    job = _make_synthetic_job(isolated_jobs_repo, event_lines=events)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["logs", job.id])
    assert result.exit_code == 0, result.stdout
    assert "job_started" in result.stdout
    assert "fetch_failed" in result.stdout


def test_logs_level_filter(isolated_jobs_repo: Path):
    events = [
        {"ts": 1700000000, "level": "INFO", "kind": "job_started"},
        {"ts": 1700000010, "level": "ERROR", "kind": "fetch_failed"},
    ]
    job = _make_synthetic_job(isolated_jobs_repo, event_lines=events)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["logs", job.id, "--level", "error"])
    assert result.exit_code == 0, result.stdout
    assert "fetch_failed" in result.stdout
    assert "job_started" not in result.stdout


def test_logs_unknown_job_errors(isolated_jobs_repo: Path):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["logs", "2026-05-02-does-not-exist"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "job not found" in combined
    assert "Traceback" not in combined


def test_view_unknown_job_errors(isolated_jobs_repo: Path):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["view", "2026-05-02-does-not-exist"], env={"EDITOR": ""})
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "job not found" in combined
    assert "Traceback" not in combined


# ---------------------------------------------------------------------------
# stop / resume verbs
# ---------------------------------------------------------------------------


def test_stop_graceful_writes_stop_flag(isolated_jobs_repo: Path):
    job = _make_synthetic_job(isolated_jobs_repo)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["stop", job.id])
    assert result.exit_code == 0, result.stdout
    assert "Stop requested" in result.stdout
    assert (job.root / "STOP").exists()


def test_stop_graceful_is_default_no_kill_called(isolated_jobs_repo: Path, monkeypatch):
    job = _make_synthetic_job(isolated_jobs_repo)
    called = {"n": 0}

    def _kill(self):
        called["n"] += 1

    monkeypatch.setattr(cli.Job, "kill", _kill)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["stop", job.id])
    assert result.exit_code == 0, result.stdout
    assert called["n"] == 0


def test_stop_kill_sends_sigterm_and_removes_pid_file(isolated_jobs_repo: Path, monkeypatch):
    job = _make_synthetic_job(isolated_jobs_repo)
    pid_file = job.root / "daemon.pid"
    pid_file.write_text("99999\n", encoding="utf-8")

    called = {"n": 0}

    def _kill(self):
        called["n"] += 1

    monkeypatch.setattr(cli.Job, "kill", _kill)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["stop", job.id, "--kill"])
    assert result.exit_code == 0, result.stdout
    assert called["n"] == 1
    assert not pid_file.exists()
    assert "Killed daemon" in result.stdout


def test_stop_kill_no_pid_file_errors_clearly(isolated_jobs_repo: Path):
    job = _make_synthetic_job(isolated_jobs_repo)
    # No daemon.pid present — Job.kill raises FileNotFoundError.
    runner = CliRunner()
    result = runner.invoke(cli.app, ["stop", job.id, "--kill"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "no daemon PID file" in combined
    assert "Traceback" not in combined


def test_stop_unknown_job_errors(isolated_jobs_repo: Path):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["stop", "2026-05-02-does-not-exist"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "job not found" in combined
    assert "Traceback" not in combined


def test_resume_refuses_when_daemon_alive(isolated_jobs_repo: Path, monkeypatch):
    job = _make_synthetic_job(isolated_jobs_repo)
    monkeypatch.setattr(cli.daemon, "is_daemon_alive", lambda _job_id: True)
    spawn_called = {"n": 0}
    monkeypatch.setattr(
        cli.daemon,
        "spawn_daemon",
        lambda _job_id: spawn_called.__setitem__("n", spawn_called["n"] + 1) or 1,
    )

    runner = CliRunner()
    result = runner.invoke(cli.app, ["resume", job.id])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "already running" in combined
    assert spawn_called["n"] == 0


def test_resume_refuses_completed_without_force(isolated_jobs_repo: Path, monkeypatch):
    job = _make_synthetic_job(isolated_jobs_repo, status="completed")
    monkeypatch.setattr(cli.daemon, "is_daemon_alive", lambda _job_id: False)
    spawn_called: dict[str, object] = {"job_id": None}

    def _fake_spawn(job_id: str) -> int:
        spawn_called["job_id"] = job_id
        return 4242

    monkeypatch.setattr(cli.daemon, "spawn_daemon", _fake_spawn)

    runner = CliRunner()
    refused = runner.invoke(cli.app, ["resume", job.id])
    assert refused.exit_code == 1
    combined = refused.stdout + (refused.stderr or "")
    assert "completed" in combined
    assert "--force" in combined
    assert spawn_called["job_id"] is None

    forced = runner.invoke(cli.app, ["resume", job.id, "--force"])
    assert forced.exit_code == 0, forced.stdout
    assert spawn_called["job_id"] == job.id
    assert "Resumed job" in forced.stdout
    assert "4242" in forced.stdout


def test_resume_failed_without_force_is_refused(isolated_jobs_repo: Path, monkeypatch):
    job = _make_synthetic_job(isolated_jobs_repo, status="failed")
    monkeypatch.setattr(cli.daemon, "is_daemon_alive", lambda _job_id: False)
    monkeypatch.setattr(cli.daemon, "spawn_daemon", lambda _job_id: 1)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["resume", job.id])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "failed" in combined
    assert "--force" in combined


def test_resume_pending_job_spawns_daemon(isolated_jobs_repo: Path, monkeypatch):
    job = _make_synthetic_job(isolated_jobs_repo)
    monkeypatch.setattr(cli.daemon, "is_daemon_alive", lambda _job_id: False)
    captured: dict[str, object] = {}

    def _fake_spawn(job_id: str) -> int:
        captured["job_id"] = job_id
        return 7777

    monkeypatch.setattr(cli.daemon, "spawn_daemon", _fake_spawn)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["resume", job.id])
    assert result.exit_code == 0, result.stdout
    assert captured["job_id"] == job.id
    assert "7777" in result.stdout


def test_resume_clears_stale_stop_flag_before_spawn(isolated_jobs_repo: Path, monkeypatch):
    """A `stop --graceful` leaves STOP on disk; resume must clear it pre-spawn.

    Otherwise the freshly spawned daemon's first `_should_stop(job)` check sees
    the stale flag and exits before doing any work.
    """
    job = _make_synthetic_job(isolated_jobs_repo)
    (job.root / "STOP").write_text("", encoding="utf-8")

    monkeypatch.setattr(cli.daemon, "is_daemon_alive", lambda _job_id: False)

    spawn_observed: dict[str, bool] = {}

    def _fake_spawn(job_id: str) -> int:
        # The flag must be gone *before* spawn_daemon is called, not after.
        spawn_observed["stop_present"] = (job.root / "STOP").exists()
        return 4321

    monkeypatch.setattr(cli.daemon, "spawn_daemon", _fake_spawn)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["resume", job.id])
    assert result.exit_code == 0, result.stdout
    assert spawn_observed["stop_present"] is False
    assert not (job.root / "STOP").exists()


def test_stop_graceful_then_resume_does_not_duplicate_findings(
    isolated_jobs_repo: Path, monkeypatch
):
    """End-to-end: graceful stop preserves done tasks; resume re-runs none of them."""
    import asyncio as _asyncio

    from research_agent.orchestrator.loop import run_loop
    from research_agent.orchestrator.plan import Plan, Subgoal, TaskSpec
    from research_agent.storage.markdown import write_plan
    from research_agent.storage.tasks import enqueue

    job = _make_synthetic_job(isolated_jobs_repo, goal="resume target")

    plan = Plan(
        version=1,
        objective="Investigate the target",
        subgoals=[Subgoal(id=1, description="Gather", done=False)],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=3,
    )
    write_plan(job, plan.model_dump())

    enqueue(
        job,
        [TaskSpec(kind="web_search", payload={"q": f"q-{i}"}) for i in range(2)],
        plan_version=1,
    )

    db_path = isolated_jobs_repo / "data" / "index.sqlite"
    now = int(time.time())
    handler_calls = {"n": 0}

    async def _record_finding(j, task):
        handler_calls["n"] += 1
        conn = db.connect(db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO findings"
                    " (job_id, md_path, claim, confidence, source_ids, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        j.id,
                        f"findings/{handler_calls['n']:06d}.md",
                        f"finding from task {task['id']}",
                        0.5,
                        "[]",
                        now,
                    ),
                )
        finally:
            conn.close()
        return {"ok": True}

    _asyncio.run(
        run_loop(
            job,
            router=None,
            plan=plan,
            handlers={"web_search": _record_finding},
            retry_waits=(0,),
        )
    )

    assert handler_calls["n"] == 2

    def _findings_count() -> int:
        conn = db.connect(db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM findings WHERE job_id = ?", (job.id,)
            ).fetchone()
        finally:
            conn.close()
        return int(row["c"])

    assert _findings_count() == 2

    runner = CliRunner()
    stop_res = runner.invoke(cli.app, ["stop", job.id])
    assert stop_res.exit_code == 0, stop_res.stdout
    assert (job.root / "STOP").exists()

    pre_resume_findings = _findings_count()

    monkeypatch.setattr(cli.daemon, "is_daemon_alive", lambda _job_id: False)

    def _fake_spawn(job_id: str) -> int:
        # Synchronously run another loop iteration in-process. The CLI's
        # resume verb is responsible for clearing the stale STOP flag before
        # this point — the loop sees a clean folder.
        assert not (job.root / "STOP").exists()
        _asyncio.run(
            run_loop(
                Job.load(job_id, jobs_root=isolated_jobs_repo / "jobs", db_path=db_path),
                router=None,
                plan=plan,
                handlers={"web_search": _record_finding},
                retry_waits=(0,),
            )
        )
        return 12345

    monkeypatch.setattr(cli.daemon, "spawn_daemon", _fake_spawn)

    resume_res = runner.invoke(cli.app, ["resume", job.id])
    assert resume_res.exit_code == 0, resume_res.stdout

    # Handler must not have been re-invoked for already-completed tasks.
    assert handler_calls["n"] == 2
    assert _findings_count() == pre_resume_findings


# ---------------------------------------------------------------------------
# Pure render helpers
# ---------------------------------------------------------------------------


def test_jobs_to_json_round_trips():
    rows = [{"id": "a", "status": "pending", "goal": "g", "created_at": 1, "cost_so_far_usd": 0.0}]
    payload = json.loads(render.jobs_to_json(rows))
    assert payload == rows


def test_render_jobs_table_includes_reason_column():
    """Issue #39: ``research list`` surfaces ``completion_reason`` as its own column."""
    rows = [
        {
            "id": "2026-05-02-x",
            "status": "completed",
            "goal": "g",
            "created_at": 1,
            "last_activity_at": 2,
            "cost_so_far_usd": 1.5,
            "completion_reason": "budget_cap",
        },
    ]
    table = render.render_jobs_table(rows)
    headers = [str(c.header) for c in table.columns]
    assert "reason" in headers


def test_list_jobs_returns_completion_reason_field(isolated_jobs_repo: Path):
    """``list_jobs`` SELECT must include ``completion_reason`` so JSON output carries it."""
    from research_agent.storage.jobs import list_jobs

    job = _make_synthetic_job(isolated_jobs_repo, goal="reason coverage")
    job.set_status("completed", completion_reason="budget_cap")

    rows = list_jobs(db_path=isolated_jobs_repo / "data" / "index.sqlite")
    assert rows
    assert rows[0]["completion_reason"] == "budget_cap"


def test_format_event_line_includes_ts_level_kind():
    line = render.format_event_line(
        {"ts": 1700000000, "level": "INFO", "kind": "job_started", "actor": "daemon"}
    )
    assert "1700000000" in line
    assert "INFO" in line
    assert "job_started" in line
    assert "daemon" in line


# ---------------------------------------------------------------------------
# config cache-clear
# ---------------------------------------------------------------------------


def test_config_cache_clear_removes_cache_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from research_agent.llm.cache import DEFAULT_CACHE_PATH, LLMCache, make_key

    cache_path = tmp_path / DEFAULT_CACHE_PATH
    cache = LLMCache(cache_path)
    cache.put(make_key("p", "m", "x"), "v")
    cache.close()
    assert cache_path.exists()

    runner = CliRunner()
    result = runner.invoke(cli.app, ["config", "cache-clear"])
    assert result.exit_code == 0, result.stdout
    assert "cleared" in result.stdout
    assert not cache_path.exists()


def test_config_cache_clear_is_idempotent_when_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["config", "cache-clear"])
    assert result.exit_code == 0, result.stdout


# ---------------------------------------------------------------------------
# Smoke verbs (_smoke-llm / _smoke-tool) — hidden from --help
# ---------------------------------------------------------------------------


@pytest.fixture
def smoke_repo(tmp_path, monkeypatch):
    """Tmp cwd with a minimal models.yaml so the smoke verbs can load config."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text(
        (
            "tiers:\n"
            "  fast:\n"
            "    provider: lmstudio\n"
            "    model: qwen3-4b\n"
            "    timeout_s: 30\n"
            "  general: { provider: lmstudio, model: x, timeout_s: 30 }\n"
            "  reasoner: { provider: lmstudio, model: x, timeout_s: 30 }\n"
            "  vision: { provider: lmstudio, model: x, timeout_s: 30 }\n"
            "  embeddings: { provider: lmstudio, model: x, timeout_s: 30 }\n"
            "  frontier: { provider: openrouter, model: x, timeout_s: 30 }\n"
            "  frontier_alt: { provider: openrouter, model: x, timeout_s: 30 }\n"
            "  frontier_speed: { provider: openrouter, model: x, timeout_s: 30 }\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_LOADED_ENV_FILES", [])
    return tmp_path


def test_smoke_llm_hidden_from_help(smoke_repo):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "_smoke-llm" not in result.stdout
    assert "_smoke-tool" not in result.stdout


def test_smoke_llm_prints_output_and_exits_zero(smoke_repo, monkeypatch):
    from research_agent.llm.smoke import SmokeResult

    async def _fake_run(tier, prompt, cfg, *, image_path=None):
        return SmokeResult(
            tier=tier,
            provider="lmstudio",
            model="qwen3-4b",
            ok=True,
            output="hi-there",
            input_tokens=11,
            output_tokens=3,
            cost_usd=0.0,
        )

    monkeypatch.setattr("research_agent.llm.smoke.run_llm_smoke", _fake_run)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["_smoke-llm", "fast", "hi"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "hi-there" in out
    assert "fast" in out
    assert "lmstudio" in out
    assert "11" in out
    assert "3" in out
    assert "$0.0000" in out


def test_smoke_llm_failure_exits_one_with_stderr(smoke_repo, monkeypatch):
    from research_agent.llm.smoke import SmokeResult

    async def _fake_run(tier, prompt, cfg, *, image_path=None):
        return SmokeResult(
            tier=tier,
            provider="lmstudio",
            model="qwen3-4b",
            ok=False,
            error="LM Studio not running",
        )

    monkeypatch.setattr("research_agent.llm.smoke.run_llm_smoke", _fake_run)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["_smoke-llm", "fast", "hi"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "LM Studio not running" in combined


def test_smoke_llm_vision_skipped(smoke_repo, monkeypatch):
    from research_agent.llm.smoke import SmokeResult

    async def _fake_run(tier, prompt, cfg, *, image_path=None):
        assert image_path is None
        return SmokeResult(
            tier=tier,
            provider="lmstudio",
            model="qwen3-vl",
            ok=True,
            skipped_reason="vision: no image provided",
        )

    monkeypatch.setattr("research_agent.llm.smoke.run_llm_smoke", _fake_run)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["_smoke-llm", "vision", "describe"])
    assert result.exit_code == 0, result.stdout
    assert "skipped" in result.stdout
    assert "no image provided" in result.stdout


def test_smoke_tool_unknown_exits_two(smoke_repo):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["_smoke-tool", "no_such_tool", "q"])
    assert result.exit_code == 2
    combined = (result.stdout or "") + (result.stderr or "")
    assert "not registered" in combined
    assert "no_such_tool" in combined


def test_smoke_tool_invokes_registered_callable(smoke_repo, monkeypatch):
    from research_agent.tools import TOOL_REGISTRY

    monkeypatch.setitem(TOOL_REGISTRY, "echo", lambda q: f"echo:{q}")
    try:
        runner = CliRunner()
        result = runner.invoke(cli.app, ["_smoke-tool", "echo", "ping"])
        assert result.exit_code == 0, result.stdout
        assert "echo:ping" in result.stdout
    finally:
        TOOL_REGISTRY.pop("echo", None)


# ---------------------------------------------------------------------------
# search verb
# ---------------------------------------------------------------------------


def _seed_search_data(repo: Path) -> tuple[Job, Job]:
    """Create two jobs with seeded findings/sources, then rebuild FTS indexes."""
    job_a = _make_synthetic_job(repo, goal="alpha quantum job", today=date(2026, 5, 1))
    job_b = _make_synthetic_job(repo, goal="beta classical job", today=date(2026, 5, 2))

    now = int(time.time())
    conn = db.connect(repo / "data" / "index.sqlite")
    try:
        with conn:
            conn.execute(
                "INSERT INTO findings"
                " (job_id, md_path, claim, confidence, source_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (job_a.id, "findings/000001.md", "quantum mechanics insight", 0.9, "[]", now),
            )
            conn.execute(
                "INSERT INTO findings"
                " (job_id, md_path, claim, confidence, source_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (job_b.id, "findings/000001.md", "quantum supremacy paper", 0.8, "[]", now),
            )
            conn.execute(
                "INSERT INTO findings"
                " (job_id, md_path, claim, confidence, source_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (job_a.id, "findings/000002.md", "classical mechanics review", 0.6, "[]", now),
            )

            cur = conn.execute(
                "INSERT INTO sources (sha256, url, title, fetched_at, md_path, kind)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("a" * 64, "http://x", "Quantum source title", now, "sources/a.md", "web"),
            )
            sid = cur.lastrowid
            conn.execute(
                "INSERT INTO job_sources (job_id, source_id) VALUES (?, ?)",
                (job_a.id, sid),
            )

            conn.execute("INSERT INTO findings_fts(findings_fts) VALUES('rebuild')")
            conn.execute("INSERT INTO sources_fts(sources_fts) VALUES('rebuild')")
    finally:
        conn.close()

    return job_a, job_b


def test_search_json_emits_results(isolated_jobs_repo: Path):
    _seed_search_data(isolated_jobs_repo)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["search", "quantum", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) >= 1
    expected = {"kind", "score", "job_id", "snippet", "id", "md_path", "title_or_claim"}
    for row in payload:
        assert expected <= row.keys()


def test_search_empty_result_zero_exit(isolated_jobs_repo: Path):
    _seed_search_data(isolated_jobs_repo)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["search", "nonexistenttoken", "--json"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == []


def test_search_kind_flag_filters_output(isolated_jobs_repo: Path):
    _seed_search_data(isolated_jobs_repo)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["search", "quantum", "--kind", "findings", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload
    assert all(r["kind"] == "finding" for r in payload)


def test_search_job_flag_scopes_to_job(isolated_jobs_repo: Path):
    job_a, _job_b = _seed_search_data(isolated_jobs_repo)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["search", "quantum", "--job", job_a.id, "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload
    assert all(r["job_id"] == job_a.id for r in payload)


def test_search_invalid_kind_exit_code_2(isolated_jobs_repo: Path):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["search", "quantum", "--kind", "bogus"])
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "kind" in combined.lower()


def test_search_unknown_job_errors(isolated_jobs_repo: Path):
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["search", "quantum", "--job", "2026-05-02-does-not-exist", "--json"],
    )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "job not found" in combined
    assert "Traceback" not in combined


def test_search_no_results_table_message(isolated_jobs_repo: Path):
    _seed_search_data(isolated_jobs_repo)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["search", "nonexistenttoken"])
    assert result.exit_code == 0, result.stdout
    assert "(no results)" in result.stdout


def test_search_malformed_query_exits_one(isolated_jobs_repo: Path):
    _seed_search_data(isolated_jobs_repo)
    runner = CliRunner()
    # Unbalanced quotes are an FTS5 syntax error.
    result = runner.invoke(cli.app, ["search", '"unterminated', "--json"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "FTS5 query error" in combined
