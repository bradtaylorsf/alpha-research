"""Per-job disk-cap enforcement (issue #38).

Implements the §6.3 "source content is the biggest disk hog" guard rail.
The daemon polls :func:`disk_usage_bytes` every 5 minutes (see
:mod:`research_agent.daemon`); when the per-job total exceeds the cap, it
calls :func:`prune_to_target` to delete the on-disk markdown for the
lowest-relevance sources until usage drops below ``cap * target_pct``.

Pruned ``sources`` rows stay in the cross-job index (``sources.md_path``
is set to ``NULL``) so audit history survives — and a future fetch with
the same sha256 re-creates the file via the re-fetch branch in
:func:`research_agent.storage.sources.write_source`. Pruned ≠ banned.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from research_agent.observability.events import emit
from research_agent.storage import db
from research_agent.storage.jobs import Job

# Relevance weights. Tuned conservatively: a single finding citation
# outweighs ~5 FTS title hits and ~10 days of age decay.
_W_FINDINGS = 5.0
_W_FTS = 1.0
_W_AGE = 0.1

# Fraction of the lowest-scored sources to prune per pass.
_PRUNE_FRACTION = 0.10

# FTS query-term sanitizer: keep alphanumerics + underscore; everything
# else becomes whitespace. Prevents FTS5 syntax errors on user goals
# containing quotes, parens, or operators (AND/OR/NOT/NEAR are still
# tokens, but stripping punctuation is the load-bearing piece).
_FTS_TERM_RE = re.compile(r"[^A-Za-z0-9_]+")


@dataclass(frozen=True)
class ScoredSource:
    source_id: int
    sha256: str
    md_path: str
    url: str | None
    score: float


def disk_usage_bytes(job_root: Path) -> int:
    """Sum the file sizes under ``job_root`` recursively (Python ``du -s``).

    Skips directories and symlink-to-directory entries; missing files
    raised by races between scan and unlink are ignored.
    """
    total = 0
    if not job_root.exists():
        return 0
    for p in job_root.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _sanitize_fts_query(goal: str) -> str:
    """Reduce ``goal`` to a whitespace-joined bag of FTS5-safe tokens."""
    if not goal:
        return ""
    tokens = [t for t in _FTS_TERM_RE.sub(" ", goal).split() if len(t) > 1]
    # Dedupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        low = t.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(low)
    return " ".join(out)


def _fts_hit_ids(conn: sqlite3.Connection, query: str) -> set[int]:
    """Return the set of ``sources.id`` whose title FTS-matches ``query``.

    Empty query or any FTS5 error → empty set (callers treat absence as
    score 0 rather than failing the whole prune pass).
    """
    if not query:
        return set()
    try:
        rows = conn.execute(
            "SELECT rowid FROM sources_fts WHERE sources_fts MATCH ?",
            (query,),
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {int(r["rowid"]) for r in rows}


def _findings_usage(
    conn: sqlite3.Connection, job_id: str, source_ids: Iterable[int]
) -> dict[int, int]:
    """Count findings (job-scoped) referencing each source id.

    ``source_ids`` is stored as a JSON array string in ``findings.source_ids``;
    a cheap Python pass beats a SQL ``json_each`` join when the per-job count
    is small (which is the common case).
    """
    counts: dict[int, int] = {sid: 0 for sid in source_ids}
    rows = conn.execute(
        "SELECT source_ids FROM findings WHERE job_id = ?",
        (job_id,),
    ).fetchall()
    for row in rows:
        raw = row["source_ids"]
        if not raw:
            continue
        try:
            ids = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(ids, list):
            continue
        for sid in ids:
            if isinstance(sid, int) and sid in counts:
                counts[sid] += 1
    return counts


def score_sources(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    goal: str | None = None,
    now_ts: int | None = None,
    only_with_md_path: bool = True,
) -> list[ScoredSource]:
    """Compute a relevance score per source linked to ``job_id``.

    score = ``W_FINDINGS * findings_usage + W_FTS * fts_hit - W_AGE * age_days``.

    With ``only_with_md_path=True`` (the default) skip rows whose ``md_path``
    has already been pruned to NULL — they're no longer disk-bearing and
    can't be pruned again.
    """
    import time as _time

    now = int(now_ts) if now_ts is not None else int(_time.time())
    rows = conn.execute(
        """
        SELECT s.id, s.sha256, s.md_path, s.url, s.fetched_at
        FROM sources s
        JOIN job_sources js ON js.source_id = s.id
        WHERE js.job_id = ?
        """,
        (job_id,),
    ).fetchall()

    candidates: list[sqlite3.Row] = []
    for r in rows:
        if only_with_md_path and not r["md_path"]:
            continue
        candidates.append(r)

    if not candidates:
        return []

    source_ids = [int(r["id"]) for r in candidates]
    findings_counts = _findings_usage(conn, job_id, source_ids)
    fts_query = _sanitize_fts_query(goal or "")
    fts_hits = _fts_hit_ids(conn, fts_query)

    scored: list[ScoredSource] = []
    for r in candidates:
        sid = int(r["id"])
        findings_usage = findings_counts.get(sid, 0)
        fts_hit = 1 if sid in fts_hits else 0
        age_days = max(0.0, (now - int(r["fetched_at"])) / 86400.0)
        score = _W_FINDINGS * findings_usage + _W_FTS * fts_hit - _W_AGE * age_days
        scored.append(
            ScoredSource(
                source_id=sid,
                sha256=r["sha256"],
                md_path=r["md_path"],
                url=r["url"],
                score=score,
            )
        )
    return scored


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def prune_to_target(
    job: Job,
    *,
    cap_bytes: int,
    target_pct: float = 0.9,
    db_path: Path | str | None = None,
) -> int:
    """Drop disk usage for ``job`` below ``cap_bytes * target_pct``.

    Strategy: rank linked sources by relevance, prune the lowest-scored
    10 % (min 1) per pass, re-measure, repeat until under the target or
    no prunable source remains. Pruning a source unlinks its
    ``sources/<sha>.md`` file and clears ``sources.md_path`` to NULL — the
    row stays in the cross-job index so future fetches with the same sha
    can re-fetch via :func:`research_agent.storage.sources.write_source`.

    Emits exactly one ``WARN``/``warning`` event when the cap is first
    crossed, plus one ``INFO``/``source_pruned`` event per file removed.
    Returns the count pruned.
    """
    if cap_bytes <= 0:
        raise ValueError(f"cap_bytes must be positive; got {cap_bytes}")
    if not 0.0 < target_pct <= 1.0:
        raise ValueError(f"target_pct must be in (0, 1]; got {target_pct}")

    db_path_p = Path(db_path) if db_path is not None else job.db_path
    target_bytes = int(cap_bytes * target_pct)

    usage = disk_usage_bytes(job.root)
    if usage <= cap_bytes:
        return 0

    try:
        emit(
            job,
            "WARN",
            "disk_cap",
            "warning",
            {
                "stage": "disk_cap_exceeded",
                "usage_bytes": usage,
                "cap_bytes": cap_bytes,
            },
            db_path=db_path_p,
        )
    except Exception:
        pass

    pruned_count = 0
    # Bound the loop: ~one pass per ~10 % chunk plus headroom for races.
    for _ in range(64):
        usage = disk_usage_bytes(job.root)
        if usage <= target_bytes:
            break

        conn = db.connect(db_path_p)
        try:
            scored = score_sources(conn, job.id, goal=job.goal)
        finally:
            conn.close()
        if not scored:
            break

        scored.sort(key=lambda s: s.score)
        bottom_count = max(1, len(scored) // 10)
        bottom = scored[:bottom_count]

        for src in bottom:
            file_path = job.root / src.md_path
            _unlink_quietly(file_path)
            sidecar = file_path.with_suffix(".json")
            _unlink_quietly(sidecar)

            conn = db.connect(db_path_p)
            try:
                with conn:
                    conn.execute(
                        "UPDATE sources SET md_path = NULL WHERE id = ?",
                        (src.source_id,),
                    )
            finally:
                conn.close()

            try:
                emit(
                    job,
                    "INFO",
                    "disk_cap",
                    "source_pruned",
                    {
                        "source_id": src.source_id,
                        "sha256": src.sha256,
                        "url": src.url,
                    },
                    db_path=db_path_p,
                )
            except Exception:
                pass
            pruned_count += 1

    return pruned_count


__all__ = [
    "ScoredSource",
    "disk_usage_bytes",
    "prune_to_target",
    "score_sources",
]
