"""Markdown writers for findings, plans, syntheses, and reports.

Implements the per-job content layout from §4 of the implementation guide:
findings get monotonic six-digit ids (``findings/000001.md`` + sidecar JSON);
plans and syntheses are versioned four-digit (``plan/0001.md``,
``synthesis/0001.md``); ``report.md`` rotates prior copies into
``report.history/<UTC ISO timestamp>.md`` before each new write.

Every write produces both a markdown file (the human-readable content)
and a JSON sidecar (structured metadata mirrored into SQLite). All file
writes go through the atomic ``*.tmp`` + :func:`os.replace` pattern from
§16 to keep tail-watching readers from observing half-written content.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_agent.storage import db
from research_agent.storage.jobs import Job, _atomic_write_json, _atomic_write_text


def _now_epoch() -> int:
    return int(time.time())


def _validate_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"confidence must be a number in [0, 1]; got {value!r}")
    conf = float(value)
    if conf < 0.0 or conf > 1.0:
        raise ValueError(f"confidence must be in [0, 1]; got {conf}")
    return conf


def _validate_source_ids(value: Any) -> list[int]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"source_ids must be a non-empty list of ints; got {value!r}")
    out: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"source_ids must contain ints; got {item!r}")
        out.append(item)
    return out


def _validate_int_id_list(value: Any, *, field_name: str, allow_empty: bool = True) -> list[int]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ValueError(f"{field_name} must be a list of ints; got {value!r}")
    out: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"{field_name} must contain ints; got {item!r}")
        out.append(item)
    return out


def _render_finding_md(
    *,
    finding_id: int,
    claim: str,
    confidence: float,
    source_ids: list[int],
    contradicts: list[int] | None,
    tags: list[str] | None,
    target_fragments: list[str] | None,
) -> str:
    contradicts_str = ", ".join(str(i) for i in contradicts) if contradicts else "—"
    tags_str = ", ".join(tags) if tags else "—"
    fragments_str = ", ".join(target_fragments) if target_fragments else "—"
    sources_str = ", ".join(str(i) for i in source_ids)
    return (
        f"# Finding {finding_id:06d}\n"
        f"\n"
        f"**Confidence:** {confidence}\n"
        f"**Sources:** {sources_str}\n"
        f"**Contradicts:** {contradicts_str}\n"
        f"**Tags:** {tags_str}\n"
        f"**Fragments:** {fragments_str}\n"
        f"\n"
        f"---\n"
        f"\n"
        f"{claim.strip()}\n"
    )


def write_finding(
    job: Job,
    claim: str,
    confidence: float,
    source_ids: list[int],
    contradicts: list[int] | None = None,
    tags: list[str] | None = None,
    target_fragments: list[str] | None = None,
) -> int:
    """Write a finding's md + json sidecar and insert the ``findings`` row.

    The autoincrement id from the DB drives the zero-padded filename
    (``findings/{id:06d}.md``) so a future UI can deep-link.
    """
    if not isinstance(claim, str) or not claim.strip():
        raise ValueError("claim must be a non-empty string")
    conf = _validate_confidence(confidence)
    sids = _validate_source_ids(source_ids)
    contradicts_list = list(contradicts) if contradicts else None
    tags_list = list(tags) if tags else None
    target_fragment_list: list[str] | None
    if target_fragments is None:
        target_fragment_list = None
    else:
        from research_agent.orchestrator.synth import normalize_fragment_tags

        target_fragment_list = normalize_fragment_tags(target_fragments, job=job)

    now = _now_epoch()
    source_ids_json = json.dumps(sids)
    contradicts_json = json.dumps(contradicts_list) if contradicts_list is not None else None
    tags_json = json.dumps(tags_list) if tags_list is not None else None
    target_fragments_json = (
        json.dumps(target_fragment_list) if target_fragment_list is not None else None
    )

    conn = db.connect(job.db_path)
    try:
        with conn:
            cur = conn.execute(
                """
                INSERT INTO findings (
                    job_id, md_path, claim, confidence,
                    source_ids, contradicts, tags, target_fragments, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    "",
                    claim,
                    conf,
                    source_ids_json,
                    contradicts_json,
                    tags_json,
                    target_fragments_json,
                    now,
                ),
            )
            assert cur.lastrowid is not None
            finding_id = int(cur.lastrowid)
            md_rel = f"findings/{finding_id:06d}.md"
            json_rel = f"findings/{finding_id:06d}.json"

            md_body = _render_finding_md(
                finding_id=finding_id,
                claim=claim,
                confidence=conf,
                source_ids=sids,
                contradicts=contradicts_list,
                tags=tags_list,
                target_fragments=target_fragment_list,
            )
            sidecar = {
                "id": finding_id,
                "claim": claim,
                "confidence": conf,
                "source_ids": sids,
                "contradicts": contradicts_list,
                "tags": tags_list,
                "target_fragments": target_fragment_list,
                "md_path": md_rel,
                "created_at": now,
            }
            _atomic_write_text(job.root / md_rel, md_body)
            _atomic_write_json(job.root / json_rel, sidecar)

            conn.execute(
                "UPDATE findings SET md_path = ? WHERE id = ?",
                (md_rel, finding_id),
            )
    finally:
        conn.close()

    return finding_id


def write_finding_translation(
    job: Job,
    *,
    finding_id: int,
    translated_body: str,
    source_lang: str,
    target_lang: str = "en",
) -> Path:
    """Write ``findings/NNNNNN.translation.md`` beside the original finding."""
    if isinstance(finding_id, bool) or not isinstance(finding_id, int) or finding_id < 1:
        raise ValueError(f"finding_id must be a positive int; got {finding_id!r}")
    if not isinstance(translated_body, str) or not translated_body.strip():
        raise ValueError("translated_body must be a non-empty string")
    if not isinstance(source_lang, str) or not source_lang.strip():
        raise ValueError("source_lang must be a non-empty string")
    if not isinstance(target_lang, str) or not target_lang.strip():
        raise ValueError("target_lang must be a non-empty string")

    rel = f"findings/{finding_id:06d}.translation.md"
    body = (
        "---\n"
        f"source_lang: {source_lang.strip()}\n"
        f"target_lang: {target_lang.strip()}\n"
        "---\n\n"
        f"{translated_body.strip()}\n"
    )
    path = job.root / rel
    _atomic_write_text(path, body)
    return path


def _next_version(conn: Any, table: str, job_id: str) -> int:
    row = conn.execute(
        f"SELECT COALESCE(MAX(version), 0) + 1 AS next FROM {table} WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return int(row["next"])


def _next_fragment_version(conn: Any, job_id: str, section_id: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(version), 0) + 1 AS next
        FROM fragments
        WHERE job_id = ? AND section_id = ?
        """,
        (job_id, section_id),
    ).fetchone()
    return int(row["next"])


def _render_plan_md(version: int, payload: dict[str, Any], created_at: int) -> str:
    pretty = json.dumps(payload, indent=2, sort_keys=True)
    return (
        f"# Plan v{version:04d}\n"
        f"\n"
        f"Created: {datetime.fromtimestamp(created_at, UTC).isoformat()}\n"
        f"\n"
        f"```json\n"
        f"{pretty}\n"
        f"```\n"
    )


def write_plan(job: Job, payload: dict[str, Any]) -> int:
    """Write the next plan version (md + json) and insert into ``plans``."""
    if not isinstance(payload, dict):
        raise ValueError(f"payload must be a dict; got {type(payload).__name__}")

    now = _now_epoch()
    payload_json = json.dumps(payload, sort_keys=True)

    conn = db.connect(job.db_path)
    try:
        with conn:
            version = _next_version(conn, "plans", job.id)
            md_rel = f"plan/{version:04d}.md"
            json_rel = f"plan/{version:04d}.json"

            _atomic_write_text(job.root / md_rel, _render_plan_md(version, payload, now))
            _atomic_write_json(job.root / json_rel, payload)

            conn.execute(
                """
                INSERT INTO plans (job_id, version, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (job.id, version, payload_json, now),
            )
    finally:
        conn.close()

    return version


def write_synthesis(
    job: Job,
    content: str,
    model: str,
    cost_usd: float | None = None,
) -> int:
    """Write the next synthesis version (md + json) and insert into ``syntheses``."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty string")
    if not isinstance(model, str) or not model:
        raise ValueError("model must be a non-empty string")
    if cost_usd is not None and (
        isinstance(cost_usd, bool) or not isinstance(cost_usd, (int, float))
    ):
        raise ValueError(f"cost_usd must be a number or None; got {cost_usd!r}")

    now = _now_epoch()
    cost = float(cost_usd) if cost_usd is not None else None

    conn = db.connect(job.db_path)
    try:
        with conn:
            version = _next_version(conn, "syntheses", job.id)
            md_rel = f"synthesis/{version:04d}.md"
            json_rel = f"synthesis/{version:04d}.json"

            md_body = content if content.endswith("\n") else content + "\n"
            _atomic_write_text(job.root / md_rel, md_body)
            _atomic_write_json(
                job.root / json_rel,
                {
                    "version": version,
                    "model": model,
                    "cost_usd": cost,
                    "created_at": now,
                },
            )

            conn.execute(
                """
                INSERT INTO syntheses (job_id, version, md_path, model, cost_usd, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job.id, version, md_rel, model, cost, now),
            )
    finally:
        conn.close()

    return version


def _fragment_spec(section_id: str) -> Any:
    from research_agent.orchestrator.fragments import get_fragment

    try:
        return get_fragment(section_id)
    except KeyError as exc:
        raise ValueError(f"unknown fragment section_id: {section_id!r}") from exc


def write_fragment(
    job: Job,
    section_id: str,
    content: str,
    *,
    source_finding_ids: list[int],
    cited_source_ids: list[int] | None = None,
    synthesis_version: int | None = None,
    model: str | None = None,
    tier: str | None = None,
    confidence: float | None = None,
    status: str = "ok",
) -> int:
    """Write the next version of a section-level synthesis fragment."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty string")
    fragment = _fragment_spec(section_id)
    finding_ids = _validate_int_id_list(source_finding_ids, field_name="source_finding_ids")
    cited_ids = (
        _validate_int_id_list(cited_source_ids, field_name="cited_source_ids")
        if cited_source_ids is not None
        else None
    )
    if synthesis_version is not None and (
        isinstance(synthesis_version, bool)
        or not isinstance(synthesis_version, int)
        or synthesis_version < 1
    ):
        raise ValueError(
            "synthesis_version must be a positive int or None; "
            f"got {synthesis_version!r}"
        )
    for field_name, value in (("model", model), ("tier", tier), ("status", status)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{field_name} must be a non-empty string")
    if confidence is not None:
        confidence = _validate_confidence(confidence)

    now = _now_epoch()
    md_body = content if content.endswith("\n") else content + "\n"
    source_finding_ids_json = json.dumps(finding_ids)
    cited_source_ids_json = json.dumps(cited_ids) if cited_ids is not None else None

    conn = db.connect(job.db_path)
    try:
        with conn:
            version = _next_fragment_version(conn, job.id, section_id)
            md_rel = f"fragments/{section_id}/{version:04d}.md"
            json_rel = f"fragments/{section_id}/{version:04d}.json"
            sidecar = {
                "job_id": job.id,
                "section_id": section_id,
                "title": fragment.title,
                "version": version,
                "md_path": md_rel,
                "json_path": json_rel,
                "synthesis_version": synthesis_version,
                "source_finding_ids": finding_ids,
                "cited_source_ids": cited_ids,
                "model": model,
                "tier": tier,
                "confidence": confidence,
                "status": status,
                "created_at": now,
            }

            _atomic_write_text(job.root / md_rel, md_body)
            _atomic_write_json(job.root / json_rel, sidecar)

            conn.execute(
                """
                INSERT INTO fragments (
                    job_id, section_id, version, md_path, json_path,
                    synthesis_version, source_finding_ids, cited_source_ids,
                    model, tier, confidence, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    section_id,
                    version,
                    md_rel,
                    json_rel,
                    synthesis_version,
                    source_finding_ids_json,
                    cited_source_ids_json,
                    model,
                    tier,
                    confidence,
                    status,
                    now,
                ),
            )
    finally:
        conn.close()

    return version


def latest_fragment(job: Job, section_id: str) -> dict[str, Any] | None:
    """Load the latest persisted fragment for one registered section."""
    _fragment_spec(section_id)
    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            """
            SELECT id, job_id, section_id, version, md_path, json_path,
                   synthesis_version, source_finding_ids, cited_source_ids,
                   model, tier, confidence, status, created_at
            FROM fragments
            WHERE job_id = ? AND section_id = ?
            ORDER BY version DESC
            LIMIT 1
            """,
            (job.id, section_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None

    md_path = job.root / row["md_path"]
    if not md_path.exists():
        return None
    item = dict(row)
    item["source_finding_ids"] = (
        json.loads(item["source_finding_ids"]) if item["source_finding_ids"] else []
    )
    item["cited_source_ids"] = (
        json.loads(item["cited_source_ids"]) if item["cited_source_ids"] else None
    )
    item["content"] = md_path.read_text(encoding="utf-8")
    return item


def latest_fragments(job: Job) -> dict[str, dict[str, Any]]:
    """Load latest fragments for every registered section that has content."""
    from research_agent.orchestrator.fragments import all_fragments

    out: dict[str, dict[str, Any]] = {}
    for fragment in all_fragments():
        item = latest_fragment(job, fragment.id)
        if item is not None:
            out[fragment.id] = item
    return out


def _render_failed_synthesis_md(
    *,
    version: int,
    partial_content: str,
    model: str,
    traceback_text: str,
    created_at: int,
) -> str:
    partial = partial_content.strip() or "_No partial output captured._"
    tb = traceback_text.strip() or "No traceback captured."
    return (
        f"# Failed Synthesis v{version:04d}\n"
        f"\n"
        f"Created: {datetime.fromtimestamp(created_at, UTC).isoformat()}\n"
        f"Model: {model}\n"
        f"\n"
        f"## Partial Output\n"
        f"\n"
        f"{partial}\n"
        f"\n"
        f"## Traceback\n"
        f"\n"
        f"```text\n"
        f"{tb}\n"
        f"```\n"
    )


def write_synthesis_failed(
    job: Job,
    partial_content: str,
    *,
    model: str,
    traceback_text: str,
) -> int:
    """Write ``synthesis/<next_version>.failed.md`` for a failed synthesis.

    Failed artifacts stay out of the canonical ``syntheses`` table and do not
    get JSON sidecars. The next successful synthesis can still claim the same
    DB version number while the failure remains on disk for debugging.
    """
    if not isinstance(partial_content, str):
        raise ValueError(
            f"partial_content must be a string; got {type(partial_content).__name__}"
        )
    if not isinstance(model, str) or not model:
        raise ValueError("model must be a non-empty string")
    if not isinstance(traceback_text, str):
        raise ValueError(
            f"traceback_text must be a string; got {type(traceback_text).__name__}"
        )

    now = _now_epoch()
    conn = db.connect(job.db_path)
    try:
        version = _next_version(conn, "syntheses", job.id)
    finally:
        conn.close()

    md_rel = f"synthesis/{version:04d}.failed.md"
    _atomic_write_text(
        job.root / md_rel,
        _render_failed_synthesis_md(
            version=version,
            partial_content=partial_content,
            model=model,
            traceback_text=traceback_text,
            created_at=now,
        ),
    )
    return version


def write_critique(
    job: Job,
    *,
    payload: dict[str, Any],
    content: str,
    model: str,
    cost_usd: float | None = None,
    should_replan: bool = False,
) -> int:
    """Write the next critique version (md + json) and insert into ``critiques``.

    The markdown body is the human-readable summary; the JSON sidecar carries
    the raw structured payload so downstream consumers (planner, future UI)
    can read the gaps/claims/suggestions without re-parsing markdown.
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty string")
    if not isinstance(model, str) or not model:
        raise ValueError("model must be a non-empty string")
    if not isinstance(payload, dict):
        raise ValueError(f"payload must be a dict; got {type(payload).__name__}")
    if cost_usd is not None and (
        isinstance(cost_usd, bool) or not isinstance(cost_usd, (int, float))
    ):
        raise ValueError(f"cost_usd must be a number or None; got {cost_usd!r}")

    now = _now_epoch()
    cost = float(cost_usd) if cost_usd is not None else None
    payload_json = json.dumps(payload, sort_keys=True, default=str)

    conn = db.connect(job.db_path)
    try:
        with conn:
            version = _next_version(conn, "critiques", job.id)
            md_rel = f"critique/{version:04d}.md"
            json_rel = f"critique/{version:04d}.json"

            md_body = content if content.endswith("\n") else content + "\n"
            _atomic_write_text(job.root / md_rel, md_body)
            _atomic_write_json(
                job.root / json_rel,
                {
                    "version": version,
                    "model": model,
                    "cost_usd": cost,
                    "should_replan": bool(should_replan),
                    "payload": payload,
                    "created_at": now,
                },
            )

            conn.execute(
                """
                INSERT INTO critiques (
                    job_id, version, md_path, model, cost_usd,
                    should_replan, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    version,
                    md_rel,
                    model,
                    cost,
                    1 if should_replan else 0,
                    payload_json,
                    now,
                ),
            )
    finally:
        conn.close()

    return version


def _rotate_report_to(history_dir: Path, report_path: Path, *, prefix: str = "") -> Path | None:
    """Rotate ``report_path`` into ``history_dir`` as ``<prefix><stamp>[-N].md``.

    Shared by :func:`write_report` (within-run rotation under
    ``report.history/``) and :meth:`Job.archive_and_soft_reset` (cross-run
    rotation under ``archive/``). Uses the project's UTC ISO timestamp shape
    (``YYYYMMDDTHHMMSSZ``); if two rotations land in the same second, appends
    a numeric suffix rather than clobbering. Returns the archive path, or
    ``None`` if ``report_path`` did not exist.
    """
    if not report_path.exists():
        return None
    history_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archived = history_dir / f"{prefix}{stamp}.md"
    suffix = 1
    while archived.exists():
        archived = history_dir / f"{prefix}{stamp}-{suffix}.md"
        suffix += 1
    os.replace(report_path, archived)
    return archived


def write_report(job: Job, content: str) -> Path:
    """Rotate any prior ``report.md`` into ``report.history/`` then write fresh content."""
    if not isinstance(content, str):
        raise ValueError(f"content must be a string; got {type(content).__name__}")

    report = job.root / "report.md"
    history_dir = job.root / "report.history"
    _rotate_report_to(history_dir, report)

    _atomic_write_text(report, content if content.endswith("\n") else content + "\n")
    return report


__all__ = [
    "latest_fragment",
    "latest_fragments",
    "write_fragment",
    "write_critique",
    "write_finding",
    "write_finding_translation",
    "write_plan",
    "write_report",
    "write_synthesis",
    "write_synthesis_failed",
]
