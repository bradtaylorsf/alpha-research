"""FTS5 search over findings and sources (issue #22).

Pure read-only helper used by the ``research search`` CLI verb. Runs against
the ``findings_fts`` and ``sources_fts`` virtual tables built in §10 of the
implementation guide. Scoring uses the FTS5 ``bm25`` function — lower score
means a better match — and snippets come from the FTS5 ``snippet`` function
with literal ``[``/``]`` markers around the matched terms (the ``ui.render``
layer transforms those into Rich markup).

The MATCH string is the user's query verbatim; FTS5 syntax errors propagate
as :class:`sqlite3.OperationalError` for the CLI to translate into a friendly
error message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research_agent.storage import db

ALLOWED_KINDS = ("findings", "sources", "both")


def search_fts(
    query: str,
    *,
    job_id: str | None,
    kind: str,
    db_path: Path | str = db.DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Run FTS5 search over findings and/or sources.

    Returns a list of result dicts with keys:
    ``kind`` (``finding`` or ``source``), ``score`` (bm25 — lower is better),
    ``job_id``, ``snippet``, ``id``, ``md_path``, ``title_or_claim``.

    Source rows are joined through ``job_sources`` so a source shared by N
    jobs produces N rows in cross-job mode and is correctly filtered when
    ``job_id`` is set.

    Raises :class:`ValueError` for empty/whitespace queries or unknown
    ``kind``. FTS5 MATCH errors surface as :class:`sqlite3.OperationalError`.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"kind must be one of {list(ALLOWED_KINDS)}; got {kind!r}")

    results: list[dict[str, Any]] = []
    conn = db.connect(Path(db_path))
    try:
        if kind in ("findings", "both"):
            sql = (
                "SELECT f.id AS id, f.job_id AS job_id, "
                "bm25(findings_fts) AS score, "
                "snippet(findings_fts, 0, '[', ']', '…', 12) AS snippet, "
                "f.md_path AS md_path, f.claim AS title_or_claim "
                "FROM findings_fts "
                "JOIN findings f ON f.id = findings_fts.rowid "
                "WHERE findings_fts MATCH ?"
            )
            params: list[Any] = [query]
            if job_id is not None:
                sql += " AND f.job_id = ?"
                params.append(job_id)
            sql += " ORDER BY score"
            for row in conn.execute(sql, params).fetchall():
                results.append(
                    {
                        "kind": "finding",
                        "score": float(row["score"]),
                        "job_id": row["job_id"],
                        "snippet": row["snippet"],
                        "id": int(row["id"]),
                        "md_path": row["md_path"],
                        "title_or_claim": row["title_or_claim"],
                    }
                )

        if kind in ("sources", "both"):
            sql = (
                "SELECT s.id AS id, js.job_id AS job_id, "
                "bm25(sources_fts) AS score, "
                "snippet(sources_fts, 0, '[', ']', '…', 12) AS snippet, "
                "s.md_path AS md_path, s.title AS title_or_claim "
                "FROM sources_fts "
                "JOIN sources s ON s.id = sources_fts.rowid "
                "JOIN job_sources js ON js.source_id = s.id "
                "WHERE sources_fts MATCH ?"
            )
            params = [query]
            if job_id is not None:
                sql += " AND js.job_id = ?"
                params.append(job_id)
            sql += " ORDER BY score"
            for row in conn.execute(sql, params).fetchall():
                results.append(
                    {
                        "kind": "source",
                        "score": float(row["score"]),
                        "job_id": row["job_id"],
                        "snippet": row["snippet"],
                        "id": int(row["id"]),
                        "md_path": row["md_path"],
                        "title_or_claim": row["title_or_claim"],
                    }
                )
    finally:
        conn.close()

    results.sort(key=lambda r: r["score"])
    return results


__all__ = ["ALLOWED_KINDS", "search_fts"]
