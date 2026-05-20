"""Source ingestion with content-hash dedup across jobs.

Implements the §4 layout: per-job ``sources/<sha256>.md`` (cleaned content)
plus ``<sha256>.json`` sidecar (url, fetched_at, archive_url, kind, metadata),
and the §10 schema's ``sources`` + ``job_sources`` tables. The same content
fetched by two different jobs collapses to a single ``sources`` row with two
``job_sources`` links — the markdown + sidecar are materialised under
**every** job that touched the source (so a job folder is self-contained on
disk; see ``write_source``).

Cleaning is intentionally minimal in v1 (line-ending normalization,
horizontal whitespace collapse, strip). Richer extraction — readability,
boilerplate stripping — belongs in the fetch tool layer (``tools/web_fetch``)
upstream of this module.

Metadata round-trip (issue #353)
--------------------------------

``write_source(metadata=...)`` persists the supplied dict to the per-job
JSON sidecar (``sources/<sha>.json`` under ``metadata``). The shape is
open-ended — any extractor / connector / orchestrator pass may stash
provenance there — but the codebase has converged on these key
conventions:

- ``parent_file`` (str): canonical URI of the source file the row belongs
  to. Set by the per-page corpus ingester (issue #352) and by connectors
  that ingest a single document as multiple chunks so the dossier rollup
  (epic #359) can group rows by file.
- ``page_no`` (int | None): 1-based page number when the row came from a
  per-page PDF extraction. ``None`` for HTML / MD / TXT / other formats
  that have no page concept.
- ``page_chunk`` (int | None): 1-based sub-chunk index inside a single
  page when a page exceeds the chunk-target token budget. ``None`` when
  the whole page fits in one chunk. The chunker never crosses pages, so
  ``(parent_file, page_no, page_chunk)`` uniquely identifies a chunk
  within its file.
- ``cornerstone_*`` (issue #206): cornerstone-document provenance —
  breadcrumb path, section index, span char offsets — set by
  ``index_cornerstone_source``.
- ``connector_*``: connector-specific provenance (FEC committee id,
  CourtListener docket id, etc.) — used by the connectors themselves
  for filtering / dedup.

Last-writer wins inside a single job: re-writing the same content under
the same job with a different ``metadata`` dict overwrites the sidecar
file. Across jobs each job's sidecar reflects that job's writer call;
the SQLite row is shared.

:func:`read_source_sidecar` is the canonical reader for the sidecar
JSON and the only blessed way to recover ``metadata`` for a source row
that downstream consumers (the dossier rollup, coverage ledger) need.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from research_agent.storage import db
from research_agent.storage.jobs import Job, _atomic_write_json, _atomic_write_text

_HORIZONTAL_WS = re.compile(r"[ \t]+")
_BLANK_LINE_RUN = re.compile(r"\n{3,}")


def clean_content(raw: str) -> str:
    """Normalize ``raw`` for deterministic hashing.

    Strips, normalizes line endings to ``\\n``, collapses runs of horizontal
    whitespace into a single space, and collapses runs of three+ newlines
    into a paragraph break. Richer cleanup belongs upstream in the fetch layer.
    """
    if not isinstance(raw, str):
        raise ValueError(f"raw must be a string; got {type(raw).__name__}")
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _HORIZONTAL_WS.sub(" ", text)
    text = _BLANK_LINE_RUN.sub("\n\n", text)
    return text.strip()


def content_sha256(cleaned: str) -> str:
    """Return the lowercase hex sha256 of the cleaned content."""
    if not isinstance(cleaned, str):
        raise ValueError(f"cleaned must be a string; got {type(cleaned).__name__}")
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def write_source(
    job: Job,
    *,
    url: str | None,
    title: str | None,
    raw_content: str,
    kind: str | None,
    archive_url: str | None = None,
    fetched_at: int | None = None,
    metadata: dict[str, Any] | None = None,
    embedding: bytes | None = None,
    parent_source_id: int | None = None,
) -> int:
    """Write a source under ``job`` with cross-job dedup by sha256.

    First writer for a given hash creates the canonical ``sources/<sha>.md``
    + sidecar under that job and inserts a ``sources`` row. Subsequent
    writers (any job) only insert a ``job_sources`` link — no duplicate
    file is written. Returns the source id (new or existing).

    ``embedding`` is the optional packed float32 BLOB written to
    ``sources.embedding`` for new rows. Reused rows keep whatever
    embedding (if any) the first writer recorded.

    ``parent_source_id`` (issue #206) links a derived source — e.g. a
    ``cornerstone_chunk`` — back to the parent document it was chunked
    from, so retrieval can filter by the cornerstone PDF without
    matching arbitrary other sources whose embedding happens to be near.

    ``metadata`` (issue #353) is an open-ended JSON-serialisable dict
    persisted to the per-job sidecar at ``sources/<sha>.json`` under
    the ``metadata`` key. The dossier mode pipeline (epic #359) reads
    ``metadata.{parent_file, page_no, page_chunk}`` via
    :func:`read_source_sidecar`. See the module docstring for the full
    list of known keys.
    """
    if not isinstance(raw_content, str) or not raw_content:
        raise ValueError("raw_content must be a non-empty string")
    if url is not None and not isinstance(url, str):
        raise ValueError(f"url must be a string or None; got {type(url).__name__}")
    if title is not None and not isinstance(title, str):
        raise ValueError(f"title must be a string or None; got {type(title).__name__}")
    if kind is not None and not isinstance(kind, str):
        raise ValueError(f"kind must be a string or None; got {type(kind).__name__}")
    if archive_url is not None and not isinstance(archive_url, str):
        raise ValueError(f"archive_url must be a string or None; got {type(archive_url).__name__}")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError(f"metadata must be a dict or None; got {type(metadata).__name__}")
    if embedding is not None and not isinstance(embedding, bytes):
        raise ValueError(f"embedding must be bytes or None; got {type(embedding).__name__}")
    if parent_source_id is not None and not isinstance(parent_source_id, int):
        raise ValueError(
            f"parent_source_id must be an int or None; got {type(parent_source_id).__name__}"
        )

    cleaned = clean_content(raw_content)
    if not cleaned:
        raise ValueError("raw_content cleans to an empty string")
    sha = content_sha256(cleaned)
    fetched = int(fetched_at) if fetched_at is not None else int(time.time())
    md_rel = f"sources/{sha}.md"
    json_rel = f"sources/{sha}.json"

    conn = db.connect(job.db_path)
    try:
        with conn:
            existing = conn.execute(
                "SELECT id, md_path FROM sources WHERE sha256 = ?",
                (sha,),
            ).fetchone()

            sidecar: dict[str, Any] = {
                "sha256": sha,
                "url": url,
                "title": title,
                "fetched_at": fetched,
                "archive_url": archive_url or "",
                "kind": kind,
                "md_path": md_rel,
                "metadata": metadata or {},
            }

            if existing is None:
                _atomic_write_text(job.root / md_rel, cleaned + "\n")
                _atomic_write_json(job.root / json_rel, sidecar)

                cur = conn.execute(
                    """
                    INSERT INTO sources (
                        sha256, url, title, fetched_at, archive_url, md_path,
                        kind, embedding, parent_source_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sha,
                        url,
                        title,
                        fetched,
                        archive_url,
                        md_rel,
                        kind,
                        embedding,
                        parent_source_id,
                    ),
                )
                assert cur.lastrowid is not None
                source_id = int(cur.lastrowid)
            else:
                source_id = int(existing["id"])
                # Always materialize the markdown + sidecar into THIS job's
                # folder, even on dedup hit. The DB's md_path column is a
                # repo-relative path under each job, so a job whose folder
                # was deleted (or never had this source written into it)
                # would otherwise have a sources row but no on-disk content
                # for ``_load_source_text`` to read.
                target = job.root / md_rel
                if not target.exists():
                    _atomic_write_text(target, cleaned + "\n")
                _atomic_write_json(job.root / json_rel, sidecar)
                # If the row was pruned (md_path NULLed by the disk-cap
                # watcher), repoint to the freshly-written file.
                if not existing["md_path"]:
                    conn.execute(
                        "UPDATE sources SET md_path = ?, fetched_at = ? WHERE id = ?",
                        (md_rel, fetched, source_id),
                    )

            conn.execute(
                "INSERT OR IGNORE INTO job_sources (job_id, source_id) VALUES (?, ?)",
                (job.id, source_id),
            )
    finally:
        conn.close()

    return source_id


def read_source_sidecar(job: Job, sha: str) -> dict[str, Any]:
    """Return the per-job JSON sidecar for ``sha`` parsed into a dict.

    The sidecar lives at ``<job.root>/sources/<sha>.json`` and carries
    everything :func:`write_source` recorded — ``sha256``, ``url``,
    ``title``, ``fetched_at``, ``archive_url``, ``kind``, ``md_path``,
    and ``metadata``. The ``metadata`` value is always returned as a
    ``dict`` (or an empty dict when none was written); callers must
    never see the raw JSON string.

    Raises :class:`FileNotFoundError` when the sidecar is missing, e.g.
    a freshly-pruned job that never re-materialised the file. Callers
    that expect a possibly-missing sidecar should catch and treat the
    metadata as empty.
    """
    if not isinstance(sha, str) or not sha:
        raise ValueError("sha must be a non-empty string")
    path = job.root / "sources" / f"{sha}.json"
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(
            f"sidecar at {path} is not a JSON object; got {type(data).__name__}"
        )
    metadata = data.get("metadata")
    if metadata is None:
        data["metadata"] = {}
    elif not isinstance(metadata, dict):
        raise ValueError(
            f"sidecar at {path} has non-dict metadata; got {type(metadata).__name__}"
        )
    return data


def read_source_metadata(job: Job, sha: str) -> dict[str, Any]:
    """Convenience: return just the ``metadata`` dict from the sidecar.

    Equivalent to ``read_source_sidecar(job, sha)["metadata"]`` but
    keeps call sites that only care about provenance free of the wider
    sidecar shape. Returns an empty dict when the sidecar carries no
    metadata (rather than the legacy default-dict pattern, so callers
    can use ``meta.get("page_no")`` without checking for ``None``).
    """
    sidecar = read_source_sidecar(job, sha)
    metadata = sidecar.get("metadata") or {}
    return dict(metadata)


__all__ = [
    "clean_content",
    "content_sha256",
    "read_source_metadata",
    "read_source_sidecar",
    "write_source",
]
