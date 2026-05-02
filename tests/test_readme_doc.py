"""README contract tests.

Issue #44 promises operators a single document that walks them from a
fresh laptop to an unattended soak. These tests pin the contract:

- The required sections exist (install, LM Studio, OpenRouter, doctor,
  walk-through, CLI, costs, directory layout, macOS hygiene,
  troubleshooting).
- The three foundational research docs are linked (and the linked files
  exist on disk).
- Each tier in ``config/models.yaml`` is named in the README so a fresh
  operator knows which models to download / which key drives which tier.
- Every CLI verb registered with the Typer app is mentioned (so adding
  a verb without README coverage trips the suite).

The goal is to fail loudly when docs drift, not to pin exact prose.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from research_agent import cli

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"


@pytest.fixture(scope="module")
def readme_text() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def readme_lower(readme_text: str) -> str:
    return readme_text.lower()


def test_readme_exists() -> None:
    assert README.is_file(), "README.md is the operator entry point"


@pytest.mark.parametrize(
    "heading",
    [
        "## Install",
        "## LM Studio",
        "## OpenRouter",
        "## `research doctor`",
        "## Walk-through",
        "## CLI surface",
        "## Costs",
        "## Directory layout",
        "## macOS hygiene",
        "## Troubleshooting",
    ],
)
def test_required_section_present(readme_text: str, heading: str) -> None:
    assert heading in readme_text, f"missing required README section: {heading!r}"


@pytest.mark.parametrize(
    "doc",
    [
        "ai-agent-research-setup.md",
        "ai-agent-investigation-playbook.md",
        "research-agent-implementation-guide.md",
    ],
)
def test_foundational_doc_linked_and_exists(readme_text: str, doc: str) -> None:
    assert f"({doc})" in readme_text, f"README does not link the foundational doc {doc!r}"
    assert (REPO_ROOT / doc).is_file(), f"linked doc {doc!r} is not on disk"


def test_all_model_tiers_named_in_readme(readme_text: str) -> None:
    """Every tier in config/models.yaml must appear by name in the README."""
    models = yaml.safe_load((REPO_ROOT / "config" / "models.yaml").read_text())
    tiers = list(models["tiers"].keys())
    assert tiers, "config/models.yaml has no tiers; sanity check failed"
    for tier in tiers:
        assert tier in readme_text, f"tier {tier!r} not mentioned in README"


def test_lmstudio_model_ids_listed(readme_text: str) -> None:
    """The LM Studio model IDs an operator must download must appear verbatim."""
    models = yaml.safe_load((REPO_ROOT / "config" / "models.yaml").read_text())
    for tier_name, tier in models["tiers"].items():
        if tier.get("provider") != "lmstudio":
            continue
        model_id = tier["model"]
        assert model_id in readme_text, (
            f"LM Studio tier {tier_name!r} model id {model_id!r} not listed in README"
        )


def test_openrouter_env_var_documented(readme_text: str) -> None:
    assert "OPENROUTER_API_KEY" in readme_text
    assert "sk-or-" in readme_text, "key shape ('sk-or-' prefix) should be documented"


def test_caffeinate_block_present(readme_text: str) -> None:
    assert "caffeinate -i -w" in readme_text, "macOS hygiene must show `caffeinate -i -w <pid>`"


def test_auto_reboot_guidance_present(readme_lower: str) -> None:
    assert "softwareupdate --schedule off" in readme_lower or (
        "automatic updates" in readme_lower and "reboot" in readme_lower
    ), "macOS hygiene must cover disabling auto-reboot for system updates"


def test_launchd_plist_example_present(readme_text: str) -> None:
    assert "LaunchAgents" in readme_text
    assert "com.alpha.research.resume" in readme_text
    assert "research resume" in readme_text


def test_costs_section_covers_cap_behavior(readme_lower: str) -> None:
    for needle in ("90", "final-pass", "budgetexceeded", "frontier_speed"):
        assert needle in readme_lower, (
            f"Costs section should explain budget cap behavior; missing {needle!r}"
        )


def test_directory_layout_describes_per_job_folder(readme_text: str) -> None:
    for path in (
        "jobs/<job-id>/",
        "job.json",
        "intake.json",
        "events.jsonl",
        "daemon.pid",
        "report.history/",
        "STOP",
    ):
        assert path in readme_text, f"per-job folder contract missing {path!r}"


def test_directory_layout_describes_data(readme_text: str) -> None:
    for path in ("data/index.sqlite", "data/llm_cache.sqlite"):
        assert path in readme_text, f"data/ contract missing {path!r}"


def test_troubleshooting_lists_smoke_commands(readme_text: str) -> None:
    for cmd in ("research _smoke-llm", "research _smoke-tool"):
        assert cmd in readme_text, f"troubleshooting must show {cmd!r}"


def test_troubleshooting_points_to_event_locations(readme_text: str) -> None:
    for path in ("research logs", "events.jsonl", "daemon.err.log"):
        assert path in readme_text


def _cli_verb_paths() -> set[tuple[str, ...]]:
    """Walk the Typer app and return the full command path for every verb.

    A top-level verb like `research start` returns `("start",)`; a
    sub-typer command like `research config cache-clear` returns
    `("config", "cache-clear")`.
    """
    paths: set[tuple[str, ...]] = set()

    def _walk(app, prefix: tuple[str, ...]) -> None:
        for info in getattr(app, "registered_commands", []) or []:
            if info.name:
                paths.add(prefix + (info.name,))
        for group in getattr(app, "registered_groups", []) or []:
            sub = group.typer_instance
            if sub is None:
                continue
            sub_name = getattr(sub.info, "name", None) or group.name
            _walk(sub, prefix + (sub_name,))

    _walk(cli.app, ())
    return paths


def test_every_cli_verb_documented(readme_text: str) -> None:
    """Every public verb registered on the Typer app must appear in the README."""
    paths = _cli_verb_paths()
    # Hidden smoke verbs (leading underscore) live in Troubleshooting; they
    # are explicitly excluded from the verb tables.
    public = {p for p in paths if not any(seg.startswith("_") for seg in p)}
    assert public, "no CLI verbs discovered; sanity check failed"
    missing = []
    for path in public:
        needle = "research " + " ".join(path)
        if needle not in readme_text:
            missing.append(needle)
    assert not missing, f"CLI verbs not mentioned in README: {sorted(missing)}"


def test_env_keys_table_covers_expected_keys(readme_text: str) -> None:
    """Every key in EXPECTED_ENV_KEYS must show up in the env-keys table."""
    from research_agent import config as cfg

    for key in cfg.EXPECTED_ENV_KEYS:
        assert key.name in readme_text, f"env key {key.name!r} not documented in README"
