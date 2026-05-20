"""Source ingestion with content-hash dedup across jobs.

Implements the §4 layout: per-job ``sources/<sha256>.md`` (cleaned content)
plus ``<sha256>.json`` sidecar (url, fetched_at, archive_url, kind), and the
§10 schema's ``sources`` + ``job_sources`` tables. The same content fetched
by two different jobs collapses to a single ``sources`` row with two
``job_sources`` links — the canonical markdown lives under the first job
that wrote it.

Cleaning is intentionally minimal in v1 (line-ending normalization,
horizontal whitespace collapse, strip). Richer extraction — readability,
boilerplate stripping — belongs in the fetch tool layer (``tools/web_fetch``)
upstream of this module.
"""

from __future__ import annotations

import hashlib
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


def content_sha256_for_write(cleaned: str, metadata: dict[str, Any] | None) -> str:
    """Dedup key for :func:`write_source`.

    Per-page dossier rows include ``metadata.page_no`` in the hash so
    identical boilerplate on different pages (e.g. repeated ``1.4(a)``)
    still get distinct ``sources`` rows and coverage units.
    """
    if isinstance(metadata, dict) and metadata.get("page_no") is not None:
        parent = str(metadata.get("parent_file") or "")
        page = metadata.get("page_no")
        chunk = metadata.get("page_chunk")
        key = f"{parent}\0{page}\0{chunk}\0{cleaned}"
        return content_sha256(key)
    return content_sha256(cleaned)


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
    sha = content_sha256_for_write(cleaned, metadata)
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


__all__ = [
    "clean_content",
    "content_sha256",
    "write_source",
]
