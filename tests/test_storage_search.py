"""Tests for `research_agent.storage.search` (issue #22 + #43)."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from research_agent.storage import db
from research_agent.storage import search as search_mod
from research_agent.storage.search import (
    DEFAULT_RRF_K,
    _rrf_fuse,
    _try_load_sqlite_vec,
    search_fts,
    search_hybrid,
)

JOB_A = "2026-05-01-job-a"
JOB_B = "2026-05-02-job-b"


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """Build a tmp DB with two jobs, several findings, and two sources."""
    db_path = tmp_path / "data" / "index.sqlite"
    db.migrate(path=db_path).close()

    now = int(time.time())
    conn = db.connect(db_path)
    try:
        with conn:
            for jid in (JOB_A, JOB_B):
                conn.execute(
                    "INSERT INTO jobs (id, goal, status, intake_json, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (jid, "g", "pending", "{}", now),
                )

            conn.execute(
                "INSERT INTO findings"
                " (job_id, md_path, claim, confidence, source_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    JOB_A,
                    "findings/000001.md",
                    "quantum entanglement breakthrough at MIT lab",
                    0.9,
                    "[]",
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO findings"
                " (job_id, md_path, claim, confidence, source_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    JOB_B,
                    "findings/000001.md",
                    "quantum supremacy benchmark surpassed",
                    0.8,
                    "[]",
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO findings"
                " (job_id, md_path, claim, confidence, source_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    JOB_A,
                    "findings/000002.md",
                    "unrelated coffee research finding",
                    0.5,
                    "[]",
                    now,
                ),
            )

            cur1 = conn.execute(
                "INSERT INTO sources"
                " (sha256, url, title, fetched_at, md_path, kind)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "a" * 64,
                    "https://example.com/q",
                    "Quantum computing report",
                    now,
                    "sources/a.md",
                    "web",
                ),
            )
            sid1 = cur1.lastrowid
            cur2 = conn.execute(
                "INSERT INTO sources"
                " (sha256, url, title, fetched_at, md_path, kind)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "b" * 64,
                    "https://example.com/c",
                    "Coffee brewing techniques",
                    now,
                    "sources/b.md",
                    "web",
                ),
            )
            sid2 = cur2.lastrowid

            # sid1 shared by both jobs; sid2 only by JOB_A.
            conn.execute(
                "INSERT INTO job_sources (job_id, source_id) VALUES (?, ?)",
                (JOB_A, sid1),
            )
            conn.execute(
                "INSERT INTO job_sources (job_id, source_id) VALUES (?, ?)",
                (JOB_B, sid1),
            )
            conn.execute(
                "INSERT INTO job_sources (job_id, source_id) VALUES (?, ?)",
                (JOB_A, sid2),
            )

            conn.execute("INSERT INTO findings_fts(findings_fts) VALUES('rebuild')")
            conn.execute("INSERT INTO sources_fts(sources_fts) VALUES('rebuild')")
    finally:
        conn.close()

    return db_path


def test_empty_result(seeded_db: Path) -> None:
    assert search_fts("nonexistenttoken", job_id=None, kind="both", db_path=seeded_db) == []


def test_multi_job_hit(seeded_db: Path) -> None:
    results = search_fts("quantum", job_id=None, kind="findings", db_path=seeded_db)
    job_ids = {r["job_id"] for r in results}
    assert JOB_A in job_ids
    assert JOB_B in job_ids


def test_kind_findings_excludes_sources(seeded_db: Path) -> None:
    results = search_fts("quantum", job_id=None, kind="findings", db_path=seeded_db)
    assert results
    assert all(r["kind"] == "finding" for r in results)


def test_kind_sources_excludes_findings(seeded_db: Path) -> None:
    results = search_fts("quantum", job_id=None, kind="sources", db_path=seeded_db)
    assert results
    assert all(r["kind"] == "source" for r in results)


def test_kind_both_returns_mixed(seeded_db: Path) -> None:
    results = search_fts("quantum", job_id=None, kind="both", db_path=seeded_db)
    kinds = {r["kind"] for r in results}
    assert kinds == {"finding", "source"}


def test_job_filter_findings(seeded_db: Path) -> None:
    results = search_fts("quantum", job_id=JOB_A, kind="findings", db_path=seeded_db)
    assert results
    assert all(r["job_id"] == JOB_A for r in results)


def test_job_filter_sources_uses_job_sources(seeded_db: Path) -> None:
    # The "Coffee brewing" source is only linked to JOB_A.
    a_only = search_fts("coffee", job_id=JOB_A, kind="sources", db_path=seeded_db)
    assert a_only
    assert all(r["job_id"] == JOB_A for r in a_only)

    b_only = search_fts("coffee", job_id=JOB_B, kind="sources", db_path=seeded_db)
    assert b_only == []


def test_snippet_highlighting(seeded_db: Path) -> None:
    results = search_fts("quantum", job_id=None, kind="findings", db_path=seeded_db)
    assert results
    snippet = results[0]["snippet"]
    assert "[" in snippet and "]" in snippet


def test_results_sorted_by_score_ascending(seeded_db: Path) -> None:
    results = search_fts("quantum", job_id=None, kind="both", db_path=seeded_db)
    scores = [r["score"] for r in results]
    assert scores == sorted(scores)


def test_invalid_kind_raises(seeded_db: Path) -> None:
    with pytest.raises(ValueError):
        search_fts("quantum", job_id=None, kind="bogus", db_path=seeded_db)


def test_empty_query_raises(seeded_db: Path) -> None:
    with pytest.raises(ValueError):
        search_fts("   ", job_id=None, kind="both", db_path=seeded_db)


def test_result_dict_has_expected_keys(seeded_db: Path) -> None:
    results = search_fts("quantum", job_id=None, kind="both", db_path=seeded_db)
    assert results
    expected = {"kind", "score", "job_id", "snippet", "id", "md_path", "title_or_claim"}
    for row in results:
        assert expected <= row.keys()


# ---------------------------------------------------------------------------
# Hybrid search (#43): RRF + cosine + sqlite-vec fallback
# ---------------------------------------------------------------------------


EMBED_DIM = 1024


def _pack(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype="<f4").tobytes()


def _unit(seed: int, dim: int = EMBED_DIM) -> np.ndarray:
    rng = np.random.default_rng(seed=seed)
    v = rng.standard_normal(dim).astype(np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


@pytest.fixture
def embedded_db(tmp_path: Path) -> Path:
    """DB seeded with findings + sources whose embeddings are deterministic.

    Two findings and two sources, each with a unique unit vector, plus one
    finding with no embedding (NULL) so we can exercise the IS NOT NULL gate.
    """
    db_path = tmp_path / "data" / "index.sqlite"
    db.migrate(path=db_path).close()

    now = int(time.time())
    conn = db.connect(db_path)
    try:
        with conn:
            for jid in (JOB_A, JOB_B):
                conn.execute(
                    "INSERT INTO jobs (id, goal, status, intake_json, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (jid, "g", "pending", "{}", now),
                )

            # Finding 1 (JOB_A) — embedding aligned with seed 1.
            conn.execute(
                "INSERT INTO findings"
                " (job_id, md_path, claim, confidence, source_ids, embedding, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    JOB_A,
                    "findings/000001.md",
                    "quantum entanglement breakthrough",
                    0.9,
                    "[]",
                    _pack(_unit(1)),
                    now,
                ),
            )
            # Finding 2 (JOB_B) — embedding aligned with seed 2.
            conn.execute(
                "INSERT INTO findings"
                " (job_id, md_path, claim, confidence, source_ids, embedding, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    JOB_B,
                    "findings/000001.md",
                    "quantum supremacy benchmark surpassed",
                    0.8,
                    "[]",
                    _pack(_unit(2)),
                    now,
                ),
            )
            # Finding 3 (JOB_A) — no embedding; should be skipped by cosine path.
            conn.execute(
                "INSERT INTO findings"
                " (job_id, md_path, claim, confidence, source_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (JOB_A, "findings/000002.md", "unrelated coffee finding", 0.5, "[]", now),
            )

            cur1 = conn.execute(
                "INSERT INTO sources"
                " (sha256, url, title, fetched_at, md_path, kind, embedding)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "a" * 64,
                    "https://example.com/q",
                    "Quantum computing report",
                    now,
                    "sources/a.md",
                    "web",
                    _pack(_unit(3)),
                ),
            )
            sid1 = cur1.lastrowid
            cur2 = conn.execute(
                "INSERT INTO sources"
                " (sha256, url, title, fetched_at, md_path, kind, embedding)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "b" * 64,
                    "https://example.com/c",
                    "Coffee brewing techniques",
                    now,
                    "sources/b.md",
                    "web",
                    _pack(_unit(4)),
                ),
            )
            sid2 = cur2.lastrowid

            conn.execute(
                "INSERT INTO job_sources (job_id, source_id) VALUES (?, ?)",
                (JOB_A, sid1),
            )
            conn.execute(
                "INSERT INTO job_sources (job_id, source_id) VALUES (?, ?)",
                (JOB_B, sid1),
            )
            conn.execute(
                "INSERT INTO job_sources (job_id, source_id) VALUES (?, ?)",
                (JOB_A, sid2),
            )

            conn.execute("INSERT INTO findings_fts(findings_fts) VALUES('rebuild')")
            conn.execute("INSERT INTO sources_fts(sources_fts) VALUES('rebuild')")
    finally:
        conn.close()

    return db_path


@pytest.fixture(autouse=True)
def _reset_sqlite_vec_cache():
    """Clear the per-process sqlite-vec availability cache between tests."""
    search_mod._reset_sqlite_vec_cache()
    yield
    search_mod._reset_sqlite_vec_cache()


# ---- _rrf_fuse ------------------------------------------------------------


def test_rrf_dedup() -> None:
    """An item present in both lists collapses to one entry with summed RRF."""
    fts = [
        {"kind": "finding", "id": 1, "score": 0.1, "snippet": "a [hit]"},
        {"kind": "finding", "id": 2, "score": 0.5, "snippet": "b"},
    ]
    cosine = [
        {"kind": "finding", "id": 1, "score": 0.9},
        {"kind": "source", "id": 5, "score": 0.7},
    ]
    fused = _rrf_fuse(fts, cosine, k=60)

    assert len(fused) == 3  # (finding,1) deduped, (finding,2), (source,5)
    by_key = {(r["kind"], r["id"]): r for r in fused}

    expected_dedup = (1.0 / (60 + 1)) + (1.0 / (60 + 1))
    assert by_key[("finding", 1)]["score"] == pytest.approx(expected_dedup)
    assert by_key[("finding", 1)]["snippet"] == "a [hit]"
    assert by_key[("finding", 1)]["fts_score"] == 0.1
    assert by_key[("finding", 1)]["cosine_score"] == 0.9


def test_rrf_math() -> None:
    """Hand-computed fused scores for a fixed 3-item / 3-item layout."""
    fts = [
        {"kind": "finding", "id": 10, "score": 0.0},  # rank 1 in FTS
        {"kind": "finding", "id": 20, "score": 0.5},  # rank 2 in FTS
        {"kind": "source", "id": 30, "score": 1.5},  # rank 3 in FTS
    ]
    cosine = [
        {"kind": "source", "id": 30, "score": 0.95},  # rank 1 in cosine
        {"kind": "finding", "id": 10, "score": 0.85},  # rank 2 in cosine
        {"kind": "finding", "id": 40, "score": 0.80},  # rank 3 in cosine, new
    ]
    k = 60
    fused = _rrf_fuse(fts, cosine, k=k)

    expected = {
        ("finding", 10): 1.0 / (k + 1) + 1.0 / (k + 2),
        ("finding", 20): 1.0 / (k + 2),
        ("source", 30): 1.0 / (k + 3) + 1.0 / (k + 1),
        ("finding", 40): 1.0 / (k + 3),
    }
    by_key = {(r["kind"], r["id"]): r for r in fused}
    assert set(by_key.keys()) == set(expected.keys())
    for key, want in expected.items():
        assert by_key[key]["score"] == pytest.approx(want)

    # Output must be sorted by fused score descending.
    scores = [r["score"] for r in fused]
    assert scores == sorted(scores, reverse=True)


def test_rrf_default_k_constant() -> None:
    assert DEFAULT_RRF_K == 60


def test_rrf_invalid_k() -> None:
    with pytest.raises(ValueError):
        _rrf_fuse([], [], k=0)


# ---- search_hybrid --------------------------------------------------------


def _patch_embed(monkeypatch: pytest.MonkeyPatch, vec: np.ndarray) -> None:
    monkeypatch.setattr(search_mod, "_embed_query", lambda q, cfg=None: vec)


def test_hybrid_falls_back_to_numpy(embedded_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When sqlite-vec can't load, results still rank correctly via numpy cosine."""
    # Force the loader to report unavailable.
    monkeypatch.setattr(search_mod, "_try_load_sqlite_vec", lambda conn: False)
    # Below threshold the numpy path is selected anyway, but we also force
    # the threshold to 0 to take the sqlite-vec branch and prove it falls back.
    monkeypatch.setattr(search_mod, "SQLITE_VEC_THRESHOLD", 0)

    # Query vector aligned with finding #1's embedding so cosine should
    # rank that finding very high (cos ≈ 1.0).
    _patch_embed(monkeypatch, _unit(1))

    results = search_hybrid("quantum", job_id=None, kind="both", db_path=embedded_db, top_k=50)

    assert results, "hybrid path should return at least one row"
    # Finding #1 is the only one whose embedding is aligned with our query
    # vector — it must appear and carry a cosine_score close to 1.
    by_key = {(r["kind"], r["id"]): r for r in results}
    assert ("finding", 1) in by_key
    f1 = by_key[("finding", 1)]
    assert f1["cosine_score"] is not None
    assert f1["cosine_score"] == pytest.approx(1.0, abs=1e-5)
    # Either it appeared in FTS too (then fts_score is set) or it didn't,
    # but the fused score must include the cosine contribution at minimum.
    assert f1["score"] >= 1.0 / (DEFAULT_RRF_K + 50)

    # The unrelated finding #3 has no embedding, so it should never show up
    # via the cosine path. It also doesn't match "quantum" via FTS.
    assert ("finding", 3) not in by_key


def test_hybrid_uses_sqlite_vec_above_threshold(
    embedded_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Above the source-count threshold the sqlite-vec branch is selected.

    Skips when the extension can't be loaded in this environment. We don't
    rebuild a 5000+ row fixture; instead we monkeypatch ``_count_sources``
    to report a high count, then assert that the sqlite-vec cosine helper
    is invoked.
    """
    # Probe whether sqlite-vec is loadable in this process. Use a temp
    # connection so the autouse fixture's reset still applies.
    import sqlite3 as _sqlite3

    probe_conn = _sqlite3.connect(":memory:")
    try:
        loadable = _try_load_sqlite_vec(probe_conn)
    finally:
        probe_conn.close()
    if not loadable:
        pytest.skip("sqlite-vec extension not available in this environment")
    search_mod._reset_sqlite_vec_cache()

    monkeypatch.setattr(search_mod, "_count_sources", lambda conn, jid: 10_000)

    called: dict[str, int] = {"sqlite_vec": 0, "numpy": 0}
    real_vec = search_mod._cosine_search_sqlite_vec
    real_np = search_mod._cosine_search_numpy

    def _spy_vec(*a, **kw):
        called["sqlite_vec"] += 1
        return real_vec(*a, **kw)

    def _spy_np(*a, **kw):
        called["numpy"] += 1
        return real_np(*a, **kw)

    monkeypatch.setattr(search_mod, "_cosine_search_sqlite_vec", _spy_vec)
    monkeypatch.setattr(search_mod, "_cosine_search_numpy", _spy_np)

    _patch_embed(monkeypatch, _unit(1))

    search_hybrid("quantum", job_id=None, kind="both", db_path=embedded_db, top_k=50)

    assert called["sqlite_vec"] == 1
    assert called["numpy"] == 0


def test_hybrid_returns_fused_score_keys(
    embedded_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_embed(monkeypatch, _unit(1))
    results = search_hybrid("quantum", job_id=None, kind="both", db_path=embedded_db)
    assert results
    expected = {"kind", "id", "job_id", "score", "fts_score", "cosine_score"}
    for r in results:
        assert expected <= r.keys()


def test_hybrid_empty_query_raises(embedded_db: Path) -> None:
    with pytest.raises(ValueError):
        search_hybrid("   ", job_id=None, kind="both", db_path=embedded_db)


def test_hybrid_invalid_kind_raises(embedded_db: Path) -> None:
    with pytest.raises(ValueError):
        search_hybrid("quantum", job_id=None, kind="bogus", db_path=embedded_db)
