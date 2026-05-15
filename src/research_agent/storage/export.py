"""Export helpers for shareable job bundles (issue #42).

Two pure helpers operate on a :class:`Job`:

* :func:`export_zip` — walks ``job.root`` recursively and writes a
  ``ZipFile`` whose entries are rooted at ``<job-id>/<relpath>`` so that
  unzipping yields the full job folder.
* :func:`export_md_bundle` — concatenates intake metadata, ``report.md``,
  every finding (ordered by id), and the source list (with ``archive_url``)
  into a single navigable markdown file.

Both helpers write through the project's atomic-write convention
(``*.tmp`` + :func:`os.replace`) so a tail-watching reader never observes
half-written output.
"""

from __future__ import annotations

import csv
import io
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from research_agent.storage import db
from research_agent.storage.artifacts import read_artifact
from research_agent.storage.jobs import Job

_HISTORY_DIRNAME = "report.history"


def _atomic_replace(tmp_path: Path, final_path: Path) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp_path, final_path)


def export_zip(job: Job, out_path: Path, *, include_history: bool) -> Path:
    """Bundle ``job.root`` into a zip archive at ``out_path``.

    Each archive entry is rooted at ``<job-id>/<relpath>`` so unzipping
    reproduces the full job folder. When ``include_history`` is False,
    paths under ``report.history/`` are skipped.
    """
    out_path = Path(out_path)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(job.root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(job.root)
            if not include_history and rel.parts and rel.parts[0] == _HISTORY_DIRNAME:
                continue
            arcname = f"{job.id}/{rel.as_posix()}"
            zf.write(path, arcname)

    _atomic_replace(tmp_path, out_path)
    return out_path


def _format_intake_block(job: Job) -> str:
    intake = job.intake or {}
    created_iso = datetime.fromtimestamp(job.created_at, UTC).isoformat()
    fields = [
        ("goal", intake.get("goal", job.goal)),
        ("domain", intake.get("domain", job.domain)),
        ("time_cap_hours", intake.get("time_cap_hours")),
        ("budget_cap_usd", intake.get("budget_cap_usd")),
        ("created_at", created_iso),
    ]
    lines = ["---"]
    for key, value in fields:
        lines.append(f"{key}: {value if value is not None else ''}")
    lines.append("---")
    return "\n".join(lines)


def _format_report_section(job: Job) -> str:
    report_path = job.root / "report.md"
    if not report_path.exists():
        return "## Report\n\n(no report.md present)\n"
    body = report_path.read_text(encoding="utf-8").rstrip("\n")
    return f"## Report\n\n{body}\n"


def _format_findings_section(job: Job) -> str:
    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM findings WHERE job_id = ? ORDER BY id ASC",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return "## Findings\n\n(no findings recorded)\n"

    parts = ["## Findings", ""]
    for row in rows:
        fid = int(row["id"])
        md_rel = f"findings/{fid:06d}.md"
        md_path = job.root / md_rel
        body = (
            md_path.read_text(encoding="utf-8").rstrip("\n")
            if md_path.exists()
            else f"(missing {md_rel})"
        )
        parts.append(f"### Finding {fid:06d}")
        parts.append("")
        parts.append(body)
        parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n"


def _format_sources_section(job: Job) -> str:
    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT s.id, s.url, s.title, s.archive_url"
            " FROM job_sources js JOIN sources s ON js.source_id = s.id"
            " WHERE js.job_id = ?"
            " ORDER BY s.id ASC",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return "## Sources\n\n(no sources recorded)\n"

    lines = ["## Sources", ""]
    for r in rows:
        title = r["title"] or "(untitled)"
        url = r["url"] or ""
        archive = r["archive_url"] or "(none)"
        url_md = f"[{url}]({url})" if url else "(no url)"
        lines.append(f"- [{r['id']}] {title} — {url_md} — archive: {archive}")
    return "\n".join(lines) + "\n"


def _format_history_section(job: Job) -> str:
    history_dir = job.root / _HISTORY_DIRNAME
    if not history_dir.is_dir():
        return ""
    files = sorted(p for p in history_dir.iterdir() if p.is_file() and p.suffix == ".md")
    if not files:
        return ""

    parts = ["## Report History", ""]
    for path in files:
        stamp = path.stem
        body = path.read_text(encoding="utf-8").rstrip("\n")
        parts.append(f"### {stamp}")
        parts.append("")
        parts.append(body)
        parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n"


def export_md_bundle(job: Job, out_path: Path, *, include_history: bool) -> Path:
    """Assemble a single navigable markdown bundle for ``job`` at ``out_path``.

    Sections in order: H1 ``# {job.id}`` + intake front matter, ``## Report``,
    ``## Findings`` (one ``### Finding {id:06d}`` per row, ordered by id),
    ``## Sources`` (one bullet per source with ``archive_url``), and — when
    ``include_history`` is True — ``## Report History`` with each archived
    rotation inlined under its UTC timestamp stem.
    """
    out_path = Path(out_path)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sections = [
        f"# {job.id}",
        "",
        _format_intake_block(job),
        "",
        _format_report_section(job),
        _format_findings_section(job),
        _format_sources_section(job),
    ]
    if include_history:
        history = _format_history_section(job)
        if history:
            sections.append(history)

    body = "\n".join(s.rstrip("\n") for s in sections).rstrip("\n") + "\n"
    tmp_path.write_text(body, encoding="utf-8")
    _atomic_replace(tmp_path, out_path)
    return out_path


def export_csv(job: Job, artifact_name: str, out_path: Path) -> Path:
    """Export one table artifact as CSV with schema-defined column ordering."""
    schema, rows = read_artifact(job, artifact_name)
    out_path = Path(out_path)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [column.name for column in schema.columns]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in fieldnames})
    tmp_path.write_text(buffer.getvalue(), encoding="utf-8")
    _atomic_replace(tmp_path, out_path)
    return out_path


__all__ = [
    "export_csv",
    "export_md_bundle",
    "export_zip",
]
