"""LLM response cache (implementation guide §11).

Backed by a *separate* SQLite file at ``data/llm_cache.sqlite`` so wiping the
cache cannot accidentally clobber the cross-job index (``data/index.sqlite``).
The router opts in per call (``cache=True``) — synthesis passes that should
explore opt out, deterministic extractions opt in. Default TTL is 30 days.

Cache key is a sha256 of a canonical JSON dict over ``(provider, model,
prompt, params, tool_defs)`` so the same logical call hits regardless of
dict ordering. Sampling params we hash today: ``temperature``, ``top_p``,
``top_k`` — anything outside that subset is intentionally ignored.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_CACHE_PATH = Path("data/llm_cache.sqlite")
DEFAULT_TTL_SECONDS = 30 * 24 * 3600

# Sampling params we hash into the cache key. Anything not in this allow-list
# is *not* part of the key — keep it stable; expanding it invalidates entries.
_PARAM_KEYS: tuple[str, ...] = ("temperature", "top_p", "top_k")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    model_meta TEXT,
    created_at INTEGER NOT NULL,
    ttl_seconds INTEGER NOT NULL
);
"""


def _canonical_json(obj: Any) -> str:
    """JSON dump with sorted keys + tight separators — stable across runs."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _normalize_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Project ``params`` onto the hashed sampling subset."""
    if not params:
        return {}
    return {k: params[k] for k in _PARAM_KEYS if k in params and params[k] is not None}


def _hash_tool_defs(tool_defs: Any) -> str | None:
    """Return a sha256 hex of the canonical-JSON tool defs, or ``None``."""
    if tool_defs is None:
        return None
    if isinstance(tool_defs, (list, tuple)) and not tool_defs:
        return None
    blob = _canonical_json(tool_defs).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def make_key(
    provider: str,
    model: str,
    prompt: str,
    params: dict[str, Any] | None = None,
    tool_defs: Any = None,
) -> str:
    """Build the canonical sha256 key for a cache lookup."""
    canonical = {
        "provider": provider,
        "model": model,
        "prompt": prompt,
        "params": _normalize_params(params),
        "tools_hash": _hash_tool_defs(tool_defs),
    }
    blob = _canonical_json(canonical).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class LLMCache:
    """SQLite-backed response cache, independent of the main index DB."""

    def __init__(
        self,
        path: Path | str = DEFAULT_CACHE_PATH,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.ttl_seconds = int(ttl_seconds)
        self._now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        # WAL + foreign_keys=OFF: this DB is wipeable on its own and never
        # references rows in the main index, so we don't need FK enforcement.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=OFF")
        with self._conn:
            self._conn.executescript(SCHEMA_SQL)

    def get(self, key: str) -> str | None:
        """Return the cached value for ``key`` or ``None`` if missing/expired.

        Expired rows are deleted inline so the table doesn't accumulate
        tombstones over time.
        """
        row = self._conn.execute(
            "SELECT value, created_at, ttl_seconds FROM llm_cache WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if int(self._now()) - int(row["created_at"]) > int(row["ttl_seconds"]):
            with self._conn:
                self._conn.execute("DELETE FROM llm_cache WHERE key = ?", (key,))
            return None
        return str(row["value"])

    def put(
        self,
        key: str,
        value: str,
        model_meta: dict[str, Any] | str | None = None,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        """Insert (or replace) a cache entry."""
        if isinstance(model_meta, dict):
            meta_blob: str | None = _canonical_json(model_meta)
        else:
            meta_blob = model_meta
        effective_ttl = int(ttl_seconds) if ttl_seconds is not None else self.ttl_seconds
        ts = int(self._now())
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO llm_cache"
                " (key, value, model_meta, created_at, ttl_seconds)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, value, meta_blob, ts, effective_ttl),
            )

    def clear(self) -> None:
        """Delete every row in the cache. File stays in place."""
        with self._conn:
            self._conn.execute("DELETE FROM llm_cache")

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def wipe_file(path: Path | str = DEFAULT_CACHE_PATH) -> None:
        """Delete the cache sqlite file plus any -wal/-shm sidecars."""
        p = Path(path)
        for suffix in ("", "-wal", "-shm"):
            target = p.with_name(p.name + suffix) if suffix else p
            try:
                target.unlink()
            except FileNotFoundError:
                continue


__all__ = [
    "DEFAULT_CACHE_PATH",
    "DEFAULT_TTL_SECONDS",
    "LLMCache",
    "make_key",
]
