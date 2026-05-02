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


def test_start_skip_intake_creates_job(isolated_jobs_repo: Path):
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["start", "--skip-intake", "--goal", "Investigate widgets", "--budget-usd", "5.0"],
    )
    assert result.exit_code == 0, result.stdout
    assert "Started job" in result.stdout
    assert "pending" in result.stdout

    # Folder + sidecars exist.
    today = time.strftime("%Y-%m-%d")
    job_id = f"{today}-investigate-widgets"
    job_root = isolated_jobs_repo / "jobs" / job_id
    assert (job_root / "job.json").exists()
    assert (job_root / "events.jsonl").exists()

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


def test_start_without_skip_intake_exits_nonzero(isolated_jobs_repo: Path):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["start", "--goal", "x"])
    assert result.exit_code != 0
    # Combined output should mention the missing flag.
    assert "skip-intake" in (result.stdout + (result.stderr or ""))


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
    result = runner.invoke(
        cli.app, ["view", "2026-05-02-does-not-exist"], env={"EDITOR": ""}
    )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "job not found" in combined
    assert "Traceback" not in combined


# ---------------------------------------------------------------------------
# Pure render helpers
# ---------------------------------------------------------------------------


def test_jobs_to_json_round_trips():
    rows = [{"id": "a", "status": "pending", "goal": "g", "created_at": 1, "cost_so_far_usd": 0.0}]
    payload = json.loads(render.jobs_to_json(rows))
    assert payload == rows


def test_format_event_line_includes_ts_level_kind():
    line = render.format_event_line(
        {"ts": 1700000000, "level": "INFO", "kind": "job_started", "actor": "daemon"}
    )
    assert "1700000000" in line
    assert "INFO" in line
    assert "job_started" in line
    assert "daemon" in line
