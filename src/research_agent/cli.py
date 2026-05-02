"""Typer entry point — `research ...` command surface (implementation guide §4, §5)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.live import Live

from research_agent import __version__, config, doctor
from research_agent.storage import db
from research_agent.storage.jobs import Job, list_jobs
from research_agent.ui import render

_LOADED_ENV_FILES = config.load_env()

app = typer.Typer(
    name="research",
    help="Autonomous CLI research agent.",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(  # noqa: ARG001 — eager callback handles exit
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    """Top-level callback — subcommands land here once registered."""


@app.command(name="doctor")
def doctor_command(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of the Rich table.",
    ),
) -> None:
    """Report environment readiness for the research agent."""
    results = doctor.run_all_checks(_LOADED_ENV_FILES)
    if json_output:
        typer.echo(doctor.emit_json(results, _LOADED_ENV_FILES))
    else:
        doctor.render_table(results)
    if doctor.has_required_failure(results):
        raise typer.Exit(code=1)


@app.command(name="start")
def start_command(
    goal: str = typer.Option(None, "--goal", help="Research goal (required with --skip-intake)."),
    skip_intake: bool = typer.Option(
        False,
        "--skip-intake",
        help="Skip the interactive intake (testing back door for phases 1–4).",
    ),
    budget_usd: float = typer.Option(None, "--budget-usd", help="USD cost cap for the job."),
    time_cap: int = typer.Option(None, "--time-cap", help="Wall-clock cap, in hours."),
    corpus: str = typer.Option(
        None,
        "--corpus",
        help="Path to a local corpus directory to scope the research.",
    ),
) -> None:
    """Register a new research job (does NOT spawn the daemon — that's phase 5)."""
    if not skip_intake:
        typer.echo(
            "interactive intake not yet implemented (phase 5) — use --skip-intake",
            err=True,
        )
        raise typer.Exit(code=2)

    if not goal or not goal.strip():
        typer.echo("--goal is required when --skip-intake is set", err=True)
        raise typer.Exit(code=2)

    intake: dict[str, object] = {
        "goal": goal.strip(),
        "domain": "general",
        "time_cap_hours": time_cap,
        "budget_cap_usd": budget_usd,
    }
    if corpus:
        intake["corpus"] = corpus

    # Make sure the schema exists so the testing back door is self-bootstrapping.
    db.migrate().close()

    job = Job.create(intake)
    typer.echo(f"Started job {job.id} (status: {job.status})")
    typer.echo("note: the daemon is not yet wired up — register-only for now (phase 5)")


@app.command(name="list")
def list_command(
    status: str = typer.Option(None, "--status", help="Filter by job status."),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON. Implied when stdout is not a TTY.",
    ),
) -> None:
    """List research jobs (newest first)."""
    jobs = list_jobs(status=status)
    use_json = json_output or not sys.stdout.isatty()
    if use_json:
        typer.echo(render.jobs_to_json(jobs))
        return
    Console().print(render.render_jobs_table(jobs))


def _load_job_or_exit(job_id: str) -> Job:
    """Load a job or emit a clean ``not found`` error and exit(1)."""
    try:
        return Job.load(job_id)
    except (FileNotFoundError, KeyError, ValueError) as e:
        typer.echo(f"job not found: {job_id} ({e})", err=True)
        raise typer.Exit(code=1) from e


@app.command(name="status")
def status_command(
    job_id: str = typer.Argument(..., help="Job id (e.g. 2026-05-02-some-slug)."),
    watch: bool = typer.Option(False, "--watch", help="Refresh every 2 seconds."),
) -> None:
    """Show a detailed Rich panel for a single job."""
    job = _load_job_or_exit(job_id)
    console = Console()

    def _panel():
        data = render.load_status_data(job)
        return render.render_status_panel(
            job,
            plan_version=data["plan_version"],
            task_counts=data["task_counts"],
            cost=data["cost"],
            recent_events=data["recent_events"],
        )

    if not watch:
        console.print(_panel())
        return

    try:
        with Live(_panel(), console=console, refresh_per_second=4) as live:
            while True:
                time.sleep(2.0)
                live.update(_panel())
    except KeyboardInterrupt:
        return


def _latest_finding_path(root: Path) -> Path | None:
    findings_dir = root / "findings"
    if not findings_dir.is_dir():
        return None
    candidates = sorted(findings_dir.glob("*.md"))
    return candidates[-1] if candidates else None


def _render_sources_block(job: Job) -> str:
    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT s.id, s.url, s.title, s.fetched_at, s.md_path"
            " FROM job_sources js JOIN sources s ON js.source_id = s.id"
            " WHERE js.job_id = ?"
            " ORDER BY s.fetched_at DESC",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return f"# Sources for {job.id}\n\n(no sources recorded)\n"
    lines = [f"# Sources for {job.id}", ""]
    for r in rows:
        title = r["title"] or "(untitled)"
        url = r["url"] or "(no url)"
        lines.append(f"- [{r['id']}] {title} — {url} (fetched {r['fetched_at']}, {r['md_path']})")
    return "\n".join(lines) + "\n"


@app.command(name="view")
def view_command(
    job_id: str = typer.Argument(..., help="Job id (e.g. 2026-05-02-some-slug)."),
    report: bool = typer.Option(False, "--report", help="View report.md (default)."),
    findings: bool = typer.Option(False, "--findings", help="View the latest finding."),
    sources: bool = typer.Option(False, "--sources", help="View a list of job sources."),
) -> None:
    """View a research artifact (report, finding, or sources list)."""
    job = _load_job_or_exit(job_id)

    if findings:
        path = _latest_finding_path(job.root)
        if path is None:
            typer.echo(f"no findings present for job {job_id}", err=True)
            raise typer.Exit(code=1)
        body = path.read_text(encoding="utf-8")
    elif sources:
        body = _render_sources_block(job)
        path = None
    else:  # report (default; --report is treated as the same path)
        _ = report  # accepted explicitly even though it's the default
        path = job.root / "report.md"
        if not path.exists():
            typer.echo(f"report.md not present for job {job_id}", err=True)
            raise typer.Exit(code=1)
        body = path.read_text(encoding="utf-8")

    editor = os.environ.get("EDITOR")
    if path is not None and editor and sys.stdout.isatty():
        subprocess.run([editor, str(path)], check=False)
        return
    typer.echo(body)


@app.command(name="logs")
def logs_command(
    job_id: str = typer.Argument(..., help="Job id (e.g. 2026-05-02-some-slug)."),
    follow: bool = typer.Option(False, "-f", "--follow", help="Tail and follow new events."),
    level: str = typer.Option(None, "--level", help="Filter by event level (e.g. INFO, ERROR)."),
) -> None:
    """Tail a job's events.jsonl. With ``-f`` follows appended events."""
    job = _load_job_or_exit(job_id)
    events_path = job.root / "events.jsonl"
    try:
        for event in render.tail_events_jsonl(events_path, follow=follow, level=level):
            typer.echo(render.format_event_line(event))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    app()
