"""End-to-end smoke for page-grain corpus ingestion (issue #354).

Exercises the full M0 wiring in one go: walk a mixed-format fixture
corpus, route the PDF through :func:`pdf.extract_pages_sync` (M0.1),
write per-page Sources via :func:`local_corpus.index` in
``per_page=True`` mode (M0.2), and recover the metadata dict via
:func:`storage.sources.read_source_metadata` (M0.3). Catches
integration drift before M1 layers coverage gating on top of these
pieces.

No network calls; the embedder is stubbed so the test stays fast and
deterministic. Real PDF parsing runs against the bundled arxiv
fixture; a second test stubs :func:`pdf.extract_pages_sync` so the
multi-page grouping assertion is meaningful even though the bundled
fixture is single-page.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pytest

from research_agent.storage import db
from research_agent.storage.jobs import Job
from research_agent.storage.sources import read_source_metadata
from research_agent.tools import local_corpus

FIXTURE_CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "corpus_dossier"
PDF_FIXTURE = FIXTURE_CORPUS / "paper.pdf"
HTML_FIXTURE = FIXTURE_CORPUS / "notes.html"
MD_FIXTURE = FIXTURE_CORPUS / "notes.md"


@pytest.fixture
def stub_models_config() -> dict:
    return {
        "tiers": {
            "embeddings": {
                "provider": "lmstudio",
                "model": "qwen3-embedding-4b",
                "timeout_s": 60,
            }
        }
    }


@pytest.fixture
def job(tmp_path: Path) -> Job:
    db_path = tmp_path / "index.sqlite"
    db.migrate(path=db_path).close()
    return Job.create(
        {"goal": "dossier-mode ingestion smoke"},
        jobs_root=tmp_path / "jobs",
        db_path=db_path,
        today=date(2026, 5, 19),
    )


@pytest.fixture
def stub_embedder(monkeypatch):
    """Deterministic numpy embedder so no LM Studio is required."""
    rng = np.random.default_rng(seed=0)
    calls: list[list[str]] = []

    def _fake(chunks, base_url, model):  # noqa: ARG001
        calls.append(list(chunks))
        return [
            rng.standard_normal(local_corpus.EMBED_DIM).astype(np.float32)
            for _ in chunks
        ]

    monkeypatch.setattr(local_corpus, "_embed_chunks_sync", _fake)
    return calls


def test_fixture_corpus_layout_present() -> None:
    """Guard the fixture set so a later cleanup doesn't silently delete files."""
    assert PDF_FIXTURE.exists(), "missing PDF fixture"
    assert HTML_FIXTURE.exists(), "missing HTML fixture"
    assert MD_FIXTURE.exists(), "missing Markdown fixture"


def test_per_page_ingestion_groups_sources_by_parent_file(
    job: Job, stub_models_config: dict, stub_embedder
) -> None:
    """Index the fixture corpus end-to-end and assert dossier-mode invariants.

    The bundled arxiv PDF is single-page so ``pages_indexed`` is 1 for
    the PDF row; the multi-page assertion lives in the stubbed test
    below. What this test covers is the full wiring: real PDF parsing
    via M0.1, real per-page Source writes via M0.2, real sidecar
    round-trip via M0.3.
    """
    summary = local_corpus.index(
        FIXTURE_CORPUS,
        job,
        models_config=stub_models_config,
        per_page=True,
    )

    assert summary["per_page"] is True
    assert summary["files_indexed"] == 3
    assert summary["files_skipped"] == 0
    assert summary["pages_indexed"] >= 1  # PDF contributed at least one page
    assert summary["pages_skipped"] == 0
    assert summary["chunks_indexed"] >= 3  # PDF + HTML + MD

    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT s.sha256, s.url FROM sources s"
            " JOIN job_sources js ON js.source_id = s.id"
            " WHERE js.job_id = ?"
            " ORDER BY s.id ASC",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == summary["chunks_indexed"]

    pdf_uri = PDF_FIXTURE.as_uri()
    html_uri = HTML_FIXTURE.as_uri()
    md_uri = MD_FIXTURE.as_uri()
    expected_parents = {pdf_uri, html_uri, md_uri}

    parent_files: set[str] = set()
    pdf_rows: list[dict] = []
    non_pdf_rows: list[dict] = []
    for row in rows:
        meta = read_source_metadata(job, row["sha256"])
        assert "parent_file" in meta
        parent_files.add(meta["parent_file"])
        if meta["parent_file"] == pdf_uri:
            pdf_rows.append(meta)
        else:
            non_pdf_rows.append(meta)

    # Per the M0.4 acceptance criterion (adapted: metadata lives in
    # per-job sidecars rather than a SQL column, so we group via the
    # sidecar reader instead of a SQL-JSON expression).
    assert parent_files == expected_parents

    # PDF rows must have a 1-based page_no; HTML/MD rows must have
    # page_no=None and page_chunk=None.
    assert pdf_rows, "no PDF source rows recorded"
    seen_pages: list[int] = []
    for meta in pdf_rows:
        assert isinstance(meta.get("page_no"), int), (
            f"PDF row missing integer page_no: {meta}"
        )
        seen_pages.append(meta["page_no"])
    # Real single-page fixture → seen_pages == [1]; multi-page logic
    # is covered by the stubbed test below.
    assert min(seen_pages) >= 1
    assert sorted(seen_pages) == list(range(1, max(seen_pages) + 1))

    assert non_pdf_rows, "no HTML/MD rows recorded"
    for meta in non_pdf_rows:
        assert meta.get("page_no") is None
        assert meta.get("page_chunk") is None
        assert meta["parent_file"] in {html_uri, md_uri}


def test_per_page_ingestion_groups_multi_page_pdf(
    job: Job, stub_models_config: dict, stub_embedder, monkeypatch
) -> None:
    """Multi-page assertion: stub the per-page extractor to return N pages.

    Keeps the rest of the pipeline real — walk, sidecar write, sidecar
    read — so the test still proves dossier-mode wiring end-to-end.
    The bundled arxiv fixture is single-page in real life; rather than
    bake a multi-page PDF into the repo we stub the parsing step and
    let M0.2 / M0.3 do their real work.
    """
    from research_agent.tools import pdf as pdf_mod

    fake_pages = [
        (1, "Page one fixture body about Alpha intelligence " * 20),
        (2, "Page two fixture body about Beta procurement " * 20),
        (3, "Page three fixture body about Gamma incidents " * 20),
    ]
    monkeypatch.setattr(
        pdf_mod, "extract_pages_sync", lambda *a, **k: list(fake_pages)
    )

    summary = local_corpus.index(
        FIXTURE_CORPUS,
        job,
        models_config=stub_models_config,
        per_page=True,
    )

    assert summary["files_indexed"] == 3
    assert summary["pages_indexed"] == 3  # stubbed PDF -> exactly 3 pages
    pdf_uri = PDF_FIXTURE.as_uri()

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

    pdf_pages: list[int] = []
    other_parents: set[str] = set()
    for row in rows:
        meta = read_source_metadata(job, row["sha256"])
        if meta["parent_file"] == pdf_uri:
            pdf_pages.append(meta["page_no"])
        else:
            other_parents.add(meta["parent_file"])

    # Acceptance: one Source per page, in ascending order.
    assert pdf_pages == [1, 2, 3]
    # And the two non-PDF formats still produced their own rows under
    # distinct parent_file URIs.
    assert other_parents == {HTML_FIXTURE.as_uri(), MD_FIXTURE.as_uri()}


def test_per_page_ingestion_completes_quickly(
    job: Job, stub_models_config: dict, stub_embedder
) -> None:
    """The smoke must run in well under 10s — assert a generous budget."""
    import time

    started = time.monotonic()
    local_corpus.index(
        FIXTURE_CORPUS,
        job,
        models_config=stub_models_config,
        per_page=True,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 10.0, f"dossier ingestion smoke too slow: {elapsed:.2f}s"
