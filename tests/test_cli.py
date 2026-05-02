"""End-to-end tests for the `research` CLI surface."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from research_agent import __version__, cli, config


@pytest.fixture(autouse=True)
def _reset_env_loader(monkeypatch):
    """Force env discovery to start clean for each invocation."""
    for key in (
        "OPENROUTER_API_KEY",
        "RESEARCH_USER_AGENT",
        "RESEARCH_HEADFUL",
        "LMSTUDIO_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    config.reset_for_tests()
    yield
    config.reset_for_tests()


@pytest.fixture
def isolated_repo(tmp_path, monkeypatch):
    """Run the CLI from a tmp dir that contains a minimal valid layout."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("tiers: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # Ensure run_all_checks treats this tmp dir as the repo root.
    monkeypatch.setattr(cli, "_LOADED_ENV_FILES", [])
    return tmp_path


def test_version_flag_prints_version_and_exits_zero():
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_version_short_flag_works():
    runner = CliRunner()
    result = runner.invoke(cli.app, ["-V"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_no_args_shows_help():
    runner = CliRunner()
    result = runner.invoke(cli.app, [])
    # Typer's no_args_is_help convention exits 2 with help on stderr/stdout.
    assert "Usage" in result.stdout or "Usage" in (result.stderr or "")


def test_doctor_json_returns_valid_json_with_required_keys(isolated_repo, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-abcdef0123456789")
    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert {"checks", "loaded_env_files", "ok"} <= payload.keys()
    assert payload["ok"] is True


def test_doctor_json_exit_one_on_required_failure(isolated_repo):
    # No OPENROUTER_API_KEY in env → required env-key check fails.
    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    by_name = {c["name"]: c for c in payload["checks"]}
    assert by_name["env:OPENROUTER_API_KEY"]["status"] == "fail"


def test_doctor_table_does_not_leak_secret(isolated_repo, monkeypatch):
    secret = "sk-or-abcdef0123456789"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0, result.stdout
    assert secret not in result.stdout
    assert "abcdef" not in result.stdout
    # Masked suffix should appear.
    assert "...6789" in result.stdout


def test_doctor_json_does_not_leak_secret(isolated_repo, monkeypatch):
    secret = "sk-or-abcdef0123456789"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout
    assert secret not in result.stdout
    assert "abcdef" not in result.stdout


def test_doctor_optional_missing_does_not_fail(isolated_repo, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-abcdef0123456789")
    # All optional keys remain unset (per autouse fixture).
    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    by_name = {c["name"]: c for c in payload["checks"]}
    assert by_name["env:RESEARCH_HEADFUL"]["status"] == "skip"
    assert by_name["env:RESEARCH_HEADFUL"]["required"] is False


def test_doctor_help_lists_command():
    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor", "--help"])
    assert result.exit_code == 0
    assert "--json" in result.stdout
