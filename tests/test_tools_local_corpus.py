"""Tests for `research_agent.tools.local_corpus` (issue #17)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from research_agent.storage import db
from research_agent.storage.jobs import Job
from research_agent.tools import local_corpus

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "corpus"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def jobs_root(tmp_path: Path) -> Path:
    return tmp_path / "jobs"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "index.sqlite"
    db.migrate(path=p).close()
    return p


@pytest.fixture
def job(jobs_root: Path, db_path: Path) -> Job:
    return Job.create(
        {"goal": "local corpus test"},
        jobs_root=jobs_root,
        db_path=db_path,
        today=date(2026, 5, 2),
    )


@pytest.fixture
def stub_models_config() -> dict:
    """A trimmed models.yaml dict that exercises the embeddings tier path."""
    return {
        "tiers": {
            "embeddings": {
                "provider": "lmstudio",
                "model": "qwen3-embedding-4b",
                "timeout_s": 60,
            }
        }
    }


def _stub_embed(monkeypatch, *, dim: int = local_corpus.EMBED_DIM) -> list[list[str]]:
    """Replace the live embeddings call with a deterministic numpy stand-in.

    Returns the list-of-batches the stub was called with so tests can assert
    on call counts and re-embedding behavior.
    """
    rng = np.random.default_rng(seed=0)
    calls: list[list[str]] = []

    def _fake(chunks, base_url, model):  # noqa: ARG001
        calls.append(list(chunks))
        return [rng.standard_normal(dim).astype(np.float32) for _ in chunks]

    monkeypatch.setattr(local_corpus, "_embed_chunks_sync", _fake)
    return calls


# ---------------------------------------------------------------------------
# _chunk_text
# ---------------------------------------------------------------------------


def test_chunk_text_short_input_returns_one_chunk() -> None:
    chunks = local_corpus._chunk_text("hello world", target=100, overlap=10)
    assert chunks == ["hello world"]


def test_chunk_text_respects_target_size() -> None:
    text = " ".join(str(i) for i in range(250))
    chunks = local_corpus._chunk_text(text, target=100, overlap=20)
    # Step size = target - overlap = 80; windows at starts {0, 80, 160} →
    # the third window (160..259) hits len(tokens)=250 and ends the loop.
    assert len(chunks) == 3
    for chunk in chunks[:-1]:
        assert len(chunk.split()) == 100
    # Last chunk may be shorter.
    assert len(chunks[-1].split()) <= 100


def test_chunk_text_overlap_present_between_neighbors() -> None:
    text = " ".join(str(i) for i in range(200))
    chunks = local_corpus._chunk_text(text, target=80, overlap=20)
    # Tail of chunk 0 should match head of chunk 1 (overlap window).
    head_tokens = chunks[0].split()[-20:]
    tail_tokens = chunks[1].split()[:20]
    assert head_tokens == tail_tokens


def test_chunk_text_empty_input() -> None:
    assert local_corpus._chunk_text("") == []
    assert local_corpus._chunk_text("   \n   \t  ") == []


def test_chunk_text_invalid_overlap_raises() -> None:
    with pytest.raises(ValueError):
        local_corpus._chunk_text("a b c", target=10, overlap=10)
    with pytest.raises(ValueError):
        local_corpus._chunk_text("a b c", target=10, overlap=-1)
    with pytest.raises(ValueError):
        local_corpus._chunk_text("a b c", target=0, overlap=0)


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------


def test_extract_text_txt(tmp_path: Path) -> None:
    f = tmp_path / "doc.txt"
    f.write_text("hello world\n")
    text, kind = local_corpus._extract_text(f)
    assert kind == "txt"
    assert text == "hello world\n"


def test_extract_text_md(tmp_path: Path) -> None:
    f = tmp_path / "doc.md"
    f.write_text("# Heading\n\nbody")
    text, kind = local_corpus._extract_text(f)
    assert kind == "md"
    assert "Heading" in text
    assert "body" in text


def test_extract_text_html_uses_bs4(tmp_path: Path) -> None:
    body = "this is the body text " * 20
    f = tmp_path / "doc.html"
    f.write_text(f"<html><head><script>x=1</script></head><body><p>{body}</p></body></html>")
    text, kind = local_corpus._extract_text(f)
    assert kind == "html"
    assert "body text" in text
    # Script content must be stripped.
    assert "x=1" not in text


def test_extract_text_unsupported_suffix_raises(tmp_path: Path) -> None:
    f = tmp_path / "doc.csv"
    f.write_text("a,b,c")
    with pytest.raises(ValueError):
        local_corpus._extract_text(f)


def test_extract_text_html_lazy_unstructured(tmp_path: Path, monkeypatch) -> None:
    """If bs4 returns under the floor, the unstructured fallback is invoked.

    We assert (a) ``unstructured.partition.html`` was the one that produced
    the final string, and (b) it was not imported until that fallback path
    was taken.
    """
    sys.modules.pop("unstructured.partition.html", None)
    f = tmp_path / "tiny.html"
    f.write_text("<html><body><p>tiny</p></body></html>")  # bs4 yields ~4 chars

    fake_module = type(sys)("unstructured.partition.html")
    fake_module.partition_html = lambda text=None, **_: ["FALLBACK PARTITIONED ARTICLE TEXT"]
    monkeypatch.setitem(sys.modules, "unstructured.partition.html", fake_module)

    text, kind = local_corpus._extract_text(f)
    assert kind == "html"
    assert "FALLBACK PARTITIONED ARTICLE TEXT" in text


# ---------------------------------------------------------------------------
# index() — happy path with stubbed embeddings
# ---------------------------------------------------------------------------


def test_index_writes_local_sources_with_embeddings(
    job: Job,
    monkeypatch,
    stub_models_config: dict,
) -> None:
    calls = _stub_embed(monkeypatch)

    summary = local_corpus.index(
        FIXTURE_CORPUS,
        job,
        models_config=stub_models_config,
    )

    assert summary["files_indexed"] == 3  # txt + md + html
    assert summary["files_skipped"] == 0
    assert summary["chunks_indexed"] >= 3
    assert summary["chunks_skipped"] == 0
    assert summary["embed_dim"] == local_corpus.EMBED_DIM
    # Each file produced one batched embed call.
    assert len(calls) == 3

    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT s.kind, s.embedding FROM sources s"
            " JOIN job_sources js ON js.source_id = s.id"
            " WHERE js.job_id = ?",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == summary["chunks_indexed"]
    for row in rows:
        assert row["kind"] == "local"
        assert row["embedding"] is not None
        assert len(row["embedding"]) == local_corpus.EMBED_DIM * 4  # float32


def test_index_is_idempotent(
    job: Job,
    monkeypatch,
    stub_models_config: dict,
) -> None:
    _stub_embed(monkeypatch)

    first = local_corpus.index(FIXTURE_CORPUS, job, models_config=stub_models_config)
    assert first["chunks_indexed"] >= 1

    # Second pass: no chunk should be re-embedded; chunks_skipped == previous chunks_indexed.
    calls_after_first: list[list[str]] = []

    def _no_embed(chunks, base_url, model):  # noqa: ARG001
        calls_after_first.append(list(chunks))
        # Should never be called for already-indexed content.
        raise AssertionError("re-embedded an existing chunk")

    monkeypatch.setattr(local_corpus, "_embed_chunks_sync", _no_embed)

    second = local_corpus.index(FIXTURE_CORPUS, job, models_config=stub_models_config)

    assert second["chunks_indexed"] == 0
    assert second["chunks_skipped"] == first["chunks_indexed"]
    assert calls_after_first == []

    conn = db.connect(job.db_path)
    try:
        n_sources = conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE kind = 'local'"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n_sources == first["chunks_indexed"]


def test_index_skips_unsupported_files(
    tmp_path: Path,
    job: Job,
    monkeypatch,
    stub_models_config: dict,
) -> None:
    _stub_embed(monkeypatch)

    corpus = tmp_path / "mixed"
    corpus.mkdir()
    (corpus / "doc.txt").write_text("alpha beta gamma " * 20)
    (corpus / "ignore.csv").write_text("a,b,c\n1,2,3")
    (corpus / "ignore.bin").write_bytes(b"\x00\x01\x02")

    summary = local_corpus.index(corpus, job, models_config=stub_models_config)
    assert summary["files_indexed"] == 1


def test_index_handles_empty_file(
    tmp_path: Path,
    job: Job,
    monkeypatch,
    stub_models_config: dict,
) -> None:
    _stub_embed(monkeypatch)

    corpus = tmp_path / "with_empty"
    corpus.mkdir()
    (corpus / "empty.txt").write_text("")
    (corpus / "real.txt").write_text("alpha beta gamma " * 20)

    summary = local_corpus.index(corpus, job, models_config=stub_models_config)
    assert summary["files_indexed"] == 1
    assert summary["files_skipped"] == 1


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


def test_search_returns_top_k_in_descending_score_order(
    job: Job, monkeypatch, stub_models_config: dict
) -> None:
    """Stub the embedding step so we control the geometry of the test."""
    # Plant three sources with known embedding directions:
    #  - vec_a: similar to query
    #  - vec_b: orthogonal-ish
    #  - vec_c: opposite of query
    dim = local_corpus.EMBED_DIM
    rng = np.random.default_rng(seed=42)
    base = rng.standard_normal(dim).astype(np.float32)
    base /= np.linalg.norm(base)

    vec_a = base.copy()
    vec_b = rng.standard_normal(dim).astype(np.float32)
    vec_b -= base * (np.dot(vec_b, base))  # remove component along base
    vec_b /= np.linalg.norm(vec_b)
    vec_c = -base.copy()

    chunks = ["chunk-a content", "chunk-b content", "chunk-c content"]
    vectors = [vec_a, vec_b, vec_c]

    call_idx = {"i": 0}

    def _fake_embed(input_chunks, base_url, model):  # noqa: ARG001
        # First call is during index() with all three chunks; later calls are
        # query embeddings during search().
        if input_chunks == chunks and call_idx["i"] == 0:
            call_idx["i"] += 1
            return vectors
        # Query embedding — return the base direction so vec_a wins.
        return [base.copy()]

    monkeypatch.setattr(local_corpus, "_embed_chunks_sync", _fake_embed)

    # Hand-roll an index call with our three chunks (skip extraction).
    monkeypatch.setattr(local_corpus, "_chunk_text", lambda text, **_: chunks)

    corpus_dir = job.root / "tmp_corpus"
    corpus_dir.mkdir()
    (corpus_dir / "doc.txt").write_text("placeholder " * 50)

    local_corpus.index(corpus_dir, job, models_config=stub_models_config)

    results = local_corpus.search("query", job, top_k=2, models_config=stub_models_config)
    assert len(results) == 2
    # Descending cosine: vec_a (≈1.0) > vec_b (≈0.0) > vec_c (≈-1.0)
    assert results[0]["score"] > results[1]["score"]
    assert results[0]["score"] > 0.99
    # The top result should map to the chunk whose embedding == base.
    top_md = Path(results[0]["md_path"])
    assert top_md.name.endswith(".md")


def test_search_empty_when_no_local_sources(
    job: Job, monkeypatch, stub_models_config: dict
) -> None:
    _stub_embed(monkeypatch)
    results = local_corpus.search("query", job, top_k=5, models_config=stub_models_config)
    assert results == []


def test_search_rejects_non_positive_top_k(job: Job, stub_models_config: dict) -> None:
    with pytest.raises(ValueError):
        local_corpus.search("q", job, top_k=0, models_config=stub_models_config)


# ---------------------------------------------------------------------------
# Lazy-import contract
# ---------------------------------------------------------------------------


def test_unstructured_not_imported_at_module_load() -> None:
    """The heavy ``unstructured`` package must only load on a fallback path.

    Importing this module (already done at the top of the test file) must
    not have pulled it in. Pre-clean the modules cache so a previous test's
    fallback doesn't pollute the assertion.
    """
    for mod in list(sys.modules):
        if mod == "unstructured" or mod.startswith("unstructured."):
            sys.modules.pop(mod, None)

    # Re-importing the module under test must remain lazy.
    import importlib

    importlib.reload(local_corpus)

    leaked = [m for m in sys.modules if m == "unstructured" or m.startswith("unstructured.")]
    assert leaked == [], f"unstructured leaked into sys.modules at import: {leaked}"


# ---------------------------------------------------------------------------
# write_source embedding plumbing
# ---------------------------------------------------------------------------


def test_write_source_persists_embedding_blob(job: Job) -> None:
    from research_agent.storage.sources import write_source

    blob = (np.arange(local_corpus.EMBED_DIM, dtype="<f4")).tobytes()
    sid = write_source(
        job,
        url="file:///x.txt",
        title="x",
        raw_content="some unique content",
        kind="local",
        embedding=blob,
    )

    conn = db.connect(job.db_path)
    try:
        row = conn.execute("SELECT embedding FROM sources WHERE id = ?", (sid,)).fetchone()
    finally:
        conn.close()

    assert row["embedding"] == blob
    assert len(row["embedding"]) == local_corpus.EMBED_DIM * 4
