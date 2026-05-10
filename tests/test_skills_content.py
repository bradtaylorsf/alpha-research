"""Validate the shipped connector and strategy skill markdown content.

Loader behavior is covered in ``test_skills_loader.py``; this file validates
the *content* of the 5 initial connector skills (congress, edgar, fedregister,
courtlistener, fec) and the shipped strategy skills — frontmatter completeness,
presence of required body sections, and that any knobs the skill names match
the connector's actual ``search()`` signature.
"""

from __future__ import annotations

import importlib
import inspect
import re

import pytest

from research_agent.skills import loader as skills_loader
from research_agent.skills.loader import clear_cache, list_skills, load_skill

CONNECTOR_SKILLS = ("congress", "edgar", "fedregister", "courtlistener", "fec")

STRATEGY_SKILLS = (
    "modern-policy-era-filtering",
    "cornerstone-extraction",
    "triangulation",
    "multilingual-source-handling",
)

REQUIRED_BODY_SECTIONS = ("Knobs available", "Anti-patterns")


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    clear_cache()
    yield
    clear_cache()


def _connector_search_params(name: str) -> set[str]:
    module = importlib.import_module(f"research_agent.tools.{name}")
    sig = inspect.signature(module.search)
    return set(sig.parameters)


def _knobs_section_identifiers(body: str) -> set[str]:
    """Extract backtick-wrapped identifiers from the ``## Knobs available`` section.

    A skill's ``## Knobs available`` section uses bullets like
    ``- `kind` — ...``. We collect the first backtick-wrapped token on each
    bullet line so the test can verify those names exist on the connector's
    ``search()`` signature.
    """
    match = re.search(
        r"##\s*Knobs available\s*\n(?P<body>.*?)(?:\n##\s|\Z)",
        body,
        re.DOTALL,
    )
    if not match:
        return set()
    knob_section = match.group("body")
    knobs: set[str] = set()
    for line in knob_section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        m = re.search(r"`([A-Za-z_][A-Za-z0-9_]*)`", stripped)
        if m:
            knobs.add(m.group(1))
    return knobs


@pytest.mark.parametrize("name", CONNECTOR_SKILLS)
def test_connector_skill_loads_with_non_empty_body(name: str) -> None:
    body = load_skill("connectors", name)
    assert body, f"connector skill {name!r} body is empty"
    assert len(body) > 200, f"connector skill {name!r} body looks truncated"


@pytest.mark.parametrize("name", CONNECTOR_SKILLS)
def test_connector_skill_frontmatter_fields_present(name: str) -> None:
    entries = {e["name"]: e for e in list_skills("connectors")}
    assert name in entries, f"skill {name!r} not found in connectors index"
    entry = entries[name]
    assert entry["description"], f"{name}: description missing"
    assert entry["when_to_use"], f"{name}: when_to_use missing"
    assert entry["when_not_to_use"], f"{name}: when_not_to_use missing"


@pytest.mark.parametrize("name", CONNECTOR_SKILLS)
@pytest.mark.parametrize("section", REQUIRED_BODY_SECTIONS)
def test_connector_skill_has_required_section(name: str, section: str) -> None:
    body = load_skill("connectors", name)
    pattern = rf"^##\s+{re.escape(section)}\s*$"
    assert re.search(pattern, body, re.MULTILINE), (
        f"{name}: missing required section '## {section}'"
    )


@pytest.mark.parametrize("name", CONNECTOR_SKILLS)
def test_connector_skill_knobs_match_search_signature(name: str) -> None:
    body = load_skill("connectors", name)
    declared_knobs = _knobs_section_identifiers(body)
    assert declared_knobs, f"{name}: no knobs found in '## Knobs available' section"

    actual_params = _connector_search_params(name)
    unknown = declared_knobs - actual_params
    assert not unknown, (
        f"{name}: knobs {sorted(unknown)} are not parameters of "
        f"research_agent.tools.{name}.search() (actual: {sorted(actual_params)})"
    )


def test_congress_skill_carries_canonical_motivator() -> None:
    """The 110th-Congress / IRA relevance trap is the headline reason this
    skill exists; the body must keep that example intact."""
    body = load_skill("connectors", "congress")
    assert "117" in body
    assert "119" in body
    assert "Inflation Reduction Act" in body


def test_skill_descriptions_are_one_line() -> None:
    """The description is the planner-facing index signal — must stay terse
    and single-line so it stays cheap to render across all 18 connectors."""
    for entry in list_skills("connectors"):
        assert "\n" not in entry["description"], (
            f"{entry['name']}: description must be a single line"
        )
        assert len(entry["description"]) <= 280, (
            f"{entry['name']}: description longer than 280 chars "
            f"({len(entry['description'])})"
        )


def test_loader_module_resolves_skills_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: skill files live where the loader expects (no install/path drift)."""
    base = skills_loader._skills_dir("connectors")
    assert base.is_dir(), f"connectors directory missing at {base}"
    shipped = sorted(p.stem for p in base.glob("*.md"))
    for name in CONNECTOR_SKILLS:
        assert name in shipped, f"{name}.md missing from {base}"


# ---------------------------------------------------------------------------
# Strategy skills
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", STRATEGY_SKILLS)
def test_strategy_skill_loads_with_non_empty_body(name: str) -> None:
    body = load_skill("strategies", name)
    assert body, f"strategy skill {name!r} body is empty"
    assert len(body) > 200, f"strategy skill {name!r} body looks truncated"


@pytest.mark.parametrize("name", STRATEGY_SKILLS)
def test_strategy_skill_frontmatter_fields_present(name: str) -> None:
    entries = {e["name"]: e for e in list_skills("strategies")}
    assert name in entries, f"skill {name!r} not found in strategies index"
    entry = entries[name]
    assert entry["description"], f"{name}: description missing"
    assert entry["when_to_use"], f"{name}: when_to_use missing"


def test_strategy_descriptions_are_one_line() -> None:
    """The description is the planner-facing index signal — single-line, terse."""
    for entry in list_skills("strategies"):
        assert "\n" not in entry["description"], (
            f"{entry['name']}: description must be a single line"
        )
        assert len(entry["description"]) <= 280, (
            f"{entry['name']}: description longer than 280 chars "
            f"({len(entry['description'])})"
        )


def test_modern_policy_era_filtering_references_every_connector() -> None:
    """The strategy's value prop is "stack onto every connector" — its body
    must concretely name each shipped connector and reference the one real
    date knob (`since` on fedregister), so the planner gets actionable
    guidance instead of generic principles."""
    body = load_skill("strategies", "modern-policy-era-filtering")
    for connector in CONNECTOR_SKILLS:
        assert connector in body, (
            f"modern-policy-era-filtering: body must reference connector "
            f"{connector!r} so planner gets per-connector directive"
        )
    assert "since" in body, (
        "modern-policy-era-filtering: must reference the `since` knob "
        "(the one real date parameter on fedregister.search())"
    )
    assert "2025-01-20" in body, (
        "modern-policy-era-filtering: must reference the 119th-Congress / "
        "Trump-2-inauguration anchor date"
    )


def test_strategies_directory_resolves() -> None:
    """Sanity: strategy files live where the loader expects."""
    base = skills_loader._skills_dir("strategies")
    assert base.is_dir(), f"strategies directory missing at {base}"
    shipped = sorted(p.stem for p in base.glob("*.md"))
    for name in STRATEGY_SKILLS:
        assert name in shipped, f"{name}.md missing from {base}"
