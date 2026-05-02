"""Tests for `research_agent.config.load_env` precedence and discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from research_agent import config


@pytest.fixture(autouse=True)
def _reset_loader():
    config.reset_for_tests()
    yield
    config.reset_for_tests()


def _write_env(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")


def test_process_env_wins_over_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_env(tmp_path / ".env", "OPENROUTER_API_KEY=from-dotenv\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-process")

    config.load_env()

    assert config.get("OPENROUTER_API_KEY") == "from-process"


def test_env_local_overrides_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _write_env(tmp_path / ".env", "OPENROUTER_API_KEY=from-env\n")
    _write_env(tmp_path / ".env.local", "OPENROUTER_API_KEY=from-local\n")

    config.load_env()

    assert config.get("OPENROUTER_API_KEY") == "from-local"


def test_walks_up_to_find_env(tmp_path, monkeypatch):
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    _write_env(tmp_path / ".env", "LMSTUDIO_BASE_URL=http://walked-up:1234/v1\n")

    loaded = config.load_env()

    assert any(p.name == ".env" for p in loaded)
    assert config.get("LMSTUDIO_BASE_URL") == "http://walked-up:1234/v1"


def test_missing_optional_keys_do_not_raise(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("OPENROUTER_API_KEY", "RESEARCH_HEADFUL", "RESEARCH_USER_AGENT"):
        monkeypatch.delenv(key, raising=False)

    config.load_env()

    assert config.get("RESEARCH_HEADFUL") is None
    # Optional key with declared default still resolves via `get`.
    assert config.get("RESEARCH_USER_AGENT") == "research-agent/0.1 (+local; contact unset)"


def test_load_env_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    _write_env(env_file, "OPENROUTER_API_KEY=first\n")

    first = config.load_env()
    assert first == [env_file]

    # Rewrite the file; second call should be a no-op (no reload).
    _write_env(env_file, "OPENROUTER_API_KEY=second\n")
    second = config.load_env()
    assert second == []
    assert config.get("OPENROUTER_API_KEY") == "first"
