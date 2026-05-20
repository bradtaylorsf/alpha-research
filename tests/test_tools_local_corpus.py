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
# PDF delegation to the layered pipeline (issue #376)
# ---------------------------------------------------------------------------


def test_extract_text_pdf_delegates_to_layered_pipeline(tmp_path: Path, monkeypatch) -> None:
    """PDFs flow through ``tools.pdf.extract_sync`` with resolved caps."""
    from research_agent.tools import pdf as pdf_mod

    captured: dict[str, Any] = {}

    def _fake_extract_sync(path, **kwargs):
        captured["path"] = path
        captured.update(kwargs)
        return "DELEGATED MARKDOWN"

    monkeypatch.setattr(pdf_mod, "extract_sync", _fake_extract_sync)

    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4\n")
    text, kind = local_corpus._extract_text(f)
    assert kind == "pdf"
    assert text == "DELEGATED MARKDOWN"
    assert captured["hybrid_pages"] is False
    assert captured["max_pages"] == pdf_mod.CORPUS_MAX_PAGES
    assert captured["max_chars"] == pdf_mod.CORPUS_MAX_CHARS


def test_extract_text_pdf_forwards_intake_knobs(tmp_path: Path, monkeypatch) -> None:
    """``job.intake`` values flow through the resolvers into ``extract_sync``."""
    from research_agent.tools import pdf as pdf_mod

    captured: dict[str, Any] = {}

    def _fake_extract_sync(path, **kwargs):
        captured.update(kwargs)
        return ""

    monkeypatch.setattr(pdf_mod, "extract_sync", _fake_extract_sync)

    class _StubJob:
        intake = {
            "pdf_hybrid_pages": True,
            "pdf_max_pages": 50,
            "pdf_max_chars": 12345,
        }

    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4\n")
    local_corpus._extract_text(f, job=_StubJob())  # type: ignore[arg-type]

    assert captured["hybrid_pages"] is True
    assert captured["max_pages"] == 50
    assert captured["max_chars"] == 12345


def test_clamp_pdf_max_pages_enforces_hard_cap() -> None:
    """Misconfigured intake values can't push extraction beyond the hard cap."""
    from research_agent.tools.pdf import MAX_PAGES_HARD_CAP

    assert local_corpus._clamp_pdf_max_pages(0) == 1
    assert local_corpus._clamp_pdf_max_pages(-5) == 1
    assert local_corpus._clamp_pdf_max_pages(MAX_PAGES_HARD_CAP) == MAX_PAGES_HARD_CAP
    assert (
        local_corpus._clamp_pdf_max_pages(MAX_PAGES_HARD_CAP + 1000) == MAX_PAGES_HARD_CAP
    )


def test_resolve_pdf_hybrid_pages_priority() -> None:
    """Explicit kwarg > intake > default False."""

    class _StubJob:
        intake = {"pdf_hybrid_pages": True}

    assert local_corpus._resolve_pdf_hybrid_pages(None, None) is False
    assert local_corpus._resolve_pdf_hybrid_pages(_StubJob(), None) is True  # type: ignore[arg-type]
    assert local_corpus._resolve_pdf_hybrid_pages(_StubJob(), False) is False  # type: ignore[arg-type]


def test_embed_dim_matches_qwen3_embedding_4b_dwq() -> None:
    """The 768-d setting must move in lock-step with the embeddings model
    in ``config/models.yaml`` (issue #375). Tripping this assertion means
    someone changed the embedding model without re-indexing or vice versa.
    """
    assert local_corpus.EMBED_DIM == 768


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


# ---------------------------------------------------------------------------
# Cornerstone vector index (issue #206)
# ---------------------------------------------------------------------------


def test_index_cornerstone_source_writes_chunk_rows(
    job: Job, monkeypatch, stub_models_config: dict
) -> None:
    """Each section's chunks land as ``cornerstone_chunk`` rows linked to parent.

    The breadcrumb context is prepended before embedding (Anthropic
    contextual-retrieval pattern), so the persisted markdown must
    carry the ``This chunk is from <breadcrumb>.`` prefix.
    """
    from research_agent.storage.sources import write_source

    parent_id = write_source(
        job,
        url="https://example.test/cornerstone.pdf",
        title="Cornerstone",
        raw_content="parent doc body",
        kind="pdf",
    )

    _stub_embed(monkeypatch)
    monkeypatch.setattr(
        local_corpus,
        "_resolve_embedding_endpoint",
        lambda *_a, **_k: ("http://x", "stub"),
    )

    sections = [
        {
            "breadcrumb": "Cornerstone > DOJ chapter (pages 1-10)",
            "text": "DOJ section body " * 200,
            "page_start": 1,
            "page_end": 10,
            "structured": True,
        },
        {
            "breadcrumb": "Cornerstone > EPA chapter (pages 11-20)",
            "text": "EPA section body " * 200,
            "page_start": 11,
            "page_end": 20,
            "structured": True,
        },
    ]

    summary = local_corpus.index_cornerstone_source(
        job,
        parent_id,
        sections,
        parent_url="https://example.test/cornerstone.pdf",
        parent_title="Cornerstone",
        models_config=stub_models_config,
    )

    assert summary["chunks_indexed"] >= 2
    assert summary["embed_dim"] == local_corpus.EMBED_DIM

    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT id, kind, title, parent_source_id, embedding"
            " FROM sources WHERE kind = 'cornerstone_chunk' ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) >= 2
    for row in rows:
        assert row["parent_source_id"] == parent_id
        assert row["embedding"] is not None
        assert "Cornerstone" in (row["title"] or "")


def test_cornerstone_query_filters_by_parent(
    job: Job, monkeypatch, stub_models_config: dict
) -> None:
    """``cornerstone_query`` only ranks chunks under the given parent.

    Sources with the same ``cornerstone_chunk`` kind but a different
    parent must not appear in results so a multi-document run keeps
    its retrieval scoped to the queried document.
    """
    from research_agent.storage.sources import write_source

    parent_a = write_source(
        job, url="https://x/a.pdf", title="A", raw_content="A doc", kind="pdf"
    )
    parent_b = write_source(
        job, url="https://x/b.pdf", title="B", raw_content="B doc", kind="pdf"
    )

    _stub_embed(monkeypatch)
    monkeypatch.setattr(
        local_corpus,
        "_resolve_embedding_endpoint",
        lambda *_a, **_k: ("http://x", "stub"),
    )

    local_corpus.index_cornerstone_source(
        job,
        parent_a,
        [{"breadcrumb": "A > Intro", "text": "alpha alpha alpha " * 100}],
        parent_url="https://x/a.pdf",
        parent_title="A",
        models_config=stub_models_config,
    )
    local_corpus.index_cornerstone_source(
        job,
        parent_b,
        [{"breadcrumb": "B > Intro", "text": "beta beta beta " * 100}],
        parent_url="https://x/b.pdf",
        parent_title="B",
        models_config=stub_models_config,
    )

    hits_a = local_corpus.cornerstone_query(
        "alpha", job, parent_a, top_k=5, models_config=stub_models_config
    )
    hits_b = local_corpus.cornerstone_query(
        "beta", job, parent_b, top_k=5, models_config=stub_models_config
    )

    # Each query must only touch its own parent's chunks.
    conn = db.connect(job.db_path)
    try:

        def _parent_of(sid: int) -> int | None:
            row = conn.execute(
                "SELECT parent_source_id FROM sources WHERE id = ?", (sid,)
            ).fetchone()
            return int(row["parent_source_id"]) if row and row["parent_source_id"] else None

        assert hits_a, "expected at least one hit for parent A"
        assert hits_b, "expected at least one hit for parent B"
        for hit in hits_a:
            assert _parent_of(hit["source_id"]) == parent_a
        for hit in hits_b:
            assert _parent_of(hit["source_id"]) == parent_b
    finally:
        conn.close()
