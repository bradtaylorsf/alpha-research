"""Tests for ``research_agent.llm.cache``."""

from __future__ import annotations

from pathlib import Path

import pytest

from research_agent.llm.cache import (
    DEFAULT_TTL_SECONDS,
    LLMCache,
    make_key,
)

# ---------------------------------------------------------------------------
# make_key
# ---------------------------------------------------------------------------


def test_make_key_stable_across_dict_ordering() -> None:
    a = make_key(
        "openrouter",
        "anthropic/claude-opus-4-7",
        "summarize this",
        params={"top_p": 0.9, "temperature": 0.2},
        tool_defs=[{"name": "search", "schema": {"type": "object"}}],
    )
    b = make_key(
        "openrouter",
        "anthropic/claude-opus-4-7",
        "summarize this",
        params={"temperature": 0.2, "top_p": 0.9},
        tool_defs=[{"name": "search", "schema": {"type": "object"}}],
    )
    assert a == b


def test_make_key_distinct_when_any_axis_differs() -> None:
    base = make_key("openrouter", "claude-opus", "extract", params={"temperature": 0.0})
    # Provider
    assert base != make_key("lmstudio", "claude-opus", "extract", params={"temperature": 0.0})
    # Model
    assert base != make_key("openrouter", "claude-haiku", "extract", params={"temperature": 0.0})
    # Prompt
    assert base != make_key("openrouter", "claude-opus", "extract!", params={"temperature": 0.0})
    # Temperature
    assert base != make_key("openrouter", "claude-opus", "extract", params={"temperature": 0.7})
    # Tool defs
    assert base != make_key(
        "openrouter",
        "claude-opus",
        "extract",
        params={"temperature": 0.0},
        tool_defs=[{"name": "fetch", "schema": {}}],
    )


def test_make_key_ignores_unknown_params() -> None:
    """Only temperature/top_p/top_k participate in the key."""
    a = make_key("p", "m", "x")
    b = make_key("p", "m", "x", params={"max_tokens": 100, "seed": 42})
    assert a == b


# ---------------------------------------------------------------------------
# LLMCache: round trip + collision behaviour
# ---------------------------------------------------------------------------


def test_round_trip_returns_same_value(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path / "c.sqlite")
    key = make_key("openrouter", "x", "hello world")
    cache.put(key, "<<the answer>>", model_meta={"tier": "frontier"})
    assert cache.get(key) == "<<the answer>>"
    cache.close()


def test_distinct_keys_do_not_collide(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path / "c.sqlite")
    k1 = make_key("p", "m", "prompt-1")
    k2 = make_key("p", "m", "prompt-2")
    cache.put(k1, "first")
    cache.put(k2, "second")
    assert cache.get(k1) == "first"
    assert cache.get(k2) == "second"
    cache.close()


def test_get_missing_key_returns_none(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path / "c.sqlite")
    assert cache.get("not-there") is None
    cache.close()


def test_put_replaces_existing_entry(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path / "c.sqlite")
    key = make_key("p", "m", "prompt")
    cache.put(key, "v1")
    cache.put(key, "v2")
    assert cache.get(key) == "v2"
    cache.close()


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


def test_expired_entry_returns_none_and_is_removed(tmp_path: Path) -> None:
    clock = {"t": 1_000_000.0}
    cache = LLMCache(tmp_path / "c.sqlite", ttl_seconds=10, now=lambda: clock["t"])
    key = make_key("p", "m", "old prompt")
    cache.put(key, "expiring soon")
    # Within TTL → still present.
    clock["t"] += 5
    assert cache.get(key) == "expiring soon"
    # Past TTL → gone.
    clock["t"] += 100
    assert cache.get(key) is None
    # Inline delete: row should not be in the table any more.
    rows = cache._conn.execute(  # noqa: SLF001 — direct read for assertion
        "SELECT COUNT(*) FROM llm_cache WHERE key = ?", (key,)
    ).fetchone()
    assert rows[0] == 0
    cache.close()


def test_per_call_ttl_override_takes_precedence(tmp_path: Path) -> None:
    clock = {"t": 0.0}
    cache = LLMCache(tmp_path / "c.sqlite", ttl_seconds=1000, now=lambda: clock["t"])
    short_key = make_key("p", "m", "short")
    long_key = make_key("p", "m", "long")
    cache.put(short_key, "v", ttl_seconds=5)
    cache.put(long_key, "v")
    clock["t"] = 50
    assert cache.get(short_key) is None  # overridden TTL elapsed
    assert cache.get(long_key) == "v"  # default TTL still alive
    cache.close()


def test_default_ttl_is_30_days() -> None:
    assert DEFAULT_TTL_SECONDS == 30 * 24 * 3600


# ---------------------------------------------------------------------------
# clear / wipe_file
# ---------------------------------------------------------------------------


def test_clear_removes_all_rows(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path / "c.sqlite")
    cache.put(make_key("p", "m", "a"), "1")
    cache.put(make_key("p", "m", "b"), "2")
    cache.clear()
    assert cache.get(make_key("p", "m", "a")) is None
    assert cache.get(make_key("p", "m", "b")) is None
    cache.close()


def test_wipe_file_removes_db_and_sidecars(tmp_path: Path) -> None:
    path = tmp_path / "c.sqlite"
    cache = LLMCache(path)
    cache.put(make_key("p", "m", "x"), "v")
    cache.close()

    assert path.exists()
    LLMCache.wipe_file(path)
    assert not path.exists()
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()


def test_wipe_file_is_safe_when_file_missing(tmp_path: Path) -> None:
    LLMCache.wipe_file(tmp_path / "no-such.sqlite")  # must not raise


# ---------------------------------------------------------------------------
# Independence from the main index
# ---------------------------------------------------------------------------


def test_cache_lives_in_its_own_sqlite_file(tmp_path: Path) -> None:
    """The cache must NOT touch the main index DB path."""
    main_db = tmp_path / "data" / "index.sqlite"
    cache_db = tmp_path / "data" / "llm_cache.sqlite"
    cache = LLMCache(cache_db)
    cache.put(make_key("p", "m", "x"), "v")
    cache.close()
    assert cache_db.exists()
    assert not main_db.exists()


def test_model_meta_dict_is_serialized(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path / "c.sqlite")
    key = make_key("p", "m", "x")
    cache.put(key, "v", model_meta={"tier": "frontier", "provider": "openrouter"})
    row = cache._conn.execute(  # noqa: SLF001
        "SELECT model_meta FROM llm_cache WHERE key = ?", (key,)
    ).fetchone()
    assert row is not None
    # Stored as compact canonical JSON.
    assert "tier" in row[0]
    assert "frontier" in row[0]
    cache.close()


@pytest.fixture
def cache_factory(tmp_path: Path):
    caches: list[LLMCache] = []

    def _make(**kwargs):
        c = LLMCache(tmp_path / f"c{len(caches)}.sqlite", **kwargs)
        caches.append(c)
        return c

    yield _make
    for c in caches:
        c.close()


def test_factory_smoke(cache_factory) -> None:
    """Sanity check: the test fixture itself works."""
    cache = cache_factory()
    cache.put("k", "v")
    assert cache.get("k") == "v"
