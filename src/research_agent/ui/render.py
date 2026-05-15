"""Rendering helpers for the `research list/status/logs` CLI verbs.

Pure helpers, no Typer dependency, so CLI handlers stay thin and tests can
exercise the table/panel/JSON shapes directly.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from research_agent.storage import db
from research_agent.storage.jobs import Job

_STATUS_STYLE = {
    "running": "green",
    "stopping": "yellow",
    "failed": "red",
}

_GOAL_TRUNCATE = 60


def _humanize_age(now_epoch: int, then_epoch: int | None) -> str:
    if then_epoch is None:
        return "—"
    delta = max(0, int(now_epoch) - int(then_epoch))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _humanize_duration(seconds: float | int | None) -> str:
    """Format a positive duration as ``Xs`` / ``Ym`` / ``Zh`` (closest unit)."""
    if seconds is None:
        return "—"
    delta = max(0, int(seconds))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _hours_to_seconds(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    return hours * 3600 if hours > 0 else None


def _truncate(text: str, limit: int = _GOAL_TRUNCATE) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_cost(value: Any) -> str:
    if value is None:
        return "$0.00"
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _status_cell(status: str) -> str:
    style = _STATUS_STYLE.get(status)
    if style is None:
        return status
    return f"[{style}]{status}[/{style}]"


def render_jobs_table(jobs: list[dict[str, Any]], *, now: int | None = None) -> Table:
    """Render a Rich table for `research list`."""
    now_epoch = int(now) if now is not None else int(time.time())
    table = Table(title="research jobs", show_lines=False)
    table.add_column("id", overflow="fold")
    table.add_column("status")
    table.add_column("reason")
    table.add_column("goal")
    table.add_column("age")
    table.add_column("cost")
    table.add_column("last activity")

    for row in jobs:
        reason = row.get("completion_reason") or ""
        table.add_row(
            str(row.get("id", "")),
            _status_cell(str(row.get("status", ""))),
            str(reason),
            _truncate(str(row.get("goal", ""))),
            _humanize_age(now_epoch, row.get("created_at")),
            _format_cost(row.get("cost_so_far_usd")),
            _humanize_age(now_epoch, row.get("last_activity_at")),
        )
    return table


def jobs_to_json(jobs: list[dict[str, Any]]) -> str:
    """Serialize the list returned by ``list_jobs`` for non-TTY callers."""
    return json.dumps(jobs, indent=2, sort_keys=True, default=str)


_ETA_SAMPLE_LIMIT = 5


def load_status_data(job: Job, *, db_path: Path | None = None) -> dict[str, Any]:
    """Pull plan version, task counts, cost, ETA, current task, and recent events."""
    intake = job.intake or {}
    path = db_path if db_path is not None else job.db_path
    conn = db.connect(path)
    try:
        plan_row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM plans WHERE job_id = ?",
            (job.id,),
        ).fetchone()
        plan_version = int(plan_row["v"]) if plan_row is not None else 0

        task_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks WHERE job_id = ? GROUP BY status",
            (job.id,),
        ).fetchall()
        task_counts: dict[str, int] = {str(r["status"]): int(r["n"]) for r in task_rows}

        job_row = conn.execute(
            "SELECT cost_so_far_usd, time_cap_hours, created_at, started_at, completion_reason"
            " FROM jobs WHERE id = ?",
            (job.id,),
        ).fetchone()
        cost_val = job_row["cost_so_far_usd"] if job_row else None
        cost = float(cost_val) if cost_val else 0.0
        time_cap_hours = (
            job_row["time_cap_hours"]
            if job_row is not None and job_row["time_cap_hours"] is not None
            else intake.get("time_cap_hours")
        )
        started_at_val = job_row["started_at"] if job_row is not None else None
        created_at_val = job_row["created_at"] if job_row is not None else job.created_at
        started_at = int(started_at_val or created_at_val or job.created_at)
        completion_reason = (
            job_row["completion_reason"] if job_row is not None else job.completion_reason
        )

        # Rolling-average ETA from the last few finished tasks. We need both
        # started_at and finished_at populated to derive a duration; tasks
        # missing either are skipped.
        duration_rows = conn.execute(
            "SELECT started_at, finished_at FROM tasks"
            " WHERE job_id = ? AND status = 'done'"
            " AND started_at IS NOT NULL AND finished_at IS NOT NULL"
            " ORDER BY finished_at DESC LIMIT ?",
            (job.id, _ETA_SAMPLE_LIMIT),
        ).fetchall()
        eta_seconds: float | None = None
        if duration_rows:
            durations = [
                max(0, int(r["finished_at"]) - int(r["started_at"])) for r in duration_rows
            ]
            avg = sum(durations) / len(durations)
            pending = task_counts.get("pending", 0)
            if pending > 0 and avg > 0:
                eta_seconds = avg * pending

        current_row = conn.execute(
            "SELECT id, kind, started_at FROM tasks"
            " WHERE job_id = ? AND status = 'running'"
            " ORDER BY started_at DESC LIMIT 1",
            (job.id,),
        ).fetchone()
        current_task: dict[str, Any] | None = None
        if current_row is not None:
            current_task = {
                "id": int(current_row["id"]),
                "kind": str(current_row["kind"]),
                "started_at": (
                    int(current_row["started_at"])
                    if current_row["started_at"] is not None
                    else None
                ),
            }

        event_rows = conn.execute(
            "SELECT ts, level, actor, kind, payload_json FROM events"
            " WHERE job_id = ? ORDER BY ts DESC LIMIT 10",
            (job.id,),
        ).fetchall()
        recent_events: list[dict[str, Any]] = [
            {
                "ts": int(r["ts"]),
                "level": r["level"],
                "actor": r["actor"],
                "kind": r["kind"],
                "payload_json": r["payload_json"],
            }
            for r in event_rows
        ]
    finally:
        conn.close()

    budget_cap = intake.get("budget_cap_usd")

    return {
        "plan_version": plan_version,
        "task_counts": task_counts,
        "cost": cost,
        "budget_cap": budget_cap,
        "time_cap_hours": time_cap_hours,
        "started_at": started_at,
        "completion_reason": completion_reason,
        "eta_seconds": eta_seconds,
        "current_task": current_task,
        "recent_events": recent_events,
    }


def _format_task_counts(task_counts: dict[str, int]) -> str:
    """Render the canonical pending/running/done/failed counter row.

    Always emits all four buckets so the layout doesn't shift between ticks
    of ``--watch``. Running and failed counts get color cues that match the
    job-list theme.
    """
    pending = int(task_counts.get("pending", 0))
    running = int(task_counts.get("running", 0))
    done = int(task_counts.get("done", 0))
    failed = int(task_counts.get("failed", 0))
    running_cell = f"[{_STATUS_STYLE['running']}]running={running}[/{_STATUS_STYLE['running']}]"
    failed_cell = f"[{_STATUS_STYLE['failed']}]failed={failed}[/{_STATUS_STYLE['failed']}]"
    return f"pending={pending} {running_cell} done={done} {failed_cell}"


def render_status_panel(
    job: Job,
    plan_version: int,
    task_counts: dict[str, int],
    cost: float,
    recent_events: list[dict[str, Any]],
    *,
    budget_cap: float | None = None,
    time_cap_hours: float | None = None,
    started_at: int | None = None,
    eta_seconds: float | None = None,
    current_task: dict[str, Any] | None = None,
    completion_reason: str | None = None,
    now: int | None = None,
) -> Panel:
    """Render the detail panel for `research status <job-id>`."""
    intake = job.intake or {}
    now_epoch = int(now) if now is not None else int(time.time())
    cap_for_display = budget_cap if budget_cap is not None else intake.get("budget_cap_usd")
    time_cap_for_display = (
        time_cap_hours if time_cap_hours is not None else intake.get("time_cap_hours")
    )
    elapsed_start = int(started_at) if started_at is not None else job.created_at
    elapsed_seconds = max(0, now_epoch - elapsed_start)
    time_cap_seconds = _hours_to_seconds(time_cap_for_display)

    summary_lines = [
        f"[bold]Status:[/bold] {_status_cell(job.status)}",
        f"[bold]Goal:[/bold] {escape(job.goal)}",
        f"[bold]Domain:[/bold] {escape(job.domain or '—')}",
        f"[bold]Plan version:[/bold] {plan_version}",
        f"[bold]Cost so far:[/bold] {_format_cost(cost)} / {_format_cost(cap_for_display)}",
        f"[bold]Tasks:[/bold] {_format_task_counts(task_counts)}",
        (
            f"[bold]Elapsed / time cap:[/bold] {_humanize_duration(elapsed_seconds)} / "
            f"{_humanize_duration(time_cap_seconds)}"
        ),
        f"[bold]ETA:[/bold] ~{_humanize_duration(eta_seconds)}",
    ]
    reason = completion_reason or job.completion_reason
    if reason:
        summary_lines.insert(1, f"[bold]Completion reason:[/bold] {escape(str(reason))}")

    if current_task is not None:
        kind = escape(str(current_task.get("kind") or "?"))
        started_at = current_task.get("started_at")
        age = _humanize_age(now_epoch, started_at) if started_at is not None else "—"
        summary_lines.append(f"[bold]Current:[/bold] {kind} (running {age})")
    else:
        summary_lines.append("[bold]Current:[/bold] (idle)")

    summary = "\n".join(summary_lines)

    events_table = Table(title="Recent events", show_header=True, expand=False)
    events_table.add_column("ts")
    events_table.add_column("level")
    events_table.add_column("actor")
    events_table.add_column("kind")
    if recent_events:
        for ev in recent_events:
            events_table.add_row(
                str(ev.get("ts", "")),
                str(ev.get("level", "")),
                str(ev.get("actor") or "—"),
                str(ev.get("kind", "")),
            )
    else:
        events_table.add_row("—", "—", "—", "(no events yet)")

    body = Group(Text.from_markup(summary), events_table)
    return Panel(body, title=f"job {job.id}", border_style="cyan")


def tail_events_jsonl(
    path: Path,
    *,
    follow: bool = False,
    level: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield parsed events from ``events.jsonl``.

    When ``follow`` is True, blocks waiting for appended lines using
    :mod:`watchfiles`. Otherwise reads the existing file once and returns.
    """
    level_norm = level.upper() if level else None

    def _emit_from(pos: int) -> tuple[list[dict[str, Any]], int]:
        if not path.exists():
            return [], pos
        with path.open("rb") as f:
            f.seek(pos)
            data = f.read()
            new_pos = f.tell()
        out: list[dict[str, Any]] = []
        text = data.decode("utf-8", errors="replace")
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if level_norm and str(obj.get("level", "")).upper() != level_norm:
                continue
            out.append(obj)
        return out, new_pos

    events, offset = _emit_from(0)
    for ev in events:
        yield ev

    if not follow:
        return

    from watchfiles import watch  # local import — only paid when --follow used

    for _changes in watch(path):
        events, offset = _emit_from(offset)
        for ev in events:
            yield ev


def _format_score(value: Any, *, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def render_search_table(results: list[dict[str, Any]]) -> Table:
    """Render a Rich table for `research search` results.

    Hybrid results expose ``fts_score`` / ``cosine_score`` alongside the
    fused ``score``; if any row carries those keys we surface them as
    additional columns so operators can see why an item ranked where it did.
    """
    show_components = any(("fts_score" in row) or ("cosine_score" in row) for row in results)

    table = Table(title="search results", show_lines=False)
    table.add_column("score")
    if show_components:
        table.add_column("fts")
        table.add_column("cosine")
    table.add_column("kind")
    table.add_column("job_id", overflow="fold")
    table.add_column("snippet", overflow="fold")
    for row in results:
        snippet = str(row.get("snippet") or "")
        snippet = snippet.replace("[", "[bold yellow]").replace("]", "[/]")
        cells = [_format_score(row.get("score"), digits=4)]
        if show_components:
            cells.append(_format_score(row.get("fts_score")))
            cells.append(_format_score(row.get("cosine_score")))
        cells.extend(
            [
                str(row.get("kind", "")),
                str(row.get("job_id") or "—"),
                snippet,
            ]
        )
        table.add_row(*cells)
    return table


def search_results_to_json(results: list[dict[str, Any]]) -> str:
    """Serialize search results for `--json` callers."""
    return json.dumps(results, indent=2, sort_keys=True, default=str)


def _delta_cell(a: int, b: int) -> str:
    """Return ``+N`` / ``-N`` / ``±0`` with a color cue for the delta column."""
    delta = b - a
    if delta > 0:
        return f"[green]+{delta}[/green]"
    if delta < 0:
        return f"[red]{delta}[/red]"
    return "±0"


def render_comparison_summary(summary_a: Any, summary_b: Any) -> Group:
    """Render a Rich group for ``research compare`` covering counts + deltas.

    Mirrors the issue #210 example output: a count table with ``a`` / ``b`` /
    ``delta`` columns, then department-coverage and source-host delta lines.
    Both summaries are :class:`research_agent.cli.ComparisonSummary` instances
    (typed as ``Any`` here so this module stays pure-rendering and avoids a
    circular import).
    """
    table = Table(title="research compare", show_lines=False)
    table.add_column("metric")
    table.add_column(getattr(summary_a, "label", "a"), justify="right")
    table.add_column(getattr(summary_b, "label", "b"), justify="right")
    table.add_column("delta", justify="right")

    metric_specs = (
        ("Tasks done", "tasks_done"),
        ("Findings", "findings"),
        ("Sources", "sources"),
        ("Plan versions", "plan_versions"),
        ("Drain-replans", "drain_replans"),
        ("Cornerstone hits", "cornerstone_hits"),
    )
    for label, attr in metric_specs:
        a_val = int(getattr(summary_a, attr) or 0)
        b_val = int(getattr(summary_b, attr) or 0)
        table.add_row(label, str(a_val), str(b_val), _delta_cell(a_val, b_val))

    dept_a = set(getattr(summary_a, "departments", set()) or set())
    dept_b = set(getattr(summary_b, "departments", set()) or set())
    only_b = sorted(dept_b - dept_a)
    only_a = sorted(dept_a - dept_b)
    dept_lines: list[str] = ["", "[bold]Department coverage delta[/bold]"]
    if only_b:
        dept_lines.append("  [green]+ " + ", ".join(only_b) + "[/green]")
    if only_a:
        dept_lines.append("  [red]- " + ", ".join(only_a) + "[/red]")
    if not only_a and not only_b:
        dept_lines.append("  (no change)")

    hosts_a = dict(getattr(summary_a, "source_hosts", {}) or {})
    hosts_b = dict(getattr(summary_b, "source_hosts", {}) or {})
    host_keys = set(hosts_a) | set(hosts_b)
    host_deltas = sorted(
        ((host, int(hosts_b.get(host, 0)) - int(hosts_a.get(host, 0))) for host in host_keys),
        key=lambda t: (-abs(t[1]), t[0]),
    )
    host_deltas = [(h, d) for h, d in host_deltas if d != 0][:10]
    host_lines: list[str] = ["", "[bold]Source-host delta (top 10 by magnitude)[/bold]"]
    if host_deltas:
        for host, delta in host_deltas:
            color = "green" if delta > 0 else "red"
            sign = "+" if delta > 0 else ""
            host_lines.append(f"  [{color}]{sign}{delta}[/{color}] {host}")
    else:
        host_lines.append("  (no change)")

    body = "\n".join(dept_lines + host_lines)
    return Group(table, body)


def comparison_summary_to_json(summary_a: Any, summary_b: Any) -> str:
    """Serialize two ``ComparisonSummary`` instances + deltas as JSON."""

    def _as_dict(s: Any) -> dict[str, Any]:
        return {
            "label": getattr(s, "label", None),
            "tasks_done": int(getattr(s, "tasks_done", 0) or 0),
            "findings": int(getattr(s, "findings", 0) or 0),
            "sources": int(getattr(s, "sources", 0) or 0),
            "plan_versions": int(getattr(s, "plan_versions", 0) or 0),
            "drain_replans": int(getattr(s, "drain_replans", 0) or 0),
            "cornerstone_hits": int(getattr(s, "cornerstone_hits", 0) or 0),
            "departments": sorted(getattr(s, "departments", set()) or set()),
            "source_hosts": dict(getattr(s, "source_hosts", {}) or {}),
            "top_cited": [list(t) for t in (getattr(s, "top_cited", []) or [])],
        }

    a = _as_dict(summary_a)
    b = _as_dict(summary_b)
    deltas = {
        "tasks_done": b["tasks_done"] - a["tasks_done"],
        "findings": b["findings"] - a["findings"],
        "sources": b["sources"] - a["sources"],
        "plan_versions": b["plan_versions"] - a["plan_versions"],
        "drain_replans": b["drain_replans"] - a["drain_replans"],
        "cornerstone_hits": b["cornerstone_hits"] - a["cornerstone_hits"],
        "departments_added": sorted(set(b["departments"]) - set(a["departments"])),
        "departments_removed": sorted(set(a["departments"]) - set(b["departments"])),
    }
    return json.dumps({"a": a, "b": b, "deltas": deltas}, indent=2, sort_keys=True, default=str)


def format_event_line(event: dict[str, Any]) -> str:
    """One-line printable form of an event for `research logs`."""
    ts = event.get("ts", "?")
    level = str(event.get("level", "?"))
    kind = event.get("kind", "?")
    payload = {k: v for k, v in event.items() if k not in ("ts", "level", "kind")}
    if payload:
        payload_str = json.dumps(payload, sort_keys=True, default=str)
    else:
        payload_str = ""
    return f"{ts} {level:<5} {kind} {payload_str}".rstrip()


__all__ = [
    "comparison_summary_to_json",
    "format_event_line",
    "jobs_to_json",
    "load_status_data",
    "render_comparison_summary",
    "render_jobs_table",
    "render_search_table",
    "render_status_panel",
    "search_results_to_json",
    "tail_events_jsonl",
]
