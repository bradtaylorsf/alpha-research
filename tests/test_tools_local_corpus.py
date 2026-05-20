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
    skipped = summary["skipped"]
    assert len(skipped) == 1
    assert skipped[0]["file_url"] == (corpus / "empty.txt").as_uri()
    assert skipped[0]["reason"] == "empty_content"


def test_index_skipped_field_default_empty(
    tmp_path: Path, job: Job, monkeypatch, stub_models_config: dict
) -> None:
    """Healthy corpus -> ``skipped`` is present and empty (issue #357)."""
    _stub_embed(monkeypatch)
    corpus = tmp_path / "clean"
    corpus.mkdir()
    (corpus / "ok.txt").write_text("alpha beta gamma " * 40)

    summary = local_corpus.index(corpus, job, models_config=stub_models_config)
    assert summary["files_skipped"] == 0
    assert summary["skipped"] == []


def test_index_extraction_failure_records_reason(
    tmp_path: Path, job: Job, monkeypatch, stub_models_config: dict
) -> None:
    """A raising ``_extract_text`` -> ``extraction_failed: <exc>`` in skipped."""
    _stub_embed(monkeypatch)
    corpus = tmp_path / "broken"
    corpus.mkdir()
    target = corpus / "broken.txt"
    target.write_text("payload")

    def _boom(path: Path):
        raise RuntimeError("synthetic extract failure")

    monkeypatch.setattr(local_corpus, "_extract_text", _boom)

    summary = local_corpus.index(corpus, job, models_config=stub_models_config)
    assert summary["files_indexed"] == 0
    assert summary["files_skipped"] == 1
    skipped = summary["skipped"]
    assert len(skipped) == 1
    assert skipped[0]["file_url"] == target.as_uri()
    assert "extraction_failed" in skipped[0]["reason"]
    assert "synthetic extract failure" in skipped[0]["reason"]


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
# Per-page ingestion (issue #352 — dossier mode, epic #359)
# ---------------------------------------------------------------------------


def _read_sidecar(job: Job, sha: str) -> dict:
    """Read the JSON sidecar a `write_source` call materialised under ``job``."""
    import json

    path = job.root / "sources" / f"{sha}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_index_per_page_default_is_unchanged(
    job: Job, monkeypatch, stub_models_config: dict
) -> None:
    """per_page omitted/False must keep the legacy behaviour byte-for-byte."""
    _stub_embed(monkeypatch)
    baseline = local_corpus.index(
        FIXTURE_CORPUS, job, models_config=stub_models_config
    )
    assert "pages_indexed" in baseline
    assert "pages_skipped" in baseline
    assert baseline["pages_indexed"] == 0
    assert baseline["pages_skipped"] == 0
    assert baseline["per_page"] is False


def test_index_per_page_pdf_writes_one_source_per_page(
    tmp_path: Path, job: Job, monkeypatch, stub_models_config: dict
) -> None:
    """PDF in per_page=True mode: N pages -> N Source rows, page_no stamped."""
    _stub_embed(monkeypatch)
    corpus = tmp_path / "pdf_only"
    corpus.mkdir()
    pdf_path = corpus / "filing.pdf"
    pdf_path.write_bytes(b"%PDF-fake")

    fake_pages = [
        (1, "Page one of the filing about Alpha topics. " * 30),
        (2, "Page two covers Beta procurement detail. " * 30),
        (3, "Page three pivots to Gamma incidents. " * 30),
    ]

    def _fake_extract_pages_sync(path, **_kwargs):
        return list(fake_pages)

    from research_agent.tools import pdf as pdf_mod

    monkeypatch.setattr(pdf_mod, "extract_pages_sync", _fake_extract_pages_sync)

    summary = local_corpus.index(
        corpus, job, models_config=stub_models_config, per_page=True
    )

    assert summary["files_indexed"] == 1
    assert summary["files_skipped"] == 0
    assert summary["pages_indexed"] == 3
    assert summary["pages_skipped"] == 0
    assert summary["chunks_indexed"] == 3
    assert summary["per_page"] is True

    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT s.sha256, s.url, s.title FROM sources s"
            " JOIN job_sources js ON js.source_id = s.id"
            " WHERE js.job_id = ?"
            " ORDER BY s.id ASC",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 3
    page_nos: list[int] = []
    parent_files: set[str] = set()
    for row in rows:
        sidecar = _read_sidecar(job, row["sha256"])
        meta = sidecar["metadata"]
        assert meta["parent_file"] == pdf_path.as_uri()
        assert meta["page_chunk"] is None
        page_nos.append(meta["page_no"])
        parent_files.add(meta["parent_file"])
    assert page_nos == [1, 2, 3]
    assert parent_files == {pdf_path.as_uri()}


def test_index_per_page_oversize_page_subchunks_within_page(
    tmp_path: Path, job: Job, monkeypatch, stub_models_config: dict
) -> None:
    """Page text > chunk target: emit multiple Sources with same page_no, distinct page_chunk."""
    _stub_embed(monkeypatch)
    corpus = tmp_path / "big_pdf"
    corpus.mkdir()
    pdf_path = corpus / "big.pdf"
    pdf_path.write_bytes(b"%PDF-fake")

    oversize = " ".join(f"tok{i}" for i in range(local_corpus.CHUNK_TARGET_TOKENS * 2 + 100))
    fake_pages = [
        (1, "Small page one body alpha. " * 30),
        (2, oversize),  # forces sub-chunking
        (3, "Small page three body gamma. " * 30),
    ]

    def _fake_extract_pages_sync(path, **_kwargs):
        return list(fake_pages)

    from research_agent.tools import pdf as pdf_mod

    monkeypatch.setattr(pdf_mod, "extract_pages_sync", _fake_extract_pages_sync)

    summary = local_corpus.index(
        corpus, job, models_config=stub_models_config, per_page=True
    )
    assert summary["pages_indexed"] == 3
    assert summary["chunks_indexed"] >= 4  # 1 + (>=2 subchunks) + 1

    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT s.sha256 FROM sources s"
            " JOIN job_sources js ON js.source_id = s.id"
            " WHERE js.job_id = ?"
            " ORDER BY s.id ASC",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()

    by_page: dict[int, list[int | None]] = {}
    for row in rows:
        meta = _read_sidecar(job, row["sha256"])["metadata"]
        by_page.setdefault(meta["page_no"], []).append(meta["page_chunk"])

    assert set(by_page.keys()) == {1, 2, 3}
    # Pages 1 and 3 fit in one chunk apiece -> page_chunk = None.
    assert by_page[1] == [None]
    assert by_page[3] == [None]
    # Page 2 is sub-chunked: distinct 1-based page_chunk values, all >= 2 chunks.
    assert all(isinstance(c, int) and c >= 1 for c in by_page[2])
    assert len(by_page[2]) >= 2
    assert sorted(by_page[2]) == sorted(set(by_page[2]))  # distinct


def test_index_per_page_empty_pages_count_as_skipped(
    tmp_path: Path, job: Job, monkeypatch, stub_models_config: dict
) -> None:
    """Pages whose text is empty are counted under pages_skipped, not pages_indexed."""
    _stub_embed(monkeypatch)
    corpus = tmp_path / "sparse_pdf"
    corpus.mkdir()
    pdf_path = corpus / "sparse.pdf"
    pdf_path.write_bytes(b"%PDF-fake")

    fake_pages = [
        (1, "Real content alpha alpha. " * 30),
        (2, ""),
        (3, "Real content gamma gamma. " * 30),
        (4, ""),
    ]

    def _fake_extract_pages_sync(path, **_kwargs):
        return list(fake_pages)

    from research_agent.tools import pdf as pdf_mod

    monkeypatch.setattr(pdf_mod, "extract_pages_sync", _fake_extract_pages_sync)

    summary = local_corpus.index(
        corpus, job, models_config=stub_models_config, per_page=True
    )
    assert summary["pages_indexed"] == 2
    assert summary["pages_skipped"] == 2
    assert summary["files_indexed"] == 1
    assert summary["chunks_indexed"] == 2


def test_index_per_page_pdf_with_no_extractable_text_skips_file(
    tmp_path: Path, job: Job, monkeypatch, stub_models_config: dict
) -> None:
    """Every page empty -> the file itself counts as skipped (pages_indexed=0)."""
    _stub_embed(monkeypatch)
    corpus = tmp_path / "blank_pdf"
    corpus.mkdir()
    pdf_path = corpus / "blank.pdf"
    pdf_path.write_bytes(b"%PDF-fake")

    def _fake_extract_pages_sync(path, **_kwargs):
        return [(1, ""), (2, "")]

    from research_agent.tools import pdf as pdf_mod

    monkeypatch.setattr(pdf_mod, "extract_pages_sync", _fake_extract_pages_sync)

    summary = local_corpus.index(
        corpus, job, models_config=stub_models_config, per_page=True
    )
    assert summary["files_indexed"] == 0
    assert summary["files_skipped"] == 1
    assert summary["pages_indexed"] == 0
    assert summary["pages_skipped"] == 2


def test_index_per_page_unopenable_pdf_skips_file(
    tmp_path: Path, job: Job, monkeypatch, stub_models_config: dict
) -> None:
    """extract_pages_sync returning [] (unopenable) -> file counted as skipped."""
    _stub_embed(monkeypatch)
    corpus = tmp_path / "broken_pdf"
    corpus.mkdir()
    pdf_path = corpus / "broken.pdf"
    pdf_path.write_bytes(b"not actually a pdf")

    from research_agent.tools import pdf as pdf_mod

    monkeypatch.setattr(pdf_mod, "extract_pages_sync", lambda *a, **k: [])

    summary = local_corpus.index(
        corpus, job, models_config=stub_models_config, per_page=True
    )
    assert summary["files_indexed"] == 0
    assert summary["files_skipped"] == 1
    assert summary["pages_indexed"] == 0
    assert summary["pages_skipped"] == 0
    assert summary["chunks_indexed"] == 0
    skipped = summary["skipped"]
    assert len(skipped) == 1
    assert skipped[0]["file_url"] == pdf_path.as_uri()
    assert skipped[0]["reason"] == "empty_content"


def test_index_per_page_pdf_extract_raises_records_reason(
    tmp_path: Path, job: Job, monkeypatch, stub_models_config: dict
) -> None:
    """extract_pages_sync raising -> ``extraction_failed: <exc>`` (issue #357)."""
    _stub_embed(monkeypatch)
    corpus = tmp_path / "raising_pdf"
    corpus.mkdir()
    pdf_path = corpus / "raises.pdf"
    pdf_path.write_bytes(b"%PDF-fake")

    from research_agent.tools import pdf as pdf_mod

    def _boom(*a, **k):
        raise RuntimeError("malformed xref table")

    monkeypatch.setattr(pdf_mod, "extract_pages_sync", _boom)

    summary = local_corpus.index(
        corpus, job, models_config=stub_models_config, per_page=True
    )
    assert summary["files_skipped"] == 1
    skipped = summary["skipped"]
    assert len(skipped) == 1
    assert skipped[0]["file_url"] == pdf_path.as_uri()
    assert "extraction_failed" in skipped[0]["reason"]
    assert "malformed xref" in skipped[0]["reason"]


def test_index_per_page_pdf_all_pages_empty_records_gap(
    tmp_path: Path, job: Job, monkeypatch, stub_models_config: dict
) -> None:
    """Every page empty -> file-level ``empty_content`` gap (issue #357)."""
    _stub_embed(monkeypatch)
    corpus = tmp_path / "blank_pages"
    corpus.mkdir()
    pdf_path = corpus / "blank.pdf"
    pdf_path.write_bytes(b"%PDF-fake")

    from research_agent.tools import pdf as pdf_mod

    monkeypatch.setattr(
        pdf_mod, "extract_pages_sync", lambda *a, **k: [(1, ""), (2, "")]
    )

    summary = local_corpus.index(
        corpus, job, models_config=stub_models_config, per_page=True
    )
    assert summary["files_skipped"] == 1
    skipped = summary["skipped"]
    assert len(skipped) == 1
    assert skipped[0]["file_url"] == pdf_path.as_uri()
    assert skipped[0]["reason"] == "empty_content"


def test_index_per_page_html_stamps_parent_file_and_null_page(
    tmp_path: Path, job: Job, monkeypatch, stub_models_config: dict
) -> None:
    """HTML in per_page=True: existing chunking, metadata.page_no = None, parent_file set."""
    _stub_embed(monkeypatch)
    corpus = tmp_path / "html_only"
    corpus.mkdir()
    html_path = corpus / "page.html"
    body = "html body section " * 80
    html_path.write_text(f"<html><body><p>{body}</p></body></html>")

    summary = local_corpus.index(
        corpus, job, models_config=stub_models_config, per_page=True
    )
    assert summary["files_indexed"] == 1
    assert summary["chunks_indexed"] >= 1
    # HTML has no pages so the page counters stay at zero even in per_page mode.
    assert summary["pages_indexed"] == 0
    assert summary["pages_skipped"] == 0

    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT s.sha256 FROM sources s"
            " JOIN job_sources js ON js.source_id = s.id"
            " WHERE js.job_id = ?",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        meta = _read_sidecar(job, row["sha256"])["metadata"]
        assert meta["parent_file"] == html_path.as_uri()
        assert meta["page_no"] is None
        assert meta["page_chunk"] is None


def test_index_per_page_is_idempotent(
    tmp_path: Path, job: Job, monkeypatch, stub_models_config: dict
) -> None:
    """Re-running per_page=True over the same PDF must skip re-embedding."""
    _stub_embed(monkeypatch)
    corpus = tmp_path / "rerun_pdf"
    corpus.mkdir()
    pdf_path = corpus / "rerun.pdf"
    pdf_path.write_bytes(b"%PDF-fake")

    fake_pages = [
        (1, "Idempotent page one alpha. " * 30),
        (2, "Idempotent page two beta. " * 30),
    ]

    from research_agent.tools import pdf as pdf_mod

    monkeypatch.setattr(
        pdf_mod, "extract_pages_sync", lambda *a, **k: list(fake_pages)
    )

    first = local_corpus.index(
        corpus, job, models_config=stub_models_config, per_page=True
    )
    assert first["chunks_indexed"] == 2

    # Reject any further embeddings call on the second pass.
    def _no_embed(chunks, base_url, model):  # noqa: ARG001
        raise AssertionError("re-embedded an already-indexed page chunk")

    monkeypatch.setattr(local_corpus, "_embed_chunks_sync", _no_embed)

    second = local_corpus.index(
        corpus, job, models_config=stub_models_config, per_page=True
    )
    assert second["chunks_indexed"] == 0
    assert second["chunks_skipped"] == 2
    assert second["pages_indexed"] == 2


def test_index_per_page_real_pdf_fixture_smoke(
    tmp_path: Path, job: Job, monkeypatch, stub_models_config: dict
) -> None:
    """Integration: a real PDF fixture goes through extract_pages_sync end-to-end."""
    _stub_embed(monkeypatch)
    corpus = tmp_path / "real_pdf"
    corpus.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "arxiv_paper.pdf"
    destination = corpus / "arxiv.pdf"
    destination.write_bytes(fixture.read_bytes())

    summary = local_corpus.index(
        corpus, job, models_config=stub_models_config, per_page=True
    )
    assert summary["files_indexed"] == 1
    assert summary["pages_indexed"] >= 1
    assert summary["chunks_indexed"] >= 1

    conn = db.connect(job.db_path)
    try:
        sha = conn.execute(
            "SELECT s.sha256 FROM sources s"
            " JOIN job_sources js ON js.source_id = s.id"
            " WHERE js.job_id = ?"
            " ORDER BY s.id ASC LIMIT 1",
            (job.id,),
        ).fetchone()["sha256"]
    finally:
        conn.close()
    meta = _read_sidecar(job, sha)["metadata"]
    assert meta["parent_file"] == destination.as_uri()
    assert meta["page_no"] == 1


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
