"""Local corpus indexer (issue #17).

One-shot recursive indexer for ``--corpus PATH`` at ``research start``. Walks
the path, ingests ``*.pdf|*.md|*.txt|*.html|*.htm``, chunks the extracted
text into 4–8K-token windows with a 200-token overlap, embeds each chunk via
the LM Studio ``embeddings`` tier, and writes one :class:`Source` row per
chunk (kind=``local``) with the packed float32 embedding in
``sources.embedding``.

Idempotent by design: each chunk is content-addressed by sha256, so re-
running on the same corpus skips already-indexed chunks (and never re-
spends embedding tokens on them).

Heavy parsers — ``pypdf``, ``unstructured.*``, ``bs4`` — are lazy-imported
only inside their extractor helpers so importing this module on startup
stays cheap.

``search(query, job, top_k)`` runs a numpy cosine over the job's local
sources and returns the top-k matches, sorted descending by score.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np

from research_agent.config import get as cfg_get
from research_agent.llm.router import LMSTUDIO_DEFAULT_BASE_URL, load_models_config
from research_agent.storage import db
from research_agent.storage.jobs import Job
from research_agent.storage.sources import (
    clean_content,
    content_sha256,
    write_source,
)

logger = logging.getLogger(__name__)

CHUNK_TARGET_TOKENS = 6000
CHUNK_OVERLAP_TOKENS = 200
EMBED_DIM = 1024

_SUPPORTED_SUFFIXES = frozenset({".pdf", ".md", ".txt", ".html", ".htm"})
_PYPDF_MIN_CHARS = 100


# ---------------------------------------------------------------------------
# Walking + extraction
# ---------------------------------------------------------------------------


def _walk_corpus(path: Path) -> Iterable[Path]:
    """Yield every supported file under ``path`` in deterministic order."""
    if not path.exists():
        raise FileNotFoundError(f"corpus path does not exist: {path}")
    if path.is_file():
        if path.suffix.lower() in _SUPPORTED_SUFFIXES:
            yield path
        return
    for candidate in sorted(path.rglob("*")):
        if candidate.is_file() and candidate.suffix.lower() in _SUPPORTED_SUFFIXES:
            yield candidate


def _extract_pdf(path: Path) -> str:
    """Extract text from a PDF. Pypdf first, ``unstructured`` lazy fallback."""
    import pypdf  # lazy

    try:
        reader = pypdf.PdfReader(str(path))
        parts = [page.extract_text() or "" for page in reader.pages]
        text = "\n\n".join(p for p in parts if p)
    except Exception as exc:  # noqa: BLE001 — fall through to unstructured
        logger.debug("pypdf failed for %s: %s", path, exc)
        text = ""

    if len(text) >= _PYPDF_MIN_CHARS:
        return text

    # Lazy fallback — only pay the unstructured import when pypdf under-extracts.
    try:
        from unstructured.partition.pdf import partition_pdf  # type: ignore[import-untyped]
    except Exception as exc:  # noqa: BLE001
        logger.debug("unstructured.partition.pdf import failed: %s", exc)
        return text

    try:
        elements = partition_pdf(filename=str(path))
        return "\n\n".join(str(el) for el in elements if str(el).strip())
    except Exception as exc:  # noqa: BLE001
        logger.debug("unstructured.partition.pdf failed for %s: %s", path, exc)
        return text


def _extract_html(path: Path) -> str:
    """Extract text from an HTML file. bs4 first, ``unstructured`` lazy fallback."""
    raw = path.read_text(encoding="utf-8", errors="replace")

    text = ""
    try:
        from bs4 import BeautifulSoup  # lazy

        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n").strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("bs4 parse failed for %s: %s", path, exc)
        text = ""

    if len(text) >= _PYPDF_MIN_CHARS:
        return text

    try:
        from unstructured.partition.html import partition_html  # type: ignore[import-untyped]
    except Exception as exc:  # noqa: BLE001
        logger.debug("unstructured.partition.html import failed: %s", exc)
        return text

    try:
        elements = partition_html(text=raw)
        return "\n\n".join(str(el) for el in elements if str(el).strip())
    except Exception as exc:  # noqa: BLE001
        logger.debug("unstructured.partition.html failed for %s: %s", path, exc)
        return text


def _extract_text(path: Path) -> tuple[str, str]:
    """Return ``(extracted_text, kind_hint)`` for ``path``.

    ``kind_hint`` is the file-type label (``pdf``, ``md``, ``txt``, ``html``)
    purely for diagnostics; the persisted ``Source.kind`` stays ``local``
    so callers can filter on the corpus origin.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path), "pdf"
    if suffix in (".html", ".htm"):
        return _extract_html(path), "html"
    if suffix in (".md", ".txt"):
        return path.read_text(encoding="utf-8", errors="replace"), suffix.lstrip(".")
    raise ValueError(f"unsupported suffix for {path}: {suffix!r}")


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def _chunk_text(
    text: str,
    target: int = CHUNK_TARGET_TOKENS,
    overlap: int = CHUNK_OVERLAP_TOKENS,
) -> list[str]:
    """Split ``text`` into whitespace-token windows of size ``target`` with ``overlap``.

    The token model is intentionally simple: ``str.split()`` whitespace
    tokens. Real BPE counts vary by tokenizer and we don't want to take a
    transformer dependency just to size chunks.
    """
    if target <= 0:
        raise ValueError(f"target must be positive; got {target}")
    if overlap < 0 or overlap >= target:
        raise ValueError(f"overlap must be in [0, target); got {overlap}")

    tokens = text.split()
    if not tokens:
        return []

    step = target - overlap
    chunks: list[str] = []
    for start in range(0, len(tokens), step):
        window = tokens[start : start + target]
        if not window:
            break
        chunks.append(" ".join(window))
        if start + target >= len(tokens):
            break
    return chunks


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


def _resolve_embedding_endpoint(
    models_config: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Return ``(base_url, model_name)`` for the embeddings tier."""
    cfg = models_config or load_models_config(Path("config/models.yaml"))
    tiers = cfg.get("tiers") or {}
    spec = tiers.get("embeddings")
    if not spec:
        raise RuntimeError("config/models.yaml is missing the 'embeddings' tier")
    if spec.get("provider") != "lmstudio":
        raise RuntimeError(
            "local_corpus expects the 'embeddings' tier to use lmstudio; "
            f"got {spec.get('provider')!r}"
        )
    base_url = cfg_get("LMSTUDIO_BASE_URL") or LMSTUDIO_DEFAULT_BASE_URL
    return base_url, spec["model"]


def _embed_chunks_sync(
    chunks: list[str],
    base_url: str,
    model: str,
) -> list[np.ndarray]:
    """POST ``chunks`` to ``{base_url}/embeddings`` and return float32 vectors.

    LM Studio mirrors the OpenAI ``/v1/embeddings`` shape, so we go through
    ``openai.OpenAI`` rather than hand-rolling httpx — the SDK already
    knows about retries, JSON parsing, and the response envelope.
    """
    if not chunks:
        return []

    import openai  # local import keeps module load cheap

    client = openai.OpenAI(base_url=base_url, api_key="lm-studio")
    resp = client.embeddings.create(model=model, input=chunks)

    vectors: list[np.ndarray] = []
    for item in resp.data:
        vec = np.asarray(item.embedding, dtype=np.float32)
        if vec.shape != (EMBED_DIM,):
            raise RuntimeError(
                f"embedding model {model!r} returned dim {vec.shape}; expected ({EMBED_DIM},)"
            )
        vectors.append(vec)
    if len(vectors) != len(chunks):
        raise RuntimeError(
            f"embedding response returned {len(vectors)} vectors for {len(chunks)} chunks"
        )
    return vectors


def _pack_embedding(vec: np.ndarray) -> bytes:
    """Pack a float32 vector to a little-endian BLOB (1024 * 4 = 4096 bytes)."""
    arr = np.asarray(vec, dtype="<f4")
    return arr.tobytes()


def _unpack_embedding(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def index(
    corpus_path: str | Path,
    job: Job,
    *,
    models_config: dict[str, Any] | None = None,
    per_page: bool = False,
) -> dict[str, Any]:
    """Index every supported file under ``corpus_path`` into ``job``.

    Returns a summary dict with ``files_indexed``, ``files_skipped``,
    ``chunks_indexed``, ``chunks_skipped``, ``pages_indexed``,
    ``pages_skipped``, ``per_page``, and ``embed_dim``. Idempotent —
    chunks already present in ``sources`` (matched by sha256) are linked
    to the job without re-embedding.

    When ``per_page=True``, PDFs use :func:`pdf.extract_pages_sync` and
    write one :class:`Source` row per page so the dossier extractor
    (issue #353 / epic #359) can record per-page findings without the
    legacy chunker straddling page boundaries. A page that exceeds the
    chunk-target token budget is sub-chunked **within that page only**
    and the sub-chunk index is recorded as ``metadata.page_chunk``;
    sub-chunking never crosses pages. HTML / MD / TXT files have no
    page concept, so each chunk records ``metadata.page_no = None`` but
    keeps the same ``metadata.parent_file`` so coverage rollup can group
    chunks by their source file.

    When ``per_page=False`` (default), behaviour is unchanged: one
    embedding batch per file, no metadata stamping, ``pages_indexed`` /
    ``pages_skipped`` are zero.
    """
    root = Path(corpus_path)
    base_url, model_name = _resolve_embedding_endpoint(models_config)

    files_indexed = 0
    files_skipped = 0
    chunks_indexed = 0
    chunks_skipped = 0
    pages_indexed = 0
    pages_skipped = 0

    for file_path in _walk_corpus(root):
        if per_page and file_path.suffix.lower() == ".pdf":
            stats = _index_pdf_per_page(
                file_path,
                job,
                base_url=base_url,
                model_name=model_name,
            )
            files_indexed += stats["files_indexed"]
            files_skipped += stats["files_skipped"]
            chunks_indexed += stats["chunks_indexed"]
            chunks_skipped += stats["chunks_skipped"]
            pages_indexed += stats["pages_indexed"]
            pages_skipped += stats["pages_skipped"]
            continue

        try:
            raw_text, _kind_hint = _extract_text(file_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to extract %s: %s", file_path, exc)
            files_skipped += 1
            continue

        cleaned_full = clean_content(raw_text) if raw_text else ""
        if not cleaned_full:
            logger.info("skipping empty file: %s", file_path)
            files_skipped += 1
            continue

        chunks = _chunk_text(cleaned_full)
        if not chunks:
            files_skipped += 1
            continue

        # Pre-check existing sources to avoid re-embedding what we already have.
        chunk_shas = [content_sha256(clean_content(c)) for c in chunks]
        existing = _existing_shas(job.db_path, chunk_shas)

        to_embed_idx = [i for i, sha in enumerate(chunk_shas) if sha not in existing]
        embed_inputs = [chunks[i] for i in to_embed_idx]

        if embed_inputs:
            vectors = _embed_chunks_sync(embed_inputs, base_url, model_name)
        else:
            vectors = []

        new_blobs: dict[int, bytes] = {
            chunk_idx: _pack_embedding(vec)
            for chunk_idx, vec in zip(to_embed_idx, vectors, strict=True)
        }

        file_url = file_path.as_uri()
        # Default path also stamps `parent_file` when per_page=True so
        # callers walking the coverage ledger can group HTML / MD / TXT
        # chunks under a single file unit. page_no stays None because
        # these formats have no page concept.
        file_metadata: dict[str, Any] | None = None
        if per_page:
            file_metadata = {
                "parent_file": file_url,
                "page_no": None,
                "page_chunk": None,
            }

        for i, chunk in enumerate(chunks):
            embedding_blob = new_blobs.get(i)
            if embedding_blob is None:
                chunks_skipped += 1
            else:
                chunks_indexed += 1
            write_source(
                job,
                url=file_url,
                title=file_path.name,
                raw_content=chunk,
                kind="local",
                embedding=embedding_blob,
                metadata=file_metadata,
            )

        files_indexed += 1

    return {
        "files_indexed": files_indexed,
        "files_skipped": files_skipped,
        "chunks_indexed": chunks_indexed,
        "chunks_skipped": chunks_skipped,
        "pages_indexed": pages_indexed,
        "pages_skipped": pages_skipped,
        "per_page": per_page,
        "embed_dim": EMBED_DIM,
    }


def _index_pdf_per_page(
    file_path: Path,
    job: Job,
    *,
    base_url: str,
    model_name: str,
) -> dict[str, int]:
    """Index one PDF as one :class:`Source` row per page (dossier mode).

    Calls :func:`pdf.extract_pages_sync` to get ``[(page_no, text), ...]``
    and writes one row per page. A page whose text exceeds the chunk
    target is sub-chunked **inside that page only**; each sub-chunk
    records the same ``page_no`` and a distinct 1-based ``page_chunk``
    in ``metadata``. The chunker never crosses page boundaries.

    Returns the same shape as :func:`index`'s per-file accumulator so
    the outer loop can fold the counters in unchanged.
    """
    from research_agent.tools import pdf as pdf_mod  # lazy

    empty_summary = {
        "files_indexed": 0,
        "files_skipped": 0,
        "chunks_indexed": 0,
        "chunks_skipped": 0,
        "pages_indexed": 0,
        "pages_skipped": 0,
    }

    try:
        pages = pdf_mod.extract_pages_sync(file_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pdf per-page extract failed for %s: %s", file_path, exc
        )
        return {**empty_summary, "files_skipped": 1}

    if not pages:
        return {**empty_summary, "files_skipped": 1}

    file_url = file_path.as_uri()

    # Materialise per-page sub-chunks first so we can batch-embed every
    # row produced by this file in a single embeddings call. Each entry
    # is ``(page_no, page_chunk_no_or_None, cleaned_chunk_text)``; an
    # empty page contributes no entries (and is counted as a skipped
    # page so coverage rollup can see the gap).
    page_chunks: list[tuple[int, int | None, str]] = []
    pages_indexed = 0
    pages_skipped = 0
    for page_no, page_text in pages:
        cleaned = clean_content(page_text) if page_text else ""
        if not cleaned:
            pages_skipped += 1
            continue
        within_page = _chunk_text(cleaned)
        if not within_page:
            pages_skipped += 1
            continue
        pages_indexed += 1
        if len(within_page) == 1:
            page_chunks.append((page_no, None, within_page[0]))
        else:
            for idx, sub in enumerate(within_page, start=1):
                page_chunks.append((page_no, idx, sub))

    if not page_chunks:
        return {
            **empty_summary,
            "files_skipped": 1,
            "pages_indexed": pages_indexed,
            "pages_skipped": pages_skipped,
        }

    chunk_shas = [content_sha256(clean_content(c)) for _, _, c in page_chunks]
    existing = _existing_shas(job.db_path, chunk_shas)
    to_embed_idx = [
        i for i, sha in enumerate(chunk_shas) if sha not in existing
    ]
    embed_inputs = [page_chunks[i][2] for i in to_embed_idx]

    if embed_inputs:
        vectors = _embed_chunks_sync(embed_inputs, base_url, model_name)
    else:
        vectors = []

    new_blobs: dict[int, bytes] = {
        chunk_idx: _pack_embedding(vec)
        for chunk_idx, vec in zip(to_embed_idx, vectors, strict=True)
    }

    chunks_indexed = 0
    chunks_skipped = 0
    for i, (page_no, page_chunk_no, chunk_text) in enumerate(page_chunks):
        embedding_blob = new_blobs.get(i)
        if embedding_blob is None:
            chunks_skipped += 1
        else:
            chunks_indexed += 1
        write_source(
            job,
            url=file_url,
            title=file_path.name,
            raw_content=chunk_text,
            kind="local",
            embedding=embedding_blob,
            metadata={
                "parent_file": file_url,
                "page_no": page_no,
                "page_chunk": page_chunk_no,
            },
        )

    return {
        "files_indexed": 1,
        "files_skipped": 0,
        "chunks_indexed": chunks_indexed,
        "chunks_skipped": chunks_skipped,
        "pages_indexed": pages_indexed,
        "pages_skipped": pages_skipped,
    }


def _existing_shas(db_path: Path, shas: list[str]) -> set[str]:
    """Return the subset of ``shas`` that already have a ``sources`` row."""
    if not shas:
        return set()
    conn = db.connect(db_path)
    try:
        # Chunked IN-clauses to stay well under SQLite's 999 parameter limit.
        out: set[str] = set()
        for offset in range(0, len(shas), 500):
            batch = shas[offset : offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT sha256 FROM sources WHERE sha256 IN ({placeholders})",
                batch,
            ).fetchall()
            out.update(r["sha256"] for r in rows)
        return out
    finally:
        conn.close()


def search(
    query: str,
    job: Job,
    top_k: int = 10,
    *,
    models_config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return the top-k local sources for ``query``, sorted by cosine descending.

    Each result is ``{source_id, sha256, md_path, score}``. Only sources
    linked to ``job`` with ``kind='local'`` and a non-null ``embedding``
    are scored.
    """
    if top_k <= 0:
        raise ValueError(f"top_k must be positive; got {top_k}")

    base_url, model_name = _resolve_embedding_endpoint(models_config)
    query_vecs = _embed_chunks_sync([query], base_url, model_name)
    if not query_vecs:
        return []
    qvec = query_vecs[0]
    qnorm = float(np.linalg.norm(qvec))
    if qnorm == 0.0:
        return []

    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT s.id, s.sha256, s.md_path, s.embedding"
            " FROM sources s"
            " JOIN job_sources js ON js.source_id = s.id"
            " WHERE js.job_id = ? AND s.kind = 'local' AND s.embedding IS NOT NULL",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        vec = _unpack_embedding(row["embedding"])
        if vec.shape[0] != qvec.shape[0]:
            continue
        vnorm = float(np.linalg.norm(vec))
        if vnorm == 0.0:
            continue
        score = float(np.dot(qvec, vec) / (qnorm * vnorm))
        scored.append(
            (
                score,
                {
                    "source_id": int(row["id"]),
                    "sha256": row["sha256"],
                    "md_path": row["md_path"],
                    "score": score,
                },
            )
        )

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:top_k]]


def index_cornerstone_source(
    job: Job,
    parent_source_id: int,
    sections: list[dict[str, object]],
    *,
    parent_url: str | None = None,
    parent_title: str | None = None,
    models_config: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Build a per-job vector index over ``sections`` (issue #206).

    Each section is chunked by :func:`_chunk_text`, prepended with a
    breadcrumb context line (Anthropic Contextual Retrieval pattern), and
    embedded via the LM Studio ``embeddings`` tier. One ``Source`` row per
    chunk is written with ``kind='cornerstone_chunk'`` and a back-reference
    to ``parent_source_id`` so retrieval can filter to chunks of *this*
    cornerstone document.

    Returns ``{chunks_indexed, chunks_skipped, embed_dim}``. Skips chunks
    whose sha256 is already present in ``sources`` (cross-job dedup) so a
    re-run never re-embeds the same content.
    """
    if not sections:
        return {"chunks_indexed": 0, "chunks_skipped": 0, "embed_dim": EMBED_DIM}

    base_url, model_name = _resolve_embedding_endpoint(models_config)

    chunks: list[str] = []
    contextual_chunks: list[str] = []
    chunk_meta: list[dict[str, Any]] = []
    for section in sections:
        breadcrumb = str(section.get("breadcrumb") or "section")
        body = section.get("text")
        if not isinstance(body, str) or not body.strip():
            continue
        for chunk in _chunk_text(body):
            cleaned = clean_content(chunk)
            if not cleaned:
                continue
            contextual = (
                f"This chunk is from {breadcrumb}. {cleaned}"
            )
            chunks.append(cleaned)
            contextual_chunks.append(contextual)
            chunk_meta.append({"breadcrumb": breadcrumb})

    if not chunks:
        return {"chunks_indexed": 0, "chunks_skipped": 0, "embed_dim": EMBED_DIM}

    # Embed contextual_chunks (the breadcrumb-prefixed text) — that is the
    # vector retrieval will match against. The persisted markdown stores
    # the same contextual form so :func:`search`-style readers see the
    # same breadcrumb when a chunk is recalled.
    chunk_shas = [content_sha256(clean_content(c)) for c in contextual_chunks]
    existing = _existing_shas(job.db_path, chunk_shas)
    to_embed_idx = [i for i, sha in enumerate(chunk_shas) if sha not in existing]
    embed_inputs = [contextual_chunks[i] for i in to_embed_idx]

    if embed_inputs:
        vectors = _embed_chunks_sync(embed_inputs, base_url, model_name)
    else:
        vectors = []

    new_blobs: dict[int, bytes] = {
        chunk_idx: _pack_embedding(vec)
        for chunk_idx, vec in zip(to_embed_idx, vectors, strict=True)
    }

    indexed = 0
    skipped = 0
    for i, contextual in enumerate(contextual_chunks):
        embedding_blob = new_blobs.get(i)
        if embedding_blob is None:
            skipped += 1
        else:
            indexed += 1
        write_source(
            job,
            url=parent_url,
            title=(
                f"{parent_title}: {chunk_meta[i]['breadcrumb']}"
                if parent_title
                else chunk_meta[i]["breadcrumb"]
            ),
            raw_content=contextual,
            kind="cornerstone_chunk",
            embedding=embedding_blob,
            parent_source_id=parent_source_id,
        )
    return {
        "chunks_indexed": indexed,
        "chunks_skipped": skipped,
        "embed_dim": EMBED_DIM,
    }


def cornerstone_query(
    query: str,
    job: Job,
    parent_source_id: int,
    *,
    top_k: int = 8,
    models_config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Cosine-rank ``cornerstone_chunk`` rows under ``parent_source_id`` (issue #206).

    Mirrors :func:`search` but filters by ``kind='cornerstone_chunk'`` and
    the parent-document link so retrieval is scoped to one cornerstone
    document at a time. Returns ``[{source_id, sha256, md_path, score,
    title}]`` sorted by descending score.
    """
    if top_k <= 0:
        raise ValueError(f"top_k must be positive; got {top_k}")

    base_url, model_name = _resolve_embedding_endpoint(models_config)
    query_vecs = _embed_chunks_sync([query], base_url, model_name)
    if not query_vecs:
        return []
    qvec = query_vecs[0]
    qnorm = float(np.linalg.norm(qvec))
    if qnorm == 0.0:
        return []

    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT s.id, s.sha256, s.md_path, s.title, s.embedding"
            " FROM sources s"
            " JOIN job_sources js ON js.source_id = s.id"
            " WHERE js.job_id = ? AND s.kind = 'cornerstone_chunk'"
            " AND s.parent_source_id = ? AND s.embedding IS NOT NULL",
            (job.id, parent_source_id),
        ).fetchall()
    finally:
        conn.close()

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        vec = _unpack_embedding(row["embedding"])
        if vec.shape[0] != qvec.shape[0]:
            continue
        vnorm = float(np.linalg.norm(vec))
        if vnorm == 0.0:
            continue
        score = float(np.dot(qvec, vec) / (qnorm * vnorm))
        scored.append(
            (
                score,
                {
                    "source_id": int(row["id"]),
                    "sha256": row["sha256"],
                    "md_path": row["md_path"],
                    "title": row["title"],
                    "score": score,
                },
            )
        )
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:top_k]]


__all__ = [
    "CHUNK_OVERLAP_TOKENS",
    "CHUNK_TARGET_TOKENS",
    "EMBED_DIM",
    "cornerstone_query",
    "index",
    "index_cornerstone_source",
    "search",
]


# ---------------------------------------------------------------------------
# Internal helpers exposed for tests
# ---------------------------------------------------------------------------


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
