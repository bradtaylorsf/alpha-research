"""FTS5 + semantic hybrid search over findings and sources.

Issue #22 introduced ``search_fts`` (BM25 over ``findings_fts`` /
``sources_fts``); issue #43 layers a cosine-similarity pass over the packed
float32 ``findings.embedding`` / ``sources.embedding`` BLOBs on top and fuses
the two ranked lists via reciprocal-rank fusion (RRF). When the cross-job
source count exceeds :data:`SQLITE_VEC_THRESHOLD`, the cosine pass tries to
load the optional ``sqlite-vec`` extension and runs the KNN in SQL; any load
or execution error falls back to numpy cosine, so the hybrid path is always
available even without the extension.

The MATCH string is the user's query verbatim; FTS5 syntax errors propagate
as :class:`sqlite3.OperationalError` for the CLI to translate into a friendly
error message.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from research_agent.storage import db

logger = logging.getLogger(__name__)

ALLOWED_KINDS = ("findings", "sources", "both")

# Above this source count the cosine pass prefers the sqlite-vec KNN path.
SQLITE_VEC_THRESHOLD = 5000

# Default RRF constant from the original Cormack et al. paper.
DEFAULT_RRF_K = 60

# Default per-list result cap (FTS top-N and cosine top-N before fusion).
DEFAULT_TOP_K = 50

# Module-level cache for sqlite-vec *availability* in this process.
# True means a prior load succeeded (so the package is importable and the
# extension works); we still have to call ``sqlite_vec.load(conn)`` on every
# new connection because SQLite extensions are connection-local. False is a
# negative cache: we already tried and the extension is unavailable, so we
# skip the load attempt and fall back to numpy. None means "not yet probed".
_SQLITE_VEC_LOADED: bool | None = None


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


# ---------------------------------------------------------------------------
# Embedding + cosine helpers
# ---------------------------------------------------------------------------


def _embed_query(
    query: str,
    models_config: dict[str, Any] | None = None,
) -> np.ndarray:
    """Embed ``query`` via the LM Studio ``embeddings`` tier.

    Reuses the wiring from :mod:`research_agent.tools.local_corpus` so the
    hybrid path goes through exactly the same endpoint and model that the
    indexer uses. Returns a float32 numpy vector.
    """
    # Local import to avoid a circular dependency at module import time
    # (``local_corpus`` imports from this package's storage layer too).
    from research_agent.tools.local_corpus import (
        _embed_chunks_sync,
        _resolve_embedding_endpoint,
    )

    base_url, model_name = _resolve_embedding_endpoint(models_config)
    vectors = _embed_chunks_sync([query], base_url, model_name)
    if not vectors:
        raise RuntimeError("embeddings tier returned no vectors for the query")
    return vectors[0]


def _count_sources(conn: sqlite3.Connection, job_id: str | None) -> int:
    """Return the number of sources visible to the search.

    When ``job_id`` is set the count is scoped to that job's ``job_sources``
    rows; otherwise it counts every row in ``sources``. Used to gate the
    sqlite-vec path against the :data:`SQLITE_VEC_THRESHOLD` cutoff.
    """
    if job_id is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM job_sources WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return int(row["n"]) if row is not None else 0


def _try_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Try to load the ``sqlite-vec`` extension on ``conn``.

    Tries the python ``sqlite_vec`` package first (the official install
    pattern), then falls back to ``conn.load_extension('vec0')``. Returns
    True on success and False on any failure — callers must use the numpy
    fallback path when this returns False.

    SQLite loads extensions *per-connection*, so this function always runs
    the load attempt on ``conn``; the module-level :data:`_SQLITE_VEC_LOADED`
    cache is only used as a *negative* short-circuit (skip the work when we
    already know the extension is unavailable in this process) and to skip
    the one-time function probe after the first successful load.
    """
    global _SQLITE_VEC_LOADED
    if _SQLITE_VEC_LOADED is False:
        return False

    try:
        conn.enable_load_extension(True)
    except (AttributeError, sqlite3.OperationalError) as exc:
        logger.debug("sqlite extension loading not supported on this build: %s", exc)
        _SQLITE_VEC_LOADED = False
        return False

    try:
        import sqlite_vec  # type: ignore[import-not-found]

        sqlite_vec.load(conn)
    except Exception as pkg_exc:  # noqa: BLE001 — fall through to direct load
        logger.debug("sqlite_vec python package unavailable: %s", pkg_exc)
        try:
            conn.load_extension("vec0")
        except Exception as load_exc:  # noqa: BLE001
            logger.debug("conn.load_extension('vec0') failed: %s", load_exc)
            try:
                conn.enable_load_extension(False)
            except sqlite3.OperationalError:
                pass
            _SQLITE_VEC_LOADED = False
            return False

    if _SQLITE_VEC_LOADED is None:
        # First time we've gotten this far in the process — confirm the
        # cosine helper is actually registered before we commit to it.
        try:
            conn.execute("SELECT vec_distance_cosine(X'00000000', X'00000000')").fetchone()
        except sqlite3.OperationalError as exc:
            logger.debug("sqlite-vec loaded but vec_distance_cosine missing: %s", exc)
            _SQLITE_VEC_LOADED = False
            return False

    _SQLITE_VEC_LOADED = True
    return True


def _reset_sqlite_vec_cache() -> None:
    """Reset the module-level sqlite-vec availability cache. Used by tests."""
    global _SQLITE_VEC_LOADED
    _SQLITE_VEC_LOADED = None


def _unpack_embedding(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4")


def _pack_query_vec(qvec: np.ndarray) -> bytes:
    return np.asarray(qvec, dtype="<f4").tobytes()


def _cosine_search_numpy(
    conn: sqlite3.Connection,
    qvec: np.ndarray,
    job_id: str | None,
    kind: str,
    top_k: int = DEFAULT_TOP_K,
) -> list[dict[str, Any]]:
    """Score every embedded row by cosine similarity in numpy. Returns top-k."""
    qnorm = float(np.linalg.norm(qvec))
    if qnorm == 0.0:
        return []

    scored: list[dict[str, Any]] = []

    if kind in ("findings", "both"):
        sql = (
            "SELECT f.id AS id, f.job_id AS job_id, f.md_path AS md_path, "
            "f.claim AS title_or_claim, f.embedding AS embedding "
            "FROM findings f WHERE f.embedding IS NOT NULL"
        )
        params: list[Any] = []
        if job_id is not None:
            sql += " AND f.job_id = ?"
            params.append(job_id)
        for row in conn.execute(sql, params).fetchall():
            vec = _unpack_embedding(row["embedding"])
            if vec.shape[0] != qvec.shape[0]:
                continue
            vnorm = float(np.linalg.norm(vec))
            if vnorm == 0.0:
                continue
            score = float(np.dot(qvec, vec) / (qnorm * vnorm))
            scored.append(
                {
                    "kind": "finding",
                    "id": int(row["id"]),
                    "job_id": row["job_id"],
                    "md_path": row["md_path"],
                    "title_or_claim": row["title_or_claim"],
                    "score": score,
                }
            )

    if kind in ("sources", "both"):
        sql = (
            "SELECT s.id AS id, js.job_id AS job_id, s.md_path AS md_path, "
            "s.title AS title_or_claim, s.embedding AS embedding "
            "FROM sources s JOIN job_sources js ON js.source_id = s.id "
            "WHERE s.embedding IS NOT NULL"
        )
        params = []
        if job_id is not None:
            sql += " AND js.job_id = ?"
            params.append(job_id)
        for row in conn.execute(sql, params).fetchall():
            vec = _unpack_embedding(row["embedding"])
            if vec.shape[0] != qvec.shape[0]:
                continue
            vnorm = float(np.linalg.norm(vec))
            if vnorm == 0.0:
                continue
            score = float(np.dot(qvec, vec) / (qnorm * vnorm))
            scored.append(
                {
                    "kind": "source",
                    "id": int(row["id"]),
                    "job_id": row["job_id"],
                    "md_path": row["md_path"],
                    "title_or_claim": row["title_or_claim"],
                    "score": score,
                }
            )

    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:top_k]


def _cosine_search_sqlite_vec(
    conn: sqlite3.Connection,
    qvec: np.ndarray,
    job_id: str | None,
    kind: str,
    top_k: int = DEFAULT_TOP_K,
) -> list[dict[str, Any]]:
    """Score every embedded row using the sqlite-vec ``vec_distance_cosine``.

    Returns the same dict shape as :func:`_cosine_search_numpy`. Cosine
    *distance* is converted back to *similarity* (``1 - distance``) so the
    score axis matches the numpy path and downstream RRF logic.
    """
    qblob = _pack_query_vec(qvec)
    rows: list[dict[str, Any]] = []

    if kind in ("findings", "both"):
        sql = (
            "SELECT f.id AS id, f.job_id AS job_id, f.md_path AS md_path, "
            "f.claim AS title_or_claim, "
            "vec_distance_cosine(f.embedding, ?) AS distance "
            "FROM findings f WHERE f.embedding IS NOT NULL"
        )
        params: list[Any] = [qblob]
        if job_id is not None:
            sql += " AND f.job_id = ?"
            params.append(job_id)
        sql += " ORDER BY distance ASC LIMIT ?"
        params.append(top_k)
        for row in conn.execute(sql, params).fetchall():
            rows.append(
                {
                    "kind": "finding",
                    "id": int(row["id"]),
                    "job_id": row["job_id"],
                    "md_path": row["md_path"],
                    "title_or_claim": row["title_or_claim"],
                    "score": 1.0 - float(row["distance"]),
                }
            )

    if kind in ("sources", "both"):
        sql = (
            "SELECT s.id AS id, js.job_id AS job_id, s.md_path AS md_path, "
            "s.title AS title_or_claim, "
            "vec_distance_cosine(s.embedding, ?) AS distance "
            "FROM sources s JOIN job_sources js ON js.source_id = s.id "
            "WHERE s.embedding IS NOT NULL"
        )
        params = [qblob]
        if job_id is not None:
            sql += " AND js.job_id = ?"
            params.append(job_id)
        sql += " ORDER BY distance ASC LIMIT ?"
        params.append(top_k)
        for row in conn.execute(sql, params).fetchall():
            rows.append(
                {
                    "kind": "source",
                    "id": int(row["id"]),
                    "job_id": row["job_id"],
                    "md_path": row["md_path"],
                    "title_or_claim": row["title_or_claim"],
                    "score": 1.0 - float(row["distance"]),
                }
            )

    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:top_k]


def _rrf_fuse(
    fts_results: list[dict[str, Any]],
    cosine_results: list[dict[str, Any]],
    k: int = DEFAULT_RRF_K,
) -> list[dict[str, Any]]:
    """Reciprocal-rank-fuse two ranked lists into a deduped, sorted list.

    Each item appears at most once, keyed by ``(kind, id)``. Its fused
    ``score`` is ``sum(1/(k + rank))`` over the lists in which it appears,
    where ``rank`` is its 1-based position. Items missing from a list
    contribute zero for that list. The returned list is sorted by fused
    score descending; metadata from the FTS hit (snippet, etc.) is kept
    when present, with the cosine hit's metadata used as a fallback.
    """
    if k <= 0:
        raise ValueError(f"k must be positive; got {k}")

    fused: dict[tuple[str, int], dict[str, Any]] = {}

    for rank_idx, item in enumerate(fts_results, start=1):
        key = (item["kind"], int(item["id"]))
        contribution = 1.0 / (k + rank_idx)
        entry = fused.setdefault(
            key,
            {
                "kind": item["kind"],
                "id": int(item["id"]),
                "job_id": item.get("job_id"),
                "md_path": item.get("md_path"),
                "title_or_claim": item.get("title_or_claim"),
                "snippet": item.get("snippet"),
                "score": 0.0,
                "fts_score": item.get("score"),
                "cosine_score": None,
            },
        )
        # Preserve FTS-only fields if this is a re-merge from cosine-first.
        if entry.get("snippet") is None and item.get("snippet") is not None:
            entry["snippet"] = item.get("snippet")
        if entry.get("fts_score") is None:
            entry["fts_score"] = item.get("score")
        entry["score"] += contribution

    for rank_idx, item in enumerate(cosine_results, start=1):
        key = (item["kind"], int(item["id"]))
        contribution = 1.0 / (k + rank_idx)
        entry = fused.setdefault(
            key,
            {
                "kind": item["kind"],
                "id": int(item["id"]),
                "job_id": item.get("job_id"),
                "md_path": item.get("md_path"),
                "title_or_claim": item.get("title_or_claim"),
                "snippet": None,
                "score": 0.0,
                "fts_score": None,
                "cosine_score": item.get("score"),
            },
        )
        if entry.get("cosine_score") is None:
            entry["cosine_score"] = item.get("score")
        entry["score"] += contribution

    out = list(fused.values())
    out.sort(key=lambda r: r["score"], reverse=True)
    return out


def search_hybrid(
    query: str,
    *,
    job_id: str | None,
    kind: str,
    db_path: Path | str = db.DEFAULT_DB_PATH,
    models_config: dict[str, Any] | None = None,
    top_k: int = DEFAULT_TOP_K,
    rrf_k: int = DEFAULT_RRF_K,
) -> list[dict[str, Any]]:
    """Hybrid FTS5 + cosine search with reciprocal-rank fusion.

    Embeds ``query`` through the embeddings tier, runs FTS and cosine in
    parallel paths (each capped at ``top_k``), and fuses the rankings via
    :func:`_rrf_fuse`. Above :data:`SQLITE_VEC_THRESHOLD` cross-job sources
    the cosine pass tries the sqlite-vec extension and falls back to numpy
    on any error.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"kind must be one of {list(ALLOWED_KINDS)}; got {kind!r}")
    if top_k <= 0:
        raise ValueError(f"top_k must be positive; got {top_k}")

    fts_results = search_fts(query, job_id=job_id, kind=kind, db_path=db_path)[:top_k]

    qvec = _embed_query(query, models_config)

    conn = db.connect(Path(db_path))
    try:
        source_count = _count_sources(conn, job_id)
        cosine_results: list[dict[str, Any]] = []
        used_sqlite_vec = False
        if source_count > SQLITE_VEC_THRESHOLD and _try_load_sqlite_vec(conn):
            try:
                cosine_results = _cosine_search_sqlite_vec(conn, qvec, job_id, kind, top_k=top_k)
                used_sqlite_vec = True
            except sqlite3.OperationalError as exc:
                logger.warning("sqlite-vec cosine path failed; falling back to numpy: %s", exc)
                cosine_results = []
        if not used_sqlite_vec:
            cosine_results = _cosine_search_numpy(conn, qvec, job_id, kind, top_k=top_k)
    finally:
        conn.close()

    return _rrf_fuse(fts_results, cosine_results, k=rrf_k)


__all__ = [
    "ALLOWED_KINDS",
    "DEFAULT_RRF_K",
    "DEFAULT_TOP_K",
    "SQLITE_VEC_THRESHOLD",
    "search_fts",
    "search_hybrid",
]
