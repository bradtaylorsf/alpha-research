"""Tests for `research_agent.doctor` checks and rendering."""

from __future__ import annotations

import json

import pytest

from research_agent import config, doctor


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    """Each test starts with a clean slate for keys we toggle."""
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


def test_mask_secret_long_value():
    assert doctor.mask_secret("sk-or-abcdef0123456789") == "...6789"


def test_mask_secret_short_value():
    assert doctor.mask_secret("short") == "***"
    assert doctor.mask_secret("12345678") == "***"
    assert doctor.mask_secret("") == "***"


def test_check_env_keys_marks_required_missing_as_fail():
    results = doctor.check_env_keys()
    by_name = {r.name: r for r in results}
    required = by_name["env:OPENROUTER_API_KEY"]
    assert required.status == "fail"
    assert required.required is True
    assert "missing (required)" in required.detail


def test_check_env_keys_marks_optional_missing_as_skip():
    results = doctor.check_env_keys()
    by_name = {r.name: r for r in results}
    optional = by_name["env:RESEARCH_HEADFUL"]
    assert optional.status == "skip"
    assert optional.required is False
    assert "missing (optional)" in optional.detail


def test_check_env_keys_masks_present_value(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-abcdef0123456789")
    results = doctor.check_env_keys()
    by_name = {r.name: r for r in results}
    present = by_name["env:OPENROUTER_API_KEY"]
    assert present.status == "ok"
    assert "...6789" in present.detail
    assert "abcdef" not in present.detail


def test_check_openrouter_key_shape_pass(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-abcdef0123456789")
    result = doctor.check_openrouter_key_shape()
    assert result.status == "ok"
    assert "abcdef" not in result.detail


def test_check_openrouter_key_shape_fail_on_bad_prefix(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "totally-wrong-prefix-12345")
    result = doctor.check_openrouter_key_shape()
    assert result.status == "fail"
    assert result.required is True


def test_check_openrouter_key_shape_skip_when_missing():
    result = doctor.check_openrouter_key_shape()
    assert result.status == "skip"
    assert result.required is False


def test_check_writable_dirs_creates_and_cleans(tmp_path):
    result = doctor.check_writable_dirs(tmp_path)
    assert result.status == "ok"
    assert (tmp_path / "data").is_dir()
    assert (tmp_path / "jobs").is_dir()
    # The probe file must be cleaned up.
    assert not (tmp_path / "data" / ".doctor-probe").exists()
    assert not (tmp_path / "jobs" / ".doctor-probe").exists()


def test_check_sqlite_wal_passes_in_tempdir():
    result = doctor.check_sqlite_wal()
    assert result.status == "ok"
    assert "wal" in result.detail.lower()


def test_check_models_yaml_passes_for_valid_yaml(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text("tiers:\n  cloud:\n    provider: openrouter\n", encoding="utf-8")
    result = doctor.check_models_yaml(path)
    assert result.status == "ok"


def test_check_models_yaml_fails_for_invalid_yaml(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text("tiers: [unterminated\n", encoding="utf-8")
    result = doctor.check_models_yaml(path)
    assert result.status == "fail"
    assert result.required is True


def test_check_models_yaml_fails_when_missing(tmp_path):
    result = doctor.check_models_yaml(tmp_path / "absent.yaml")
    assert result.status == "fail"


def test_check_lm_studio_skip_when_unreachable(monkeypatch):
    # Point at a port that should refuse the connection.
    result = doctor.check_lm_studio("http://127.0.0.1:1/v1")
    assert result.status == "skip"
    assert result.required is False


def test_check_lm_studio_skip_when_unset():
    result = doctor.check_lm_studio(None)
    assert result.status == "skip"


def test_check_python_reports_current_runtime():
    result = doctor.check_python()
    # We're running on >= 3.12 in CI per pyproject; sanity check the shape.
    assert result.status == "ok"
    assert result.required is True


def test_check_env_files_with_loaded_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("X=1\n", encoding="utf-8")
    result = doctor.check_env_files([env_file])
    assert result.status == "ok"
    assert ".env" in result.detail


def test_check_env_files_reports_not_found():
    result = doctor.check_env_files([])
    assert result.status == "skip"
    assert "not found" in result.detail


def test_to_json_contains_no_raw_secret_value(monkeypatch, tmp_path):
    secret = "sk-or-abcdef0123456789"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("tiers: {}\n", encoding="utf-8")

    results = doctor.run_all_checks([], repo_root=tmp_path)
    payload = doctor.to_json(results, [])
    serialised = json.dumps(payload)
    assert secret not in serialised
    assert "abcdef" not in serialised
    # Last four chars are allowed (masked form).
    assert "6789" in serialised


def test_run_all_checks_returns_required_failure_when_keys_missing(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("tiers: {}\n", encoding="utf-8")
    results = doctor.run_all_checks([], repo_root=tmp_path)
    assert doctor.has_required_failure(results)


def test_run_all_checks_passes_when_required_satisfied(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-abcdef0123456789")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("tiers: {}\n", encoding="utf-8")
    results = doctor.run_all_checks([], repo_root=tmp_path)
    assert not doctor.has_required_failure(results)


def test_emit_json_returns_valid_json(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("tiers: {}\n", encoding="utf-8")
    results = doctor.run_all_checks([], repo_root=tmp_path)
    payload = json.loads(doctor.emit_json(results, []))
    assert "checks" in payload
    assert "loaded_env_files" in payload
    assert "ok" in payload


def test_render_table_runs_without_error(capsys, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("tiers: {}\n", encoding="utf-8")
    results = doctor.run_all_checks([], repo_root=tmp_path)
    doctor.render_table(results)
    captured = capsys.readouterr()
    assert "research doctor" in captured.out


def test_render_table_does_not_print_raw_secret(capsys, monkeypatch, tmp_path):
    secret = "sk-or-abcdef0123456789"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("tiers: {}\n", encoding="utf-8")
    results = doctor.run_all_checks([], repo_root=tmp_path)
    doctor.render_table(results)
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert "abcdef" not in captured.out


def test_check_tesseract_ok_when_binary_returns_version(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/opt/homebrew/bin/tesseract")

    class _Completed:
        returncode = 0
        stdout = b"tesseract 5.3.4\n leptonica-1.84.1\n"
        stderr = b""

    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **kw: _Completed())
    result = doctor.check_tesseract()
    assert result.status == "ok"
    assert result.required is False
    assert "tesseract 5.3.4" in result.detail


def test_check_tesseract_skip_when_binary_missing(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    result = doctor.check_tesseract()
    assert result.status == "skip"
    assert result.required is False
    assert "brew install tesseract" in result.detail


def test_check_tesseract_skip_when_subprocess_raises(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/opt/homebrew/bin/tesseract")

    def _raise(*_a, **_kw):
        raise FileNotFoundError("vanished between which and run")

    monkeypatch.setattr(doctor.subprocess, "run", _raise)
    result = doctor.check_tesseract()
    assert result.status == "skip"
    assert result.required is False
    assert "brew install tesseract" in result.detail


def test_run_all_checks_includes_tesseract(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("tiers: {}\n", encoding="utf-8")
    results = doctor.run_all_checks([], repo_root=tmp_path)
    assert any(r.name == "tesseract" for r in results)


def test_check_trove_api_note_skips_when_key_missing(monkeypatch):
    monkeypatch.delenv("TROVE_API_KEY", raising=False)

    result = doctor.check_trove_api_note()

    assert result.name == "trove_api_note"
    assert result.status == "skip"
    assert result.required is False
    assert "metadata-only" in result.detail
    assert "12 months" in result.detail


def test_check_trove_api_note_masks_present_key(monkeypatch):
    monkeypatch.setenv("TROVE_API_KEY", "trove-test-key-abcdef")

    result = doctor.check_trove_api_note()

    assert result.status == "ok"
    assert "...cdef" in result.detail
    assert "trove-test-key" not in result.detail


def test_run_all_checks_includes_trove_note(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("tiers: {}\n", encoding="utf-8")
    results = doctor.run_all_checks([], repo_root=tmp_path)
    assert any(r.name == "trove_api_note" for r in results)


def test_check_result_dataclass_shape():
    from dataclasses import FrozenInstanceError

    result = doctor.CheckResult(name="x", status="ok", required=True, detail="d")
    # Frozen dataclass — must reject mutation.
    with pytest.raises(FrozenInstanceError):
        result.status = "fail"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# sanctions_refresh check (issue #154)
# ---------------------------------------------------------------------------


def test_check_sanctions_refresh_surfaces_per_list(monkeypatch):
    """Recent SDN -> ok, stale UK -> fail, missing/disabled EU -> skip."""
    import time as _time

    from research_agent.tools import sanctions

    now = _time.time()
    fake_meta = {
        "SDN": now - 60,                         # fresh
        "UK": now - (10 * 24 * 60 * 60),         # 10 days stale
        # EU missing (disabled)
    }

    def _stub_get_last_refresh() -> dict[str, float | None]:
        return {kind: fake_meta.get(kind) for kind in sanctions.LIST_KINDS}

    monkeypatch.setattr(sanctions, "get_last_refresh", _stub_get_last_refresh)

    results = doctor.check_sanctions_refresh()
    by_name = {r.name: r for r in results}
    assert set(by_name) == {"sanctions:SDN", "sanctions:EU", "sanctions:UK"}

    sdn = by_name["sanctions:SDN"]
    assert sdn.status == "ok"
    assert sdn.required is False

    eu = by_name["sanctions:EU"]
    assert eu.status == "skip"
    assert eu.required is False
    assert "disabled" in eu.detail

    uk = by_name["sanctions:UK"]
    assert uk.status == "fail"
    assert uk.required is False
    assert "stale" in uk.detail


def test_check_sanctions_refresh_never_refreshed(monkeypatch):
    from research_agent.tools import sanctions

    monkeypatch.setattr(
        sanctions,
        "get_last_refresh",
        lambda: {kind: None for kind in sanctions.LIST_KINDS},
    )
    results = doctor.check_sanctions_refresh()
    by_name = {r.name: r for r in results}
    # Disabled list (EU) is skip; the other two are fail with "never refreshed".
    assert by_name["sanctions:EU"].status == "skip"
    assert by_name["sanctions:SDN"].status == "fail"
    assert "never refreshed" in by_name["sanctions:SDN"].detail
    assert by_name["sanctions:UK"].status == "fail"
    # None of these should affect the doctor exit code.
    assert all(r.required is False for r in results)


def test_run_all_checks_includes_sanctions_refresh(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("tiers: {}\n", encoding="utf-8")
    results = doctor.run_all_checks([], repo_root=tmp_path)
    names = {r.name for r in results}
    assert {"sanctions:SDN", "sanctions:EU", "sanctions:UK"}.issubset(names)


# ---------------------------------------------------------------------------
# Issue #223 — registry coherence checks.
# ---------------------------------------------------------------------------


def test_check_planner_allowlist_coherence_passes_for_live_registry() -> None:
    """The shipping registry + planner prompt must round-trip cleanly."""
    result = doctor.check_planner_allowlist_coherence()
    assert result.status == "ok", result.detail
    assert result.required is True


def test_check_planner_allowlist_coherence_flags_orphan(monkeypatch) -> None:
    """A kind in the allowlist that isn't registered is a hard fail.

    The rendered allowlist is the source of truth for the model — if it
    contains a kind the registry doesn't know about, the planner can emit
    a task the orchestrator can't dispatch. We patch the renderer to
    inject a synthetic orphan and confirm the check raises ``fail``.
    """
    from research_agent.prompts import loader as prompts_loader

    real = prompts_loader._render_registry_vars

    def _orphan_render() -> dict[str, str]:
        out = real()
        out["kinds_allowlist"] = out["kinds_allowlist"] + ", `phantom_search`"
        return out

    monkeypatch.setattr(prompts_loader, "_render_registry_vars", _orphan_render)
    result = doctor.check_planner_allowlist_coherence()

    assert result.status == "fail"
    assert "in allowlist but not registered" in result.detail
    assert "phantom_search" in result.detail


def test_check_planner_allowlist_coherence_flags_missing_table_row(monkeypatch) -> None:
    """A registered kind missing from the Direct kinds table fails."""
    from research_agent.prompts import loader as prompts_loader

    real = prompts_loader._render_registry_vars

    def _truncated() -> dict[str, str]:
        out = real()
        out["direct_kinds_table"] = (
            "| Kind | What it covers | Optional payload knobs | Example query |\n"
            "|---|---|---|---|"
        )  # header only, zero rows
        return out

    monkeypatch.setattr(prompts_loader, "_render_registry_vars", _truncated)
    result = doctor.check_planner_allowlist_coherence()

    assert result.status == "fail"
    assert "no Direct-kinds-table row" in result.detail


def test_check_registry_skill_coherence_skips_grandfathered() -> None:
    """Kinds with ``skill_name=None`` produce ``skip`` rows, not ``fail``."""
    rows = doctor.check_registry_skill_coherence()
    by_name = {r.name: r for r in rows}
    # ``bbb_search`` is grandfathered (skill_name=None in the live registry).
    skipped = by_name["registry_skill:bbb_search"]
    assert skipped.status == "skip"
    assert "grandfathered" in skipped.detail
    # ``congress_search`` ships a skill — must be ok.
    ok_row = by_name["registry_skill:congress_search"]
    assert ok_row.status == "ok"
    commons_row = by_name["registry_skill:commons_search"]
    assert commons_row.status == "ok"
    wikidata_row = by_name["registry_skill:wikidata_search"]
    assert wikidata_row.status == "ok"


def test_check_registry_skill_coherence_fails_when_skill_file_missing(
    monkeypatch, tmp_path
) -> None:
    """A registered kind with skill_name pointing at a missing file fails."""
    from research_agent.skills import loader as skills_loader
    from research_agent.tools import _registry

    fake_dir = tmp_path / "skills" / "connectors"
    fake_dir.mkdir(parents=True)

    monkeypatch.setattr(
        skills_loader, "_skills_dir", lambda category: tmp_path / "skills" / category
    )

    fake_entry = _registry.KindEntry(
        name="ghost_search",
        payload_schema=_registry.BaseSearchPayload,
        search_fn=lambda *a, **kw: None,
        fetch_fn=None,
        host_patterns=(),
        skill_name="ghost",
        description="",
        optional_payload_knobs="",
        example_query="",
        module_name="ghost",
    )
    monkeypatch.setattr(_registry, "iter_kinds", lambda: [fake_entry])

    rows = doctor.check_registry_skill_coherence()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "fail"
    assert row.required is True
    assert "missing skills/connectors/ghost.md" in row.detail


def test_run_all_checks_includes_registry_coherence(tmp_path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("tiers: {}\n", encoding="utf-8")
    results = doctor.run_all_checks([], repo_root=tmp_path)
    names = {r.name for r in results}
    assert "planner_allowlist_coherence" in names
    assert any(n.startswith("registry_skill:") for n in names)
