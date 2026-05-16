"""Typer entry point — `research ...` command surface (implementation guide §4, §5)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table

import research_agent.api as public_api
from research_agent import __version__, config, doctor, intake
from research_agent import daemon as daemon  # noqa: F401 - tests monkeypatch cli.daemon
from research_agent.errors import InvalidGoal, JobAlreadyRunning, JobNotFound
from research_agent.storage import db
from research_agent.storage.jobs import (
    _JOB_ID_RE,
    DEFAULT_JOBS_ROOT,
    INBOX_REPLAN_FILE,
    Job,
)
from research_agent.ui import render

_LOADED_ENV_FILES = config.load_env()

app = typer.Typer(
    name="research",
    help="Autonomous CLI research agent.",
    no_args_is_help=True,
)

config_app = typer.Typer(
    name="config",
    help="Configuration / state management commands.",
    no_args_is_help=True,
)
app.add_typer(config_app)

inbox_app = typer.Typer(
    name="inbox",
    help="Manage jobs/<id>/inbox/ human-supplied documents.",
    no_args_is_help=True,
)
app.add_typer(inbox_app)


@inbox_app.callback()
def inbox_callback(
    ctx: typer.Context,
    job_id: str = typer.Argument(  # noqa: B008
        ...,
        help="Job id (e.g. 2026-05-02-some-slug).",
    ),
) -> None:
    """Inbox command group for one job."""
    ctx.obj = {"job_id": job_id}


@config_app.command(name="cache-clear")
def cache_clear_command() -> None:
    """Wipe the LLM response cache file."""
    from research_agent.llm.cache import DEFAULT_CACHE_PATH, LLMCache

    LLMCache.wipe_file(DEFAULT_CACHE_PATH)
    typer.echo(f"cleared {DEFAULT_CACHE_PATH}")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


def _split_key_columns(raw: list[str] | None) -> list[str]:
    keys: list[str] = []
    for item in raw or []:
        for part in str(item).split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in keys:
                keys.append(cleaned)
    return keys


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
    disk_cap_gb: float = typer.Option(
        10.0,
        "--disk-cap-gb",
        help="Per-job disk cap in GB (default 10). Source markdown is pruned in"
        " relevance order when usage exceeds the cap.",
    ),
    max_tasks: int = typer.Option(
        None,
        "--max-tasks",
        help="Override the per-job task cap. Useful for short smoke runs"
        " (e.g. --max-tasks 5). Defaults to MAX_TASKS_PER_JOB (10000).",
    ),
    local: bool = typer.Option(
        False,
        "--local",
        help="Route every tier (planner, synth, workers) through LM Studio."
        " Skips the OpenRouter health check and uses config/models.local.yaml."
        " Cost is always $0; useful for validation runs.",
    ),
    translate_non_english: bool = typer.Option(
        False,
        "--translate-non-english",
        help="Translate non-English extracted findings into English mirrors using"
        " the frontier_speed tier. Off by default.",
    ),
    fragments: bool = typer.Option(
        False,
        "--fragments",
        help="Enable experimental section-fragment synthesis for this job."
        " Unset keeps the legacy whole-report synthesizer.",
    ),
    fresh_reset: bool = typer.Option(
        False,
        "--fresh-reset",
        help="Refuse to reuse an existing job folder. Default behavior on a"
        " collision is to archive the prior report.md into archive/ and soft-"
        " reset the existing job (DB rows + non-archive subfolders wiped); use"
        " --fresh-reset to opt out and require a clean slate (which fails with"
        " FileExistsError if the folder still exists — run _reset-job first).",
    ),
    inbox: bool = typer.Option(
        False,
        "--inbox",
        help="Enable jobs/<id>/inbox/ watcher for mid-run document ingest.",
    ),
    input_csv: Path = typer.Option(  # noqa: B008
        None,
        "--input-csv",
        help="Import an existing CSV into jobs/<id>/artifacts/ for enrichment.",
    ),
    artifact_name: str = typer.Option(  # noqa: B008
        "candidates",
        "--artifact",
        help="Artifact name for --input-csv (default: candidates).",
    ),
    key_columns_raw: list[str] = typer.Option(  # noqa: B008
        None,
        "--key",
        help="Key column for --input-csv. Repeat or pass comma-separated names.",
    ),
    target_columns_raw: list[str] = typer.Option(  # noqa: B008
        None,
        "--target-column",
        help=(
            "Column to enrich in --input-csv artifacts. Repeat or pass "
            "comma-separated names. Defaults to any non-key column."
        ),
    ),
    update_existing: bool = typer.Option(  # noqa: B008
        False,
        "--update-existing",
        help="Allow later enrichment to overwrite non-empty cells.",
    ),
    no_overwrite: bool = typer.Option(  # noqa: B008
        False,
        "--no-overwrite",
        help="Explicitly preserve non-empty cells during enrichment (default behavior).",
    ),
) -> None:
    """Register a new research job and spawn its background daemon."""
    if skip_intake:
        if not goal or not goal.strip():
            typer.echo("--goal is required when --skip-intake is set", err=True)
            raise typer.Exit(code=2)
        intake_data: dict[str, object] = {
            "goal": goal.strip(),
            "domain": "general",
            "time_cap_hours": time_cap,
            "budget_cap_usd": budget_usd,
            "disk_cap_gb": disk_cap_gb,
            "translate_non_english": translate_non_english,
            "fragments": fragments,
            "inbox": inbox,
        }
        if corpus:
            intake_data["corpus"] = corpus
    else:
        answers = intake.run_intake(
            corpus=corpus,
            budget_usd=budget_usd,
            time_cap=time_cap,
            fragments=fragments,
        )
        intake_data = {
            **answers,
            "time_cap_hours": answers["time_cap"],
            "budget_cap_usd": answers["budget_usd"],
            "corpus": answers["corpus_path"],
            "disk_cap_gb": disk_cap_gb,
            "translate_non_english": translate_non_english,
            "fragments": bool(answers.get("fragments") or fragments),
            "inbox": inbox,
        }

    if max_tasks is not None:
        if max_tasks < 1:
            typer.echo("--max-tasks must be >= 1", err=True)
            raise typer.Exit(code=2)
        intake_data["max_tasks"] = max_tasks

    key_columns = _split_key_columns(key_columns_raw)
    target_columns = _split_key_columns(target_columns_raw)
    if input_csv is not None:
        if not input_csv.is_file():
            typer.echo(f"--input-csv not found: {input_csv}", err=True)
            raise typer.Exit(code=2)
        if not key_columns:
            typer.echo("--key is required when --input-csv is set", err=True)
            raise typer.Exit(code=2)
        if update_existing and no_overwrite:
            typer.echo("--update-existing conflicts with --no-overwrite", err=True)
            raise typer.Exit(code=2)
        intake_data["input_csv_artifact"] = artifact_name
        intake_data["enrichment"] = {
            "artifact": artifact_name,
            "input_csv": str(input_csv),
            "key_columns": key_columns,
            "target_columns": target_columns,
            "overwrite_non_empty": bool(update_existing and not no_overwrite),
        }

    if local:
        local_cfg = Path("config/models.local.yaml")
        if not local_cfg.exists():
            typer.echo(f"--local requires {local_cfg} (not found)", err=True)
            raise typer.Exit(code=2)
        intake_data["local"] = True

    if fragments:
        intake_data["fragments"] = True

    goal_text = str(intake_data.get("goal") or "").strip()
    try:
        result = public_api.start_job(
            goal_text,
            budget_usd=budget_usd,
            time_cap=time_cap,
            corpus=corpus,
            disk_cap_gb=disk_cap_gb,
            max_tasks=max_tasks,
            local=local,
            translate_non_english=translate_non_english,
            fragments=fragments,
            fresh_reset=fresh_reset,
            inbox=inbox,
            intake=intake_data,
            input_csv=input_csv,
            artifact_name=artifact_name,
            key_columns=key_columns,
            target_columns=target_columns,
            update_existing=bool(update_existing and not no_overwrite),
        )
    except InvalidGoal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except JobAlreadyRunning as exc:
        if fresh_reset and isinstance(exc.__cause__, FileExistsError):
            raise exc.__cause__ from exc
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        if input_csv is not None:
            typer.echo(f"failed to import --input-csv: {exc}", err=True)
        else:
            typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if result.reused:
        if result.archived_report is not None:
            typer.echo(f"archived prior report to {result.archived_report}")
        else:
            typer.echo(f"reusing job {result.job_id} (no prior report.md to archive)")
    typer.echo(
        f"Started job {result.job_id} (daemon pid {result.daemon_pid}). "
        f"Tail logs with: research logs {result.job_id} -f"
    )


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
    jobs = [
        item.model_dump(by_alias=True)
        for item in public_api.list_jobs(status=status)
    ]
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


def _job_inbox_dir(job: Job) -> Path:
    inbox_dir = job.root / "inbox"
    (inbox_dir / "processed").mkdir(parents=True, exist_ok=True)
    return inbox_dir


@inbox_app.command(name="add")
def inbox_add_command(
    ctx: typer.Context,
    file: Path = typer.Argument(  # noqa: B008
        ...,
        help="File to copy into jobs/<id>/inbox/.",
    ),
) -> None:
    """Copy a human-supplied document into a job inbox."""
    job_id = str((ctx.obj or {}).get("job_id") or "")
    job = _load_job_or_exit(job_id)
    source = Path(file)
    if not source.is_file():
        typer.echo(f"file not found: {source}", err=True)
        raise typer.Exit(code=1)
    inbox_dir = _job_inbox_dir(job)
    dest = inbox_dir / source.name
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        shutil.copyfile(source, tmp)
        os.replace(tmp, dest)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    typer.echo(str(dest))


@inbox_app.command(name="list")
def inbox_list_command(
    ctx: typer.Context,
) -> None:
    """List pending and processed files in a job inbox."""
    job_id = str((ctx.obj or {}).get("job_id") or "")
    job = _load_job_or_exit(job_id)
    inbox_dir = _job_inbox_dir(job)
    processed_dir = inbox_dir / "processed"
    table = Table(title=f"Inbox for {job.id}")
    table.add_column("state")
    table.add_column("file")
    table.add_column("bytes", justify="right")

    rows = 0
    for path in sorted(inbox_dir.iterdir()):
        if path.name == "processed" or not path.is_file() or path.name.endswith(".tmp"):
            continue
        table.add_row("pending", path.name, str(path.stat().st_size))
        rows += 1
    for path in sorted(processed_dir.iterdir()) if processed_dir.exists() else []:
        if not path.is_file():
            continue
        table.add_row("processed", path.name, str(path.stat().st_size))
        rows += 1
    if (job.root / INBOX_REPLAN_FILE).exists():
        table.add_row("replan", INBOX_REPLAN_FILE, "")
        rows += 1
    if rows == 0:
        typer.echo(f"no inbox files for job {job.id}")
        return
    Console().print(table)


@app.command(name="status")
def status_command(
    job_id: str = typer.Argument(..., help="Job id (e.g. 2026-05-02-some-slug)."),
    watch: bool = typer.Option(False, "--watch", help="Refresh every 2 seconds."),
) -> None:
    """Show a detailed Rich panel for a single job."""
    console = Console()

    def _panel():
        try:
            data = public_api.get_job_status_detail(job_id)
        except JobNotFound as e:
            typer.echo(f"job not found: {job_id} ({e})", err=True)
            raise typer.Exit(code=1) from e
        return render.render_status_panel(
            data,  # Job-like public status model.
            plan_version=data.plan_version,
            task_counts=data.task_counts,
            cost=data.spent_usd,
            recent_events=data.recent_events,
            budget_cap=data.budget_cap,
            time_cap_hours=data.time_cap_hours,
            started_at=data.started_at,
            eta_seconds=data.eta_seconds,
            current_task=data.current_task,
            completion_reason=data.completion_reason,
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


def _render_hypotheses_block(job: Job) -> str:
    from research_agent.storage import hypotheses as hypotheses_store

    rows = hypotheses_store.latest_for_job(job)
    if not rows:
        return f"# Hypotheses for {job.id}\n\n(no hypotheses recorded)\n"

    table = Table(title=f"Hypotheses for {job.id}")
    table.add_column("id", justify="right")
    table.add_column("status")
    table.add_column("confidence", justify="right")
    table.add_column("supports")
    table.add_column("refutes")
    table.add_column("statement")
    for row in rows:
        table.add_row(
            str(row["id"]),
            str(row["status"]),
            f"{float(row['confidence']):.2f}",
            ",".join(str(x) for x in row.get("supports") or []),
            ",".join(str(x) for x in row.get("refutes") or []),
            str(row["statement"]),
        )
    console = Console(record=True, width=140)
    console.print(table)
    return console.export_text()


@app.command(name="view")
def view_command(
    job_id: str = typer.Argument(..., help="Job id (e.g. 2026-05-02-some-slug)."),
    report: bool = typer.Option(False, "--report", help="View report.md (default)."),
    findings: bool = typer.Option(False, "--findings", help="View the latest finding."),
    sources: bool = typer.Option(False, "--sources", help="View a list of job sources."),
    hypotheses: bool = typer.Option(
        False,
        "--hypotheses",
        help="View the current working hypotheses ledger.",
    ),
) -> None:
    """View a research artifact (report, finding, sources, or hypotheses)."""
    job = _load_job_or_exit(job_id)

    if sum(bool(flag) for flag in (report, findings, sources, hypotheses)) > 1:
        typer.echo("choose only one of --report, --findings, --sources, or --hypotheses", err=True)
        raise typer.Exit(code=2)

    if findings:
        path = _latest_finding_path(job.root)
        if path is None:
            typer.echo(f"no findings present for job {job_id}", err=True)
            raise typer.Exit(code=1)
        items = public_api.get_findings(job_id)
        body = items[-1].body if items else path.read_text(encoding="utf-8")
    elif sources:
        body = _render_sources_block(job)
        path = None
    elif hypotheses:
        body = _render_hypotheses_block(job)
        path = None
    else:  # report (default; --report is treated as the same path)
        _ = report  # accepted explicitly even though it's the default
        path = job.root / "report.md"
        try:
            report_result = public_api.get_report(job_id)
        except JobNotFound as exc:
            typer.echo(f"report.md not present for job {job_id}", err=True)
            raise typer.Exit(code=1) from exc
        body = report_result.report_md
        if job.completion_reason:
            body = f"<!-- completion_reason: {job.completion_reason} -->\n\n{body}"

    editor = os.environ.get("EDITOR")
    if path is not None and editor and sys.stdout.isatty():
        subprocess.run([editor, str(path)], check=False)
        return
    typer.echo(body)


def _print_smoke_result(result) -> None:  # type: ignore[no-untyped-def]
    """Render a single :class:`SmokeResult` to stdout as a key/value list.

    A table-shape was rejected because Rich truncates the output cell when
    the terminal is narrower than the row — operators reading CI logs would
    see ``skipped: visi…`` and miss the reason. A flat key/value listing
    keeps every field grep-able regardless of width.
    """
    console = Console()
    if result.skipped_reason is not None:
        output_line = f"output: skipped: {result.skipped_reason}"
    else:
        preview = result.output[:200] + ("…" if len(result.output) > 200 else "")
        output_line = f"output: {preview or '(empty)'}"

    lines = [
        f"tier: {result.tier}",
        f"provider: {result.provider}",
        f"model: {result.model}",
        output_line,
        f"input_tokens: {result.input_tokens}",
        f"output_tokens: {result.output_tokens}",
        f"cost_usd: ${result.cost_usd:.4f}",
    ]
    for line in lines:
        console.print(line, highlight=False)


@app.command(name="_reset-job", hidden=True)
def reset_job_command(
    job_id: str = typer.Argument(..., help="Job id to wipe (folder + all DB rows)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Hidden dev helper: nuke a job's folder + every DB row that references it.

    Includes ``sources`` and ``job_sources`` so re-running the same goal in
    dev does not trip the cross-job dedup → "source not found" failure mode.
    """
    import shutil

    if not yes:
        confirmed = typer.confirm(f"Wipe all state for job {job_id!r}?")
        if not confirmed:
            raise typer.Exit(code=1)

    from research_agent.storage.jobs import DEFAULT_JOBS_ROOT

    folder = Path(DEFAULT_JOBS_ROOT) / job_id
    if folder.exists():
        shutil.rmtree(folder)
        typer.echo(f"removed {folder}")

    conn = db.connect()
    try:
        with conn:
            # Order matters: delete dependent rows first to satisfy FKs.
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
                conn.execute(f"DELETE FROM {tbl} WHERE job_id = ?", (job_id,))
            # ``sources`` is shared across jobs (sha256 dedup). After
            # removing the join rows above, drop any source row that no
            # longer has a job referencing it — keeps the dedup table tight
            # and avoids the "deleted-job-folder leaves orphan rows" trap.
            conn.execute(
                "DELETE FROM sources WHERE id NOT IN"
                " (SELECT source_id FROM job_sources)"
            )
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    finally:
        conn.close()
    typer.echo(f"cleared DB rows for {job_id}")


@app.command(name="_smoke-llm", hidden=True)
def smoke_llm_command(
    tier: str = typer.Argument(..., help="Tier name from config/models.yaml."),
    prompt: str = typer.Argument(..., help="Prompt to send to the tier."),
    image: Path = typer.Option(  # noqa: B008 — Typer captures the default at decoration time
        None,
        "--image",
        help="Path to an image (only meaningful for the vision tier).",
    ),
) -> None:
    """Hidden: smoke-test a single LLM tier end-to-end."""
    import asyncio

    from research_agent.llm.router import load_models_config
    from research_agent.llm.smoke import run_llm_smoke

    cfg = load_models_config(Path("config/models.yaml"))
    result = asyncio.run(run_llm_smoke(tier, prompt, cfg, image_path=image))
    _print_smoke_result(result)
    if not result.ok:
        typer.echo(f"smoke failed: {result.error}", err=True)
        raise typer.Exit(code=1)


@app.command(name="_smoke-tool", hidden=True)
def smoke_tool_command(
    tool_name: str = typer.Argument(..., help="Registered tool name."),
    query: str = typer.Argument(..., help="Query string passed to the tool."),
) -> None:
    """Hidden: smoke-test a single registered tool/connector."""
    from research_agent.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        available = sorted(TOOL_REGISTRY) or ["(none registered yet)"]
        typer.echo(
            f"tool not registered: {tool_name} (available: {available})",
            err=True,
        )
        raise typer.Exit(code=2)

    result = fn(query)
    if isinstance(result, str):
        typer.echo(result)
    else:
        typer.echo(repr(result))


@app.command(name="search")
def search_command(
    query: str = typer.Argument(..., help="Search query string."),
    job: str = typer.Option(None, "--job", help="Scope to a single job id."),
    all_: bool = typer.Option(  # noqa: ARG001 — explicit flag; --all is the default
        False,
        "--all",
        help="Search across all jobs (default when --job is not set).",
    ),
    kind: str = typer.Option(
        "both",
        "--kind",
        help="What to search: findings | sources | both.",
    ),
    fts_only: bool = typer.Option(
        False,
        "--fts-only",
        help="Skip the semantic pass and run FTS5 only (legacy / escape hatch).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of the Rich table.",
    ),
) -> None:
    """Hybrid FTS5 + semantic search over findings and/or sources."""
    import sqlite3

    from research_agent.llm.router import load_models_config
    from research_agent.storage.search import ALLOWED_KINDS

    if kind not in ALLOWED_KINDS:
        typer.echo(
            f"--kind must be one of {list(ALLOWED_KINDS)}; got {kind!r}",
            err=True,
        )
        raise typer.Exit(code=2)

    job_id: str | None = None
    if job is not None:
        _load_job_or_exit(job)
        job_id = job

    try:
        models_cfg = None if fts_only else load_models_config(Path("config/models.yaml"))
        results = [
            item.model_dump()
            for item in public_api.search_findings(
                query,
                job_id=job_id,
                kind=kind,  # type: ignore[arg-type]
                fts_only=fts_only,
                models_config=models_cfg,
            )
        ]
    except sqlite3.OperationalError as e:
        typer.echo(f"FTS5 query error: {e}", err=True)
        raise typer.Exit(code=1) from e

    if json_output:
        typer.echo(render.search_results_to_json(results))
        return

    if not results:
        typer.echo("(no results)")
        return

    Console().print(render.render_search_table(results))


@app.command(name="stop")
def stop_command(
    job_id: str = typer.Argument(..., help="Job id (e.g. 2026-05-02-some-slug)."),
    graceful: bool = typer.Option(
        True,
        "--graceful/--kill",
        help="Graceful stop drops a STOP flag; --kill SIGTERMs then SIGKILLs.",
    ),
) -> None:
    """Stop a running job, gracefully (default) or hard-killing the daemon."""
    try:
        public_api.stop_job(job_id, graceful=graceful)
    except JobNotFound as exc:
        msg = str(exc)
        typer.echo(msg if "daemon PID" in msg else f"job not found: {job_id} ({msg})", err=True)
        raise typer.Exit(code=1) from exc

    if graceful:
        typer.echo("Stop requested; daemon will finish current task and synthesize.")
        return

    typer.echo(f"Killed daemon for job {job_id}.")


@app.command(name="resume")
def resume_command(
    job_id: str = typer.Argument(..., help="Job id (e.g. 2026-05-02-some-slug)."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Resume even if the job is in a terminal state (completed/failed).",
    ),
    replan: bool = typer.Option(
        False,
        "--replan",
        help="Run tactical_replan before resuming the existing queue.",
    ),
    note: str | None = typer.Option(
        None,
        "--note",
        help="Operator hint to include in the resume replan context.",
    ),
) -> None:
    """Restart a stranded job's daemon — checkpoint-restore happens at startup."""
    try:
        result = public_api.resume_job(
            job_id,
            force=force,
            replan=replan,
            note=note,
        )
    except JobAlreadyRunning as exc:
        typer.echo(
            f"job {job_id} is already running (pid file present and process alive)",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    except InvalidGoal as exc:
        typer.echo(str(exc), err=True)
        code = 2 if "--note" in str(exc) else 1
        raise typer.Exit(code=code) from exc
    except JobNotFound as exc:
        typer.echo(f"job not found: {job_id} ({exc})", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Resumed job {job_id} (daemon pid {result.daemon_pid}).")


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


@app.command(name="export")
def export_command(
    job_id: str = typer.Argument(..., help="Job id (e.g. 2026-05-02-some-slug)."),
    zip_: bool = typer.Option(False, "--zip", help="Bundle the job folder into a zip archive."),
    md_bundle: bool = typer.Option(
        False,
        "--md-bundle",
        help="Concatenate report + findings + sources into one markdown file.",
    ),
    csv_artifact: str = typer.Option(
        None,
        "--csv",
        help="Export a named table artifact from jobs/<id>/artifacts/ as CSV.",
    ),
    out: Path = typer.Option(  # noqa: B008 — Typer captures defaults at decoration time
        None,
        "--out",
        help="Output path (file or directory). Defaults to <job-id>.{zip,md} in the cwd.",
    ),
    include_history: bool = typer.Option(
        False,
        "--include-history",
        help="Include report.history/ in the export.",
    ),
) -> None:
    """Export a job as a shareable bundle or table artifact."""
    from research_agent.storage.artifacts import list_artifacts

    selected = sum([bool(zip_), bool(md_bundle), bool(csv_artifact)])
    if selected != 1:
        typer.echo("exactly one of --zip, --md-bundle, or --csv must be set", err=True)
        raise typer.Exit(code=2)

    try:
        result = public_api.export_job(
            job_id,
            zip=zip_,
            md_bundle=md_bundle,
            csv_artifact=csv_artifact,
            out=out,
            include_history=include_history,
        )
    except FileNotFoundError as exc:
        job = _load_job_or_exit(job_id)
        available = [item["name"] for item in list_artifacts(job)]
        suffix = f" Available artifacts: {', '.join(available)}." if available else ""
        typer.echo(f"{exc}.{suffix}", err=True)
        raise typer.Exit(code=1) from exc
    except JobNotFound as exc:
        typer.echo(f"job not found: {job_id} ({exc})", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(result.path)


# ---------------------------------------------------------------------------
# `research compare` (issue #210)
# ---------------------------------------------------------------------------


@dataclass
class ComparisonSummary:
    """Counts + textual deltas for one side of a ``research compare`` run."""

    label: str
    tasks_done: int = 0
    findings: int = 0
    sources: int = 0
    plan_versions: int = 0
    drain_replans: int = 0
    cornerstone_hits: int = 0
    departments: set[str] = field(default_factory=set)
    source_hosts: Counter = field(default_factory=Counter)
    top_cited: list[tuple[int, int]] = field(default_factory=list)
    fragments: dict[str, dict[str, Any]] = field(default_factory=dict)


# Match either ``[1]`` / ``[1, 2]`` (current synthesizer shape) or ``[S1]``
# (legacy / issue #210 example) so the comparator survives a citation-format
# refactor without silent zeroes.
_INLINE_CITE_RE = re.compile(r"\[S?(\d+(?:,\s*S?\d+)*)\]")
_H2_RE = re.compile(r"^##\s+(?!Sources\b|References\b|Bibliography\b)(.+?)\s*$", re.MULTILINE)
_SOURCES_LINE_RE = re.compile(r"^[-*]\s*\[(\d+)\]\s+.*?(https?://\S+)", re.MULTILINE)
# Synthesizer's plan-version banner — "Plan vN" headings appear in report
# sections that summarize the plan rollups.
_PLAN_VERSION_RE = re.compile(r"plan\s*v?(\d+)", re.IGNORECASE)


def _parse_inline_citations(text: str) -> Counter:
    """Return a Counter of source-id -> citation count from inline ``[N]`` markers."""
    counter: Counter = Counter()
    for match in _INLINE_CITE_RE.finditer(text):
        for raw_id in match.group(1).split(","):
            stripped = raw_id.strip().lstrip("S").lstrip("s")
            if stripped.isdigit():
                counter[int(stripped)] += 1
    return counter


def _split_sources_section(text: str) -> str:
    """Return everything after the first ``## Sources`` heading, or ``""``."""
    match = re.search(r"^##\s+Sources\s*$", text, re.MULTILINE)
    if match is None:
        return ""
    return text[match.end():]


def _parse_report_md(text: str) -> dict:
    """Pull departments, source hosts, and top-cited tallies out of a report body."""
    departments = {m.group(1).strip() for m in _H2_RE.finditer(text)}

    sources_section = _split_sources_section(text)
    source_lines = list(_SOURCES_LINE_RE.finditer(sources_section))
    source_ids = {int(m.group(1)) for m in source_lines}
    hosts: Counter = Counter()
    for m in source_lines:
        try:
            host = urlparse(m.group(2)).netloc.lower()
        except ValueError:
            continue
        if host:
            # Strip a leading port if any (parsed netloc may include it).
            hosts[host.split(":", 1)[0]] += 1

    body = text[: _split_sources_section_offset(text)] if sources_section else text
    citations = _parse_inline_citations(body)
    top_cited = sorted(
        ((sid, count) for sid, count in citations.items()),
        key=lambda t: (-t[1], t[0]),
    )[:10]

    return {
        "departments": departments,
        "source_hosts": hosts,
        "source_ids": source_ids,
        "top_cited": top_cited,
        "inline_citation_total": sum(citations.values()),
    }


def _split_sources_section_offset(text: str) -> int:
    match = re.search(r"^##\s+Sources\s*$", text, re.MULTILINE)
    return match.start() if match else len(text)


def _newest_archive_report(job: Job) -> Path | None:
    archive_dir = job.root / "archive"
    if not archive_dir.is_dir():
        return None
    candidates = sorted(archive_dir.glob("report-*.md"))
    return candidates[-1] if candidates else None


def _collect_from_db(job: Job, label: str) -> ComparisonSummary:
    """Build a ``ComparisonSummary`` from live DB counts + (if present) report.md."""
    from research_agent.storage.markdown import fragment_digests

    summary = ComparisonSummary(label=label)
    conn = db.connect(job.db_path)
    try:
        summary.tasks_done = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE job_id = ? AND status = 'done'",
                (job.id,),
            ).fetchone()["c"]
        )
        summary.findings = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM findings WHERE job_id = ?",
                (job.id,),
            ).fetchone()["c"]
        )
        summary.sources = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM job_sources WHERE job_id = ?",
                (job.id,),
            ).fetchone()["c"]
        )
        summary.plan_versions = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM plans WHERE job_id = ?",
                (job.id,),
            ).fetchone()["c"]
        )
        summary.drain_replans = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE job_id = ? AND kind = 'drain_replan'",
                (job.id,),
            ).fetchone()["c"]
        )
        summary.cornerstone_hits = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE job_id = ? AND kind LIKE 'cornerstone%'",
                (job.id,),
            ).fetchone()["c"]
        )
    finally:
        conn.close()

    report_path = job.root / "report.md"
    text: str | None = None
    if report_path.exists():
        text = report_path.read_text(encoding="utf-8")
    else:
        archived = _newest_archive_report(job)
        if archived is not None:
            text = archived.read_text(encoding="utf-8")
    if text:
        parsed = _parse_report_md(text)
        summary.departments = parsed["departments"]
        summary.source_hosts = parsed["source_hosts"]
        summary.top_cited = parsed["top_cited"]
    summary.fragments = fragment_digests(job)
    return summary


def _collect_from_report_md(path: Path, label: str) -> ComparisonSummary:
    """Build a ``ComparisonSummary`` purely from a report.md file on disk.

    Used when the comparator is handed a filesystem path (e.g. an archived
    report from a job whose DB rows have since been wiped). Counts that can't
    be derived from the text — task/plan/drain-replan totals — stay at 0; the
    operator gets findings/sources/departments/host counts from the report.
    """
    summary = ComparisonSummary(label=label)
    if not path.exists():
        raise FileNotFoundError(f"report not found: {path}")
    text = path.read_text(encoding="utf-8")
    parsed = _parse_report_md(text)
    summary.departments = parsed["departments"]
    summary.source_hosts = parsed["source_hosts"]
    summary.top_cited = parsed["top_cited"]
    summary.findings = parsed["inline_citation_total"]
    summary.sources = len(parsed["source_ids"])
    return summary


def _resolve_compare_ref(ref: str, label: str) -> ComparisonSummary:
    """Resolve a ``research compare`` argument to a :class:`ComparisonSummary`.

    Tries job-id resolution first (matches ``_JOB_ID_RE`` and a successful
    ``Job.load``); otherwise treats the value as a filesystem path.
    """
    if _JOB_ID_RE.match(ref):
        try:
            job = Job.load(ref, jobs_root=DEFAULT_JOBS_ROOT, db_path=db.DEFAULT_DB_PATH)
        except (FileNotFoundError, KeyError, ValueError):
            pass
        else:
            return _collect_from_db(job, label)

    path = Path(ref)
    if path.is_dir():
        # Be helpful: accept a job folder path; pick its report.md or newest archive.
        candidate = path / "report.md"
        if not candidate.exists():
            archive_dir = path / "archive"
            archives = sorted(archive_dir.glob("report-*.md")) if archive_dir.is_dir() else []
            if not archives:
                raise FileNotFoundError(f"no report.md or archive/report-*.md in {path}")
            candidate = archives[-1]
        return _collect_from_report_md(candidate, label)
    return _collect_from_report_md(path, label)


@app.command(name="compare")
def compare_command(
    ref_a: str = typer.Argument(..., help="Job id OR path to a report.md (or archived copy)."),
    ref_b: str = typer.Argument(..., help="Job id OR path to a report.md (or archived copy)."),
    side_by_side: bool = typer.Option(
        False,
        "--side-by-side",
        help="Show a unified diff of report bodies in the configured pager"
        " (PAGER, falling back to 'less -R').",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit comparison deltas as JSON for downstream tooling.",
    ),
) -> None:
    """Compare two research runs (counts + textual deltas).

    Each argument is either a live job id or a filesystem path. Paths can
    point at a ``report.md`` directly, at an archived copy under
    ``jobs/<id>/archive/``, or at a job folder (its ``report.md`` or newest
    ``archive/report-*.md`` is used).
    """
    try:
        summary_a = _resolve_compare_ref(ref_a, label=ref_a)
        summary_b = _resolve_compare_ref(ref_b, label=ref_b)
    except FileNotFoundError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e

    if side_by_side:
        import difflib

        text_a = _resolve_report_text(ref_a) or ""
        text_b = _resolve_report_text(ref_b) or ""
        diff = "".join(
            difflib.unified_diff(
                text_a.splitlines(keepends=True),
                text_b.splitlines(keepends=True),
                fromfile=ref_a,
                tofile=ref_b,
                n=3,
            )
        )
        pager_cmd = os.environ.get("PAGER") or "less -R"
        try:
            subprocess.run(pager_cmd, input=diff, text=True, shell=True, check=False)
        except OSError:
            typer.echo(diff)
        return

    if json_output:
        typer.echo(render.comparison_summary_to_json(summary_a, summary_b))
        return

    Console().print(render.render_comparison_summary(summary_a, summary_b))


def _resolve_report_text(ref: str) -> str | None:
    """Pull the report body for a compare ref (job id or path) for diffing."""
    if _JOB_ID_RE.match(ref):
        try:
            job = Job.load(ref, jobs_root=DEFAULT_JOBS_ROOT, db_path=db.DEFAULT_DB_PATH)
        except (FileNotFoundError, KeyError, ValueError):
            return None
        if (job.root / "report.md").exists():
            return (job.root / "report.md").read_text(encoding="utf-8")
        archived = _newest_archive_report(job)
        if archived is not None:
            return archived.read_text(encoding="utf-8")
        return None
    path = Path(ref)
    if path.is_dir():
        if (path / "report.md").exists():
            return (path / "report.md").read_text(encoding="utf-8")
        archive_dir = path / "archive"
        archives = sorted(archive_dir.glob("report-*.md")) if archive_dir.is_dir() else []
        if archives:
            return archives[-1].read_text(encoding="utf-8")
        return None
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


if __name__ == "__main__":
    app()
