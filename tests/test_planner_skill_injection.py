"""Tests that the planner system prompt embeds the skills index."""

from __future__ import annotations

from pathlib import Path

import pytest

from research_agent.orchestrator import plan as plan_module
from research_agent.prompts import loader as prompts_loader
from research_agent.skills import loader as skills_loader
from research_agent.storage import db
from research_agent.storage.jobs import Job


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    prompts_loader.clear_cache()
    skills_loader.clear_cache()
    yield
    prompts_loader.clear_cache()
    skills_loader.clear_cache()


@pytest.fixture
def isolated_skills_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "skills"
    (root / "connectors").mkdir(parents=True)
    (root / "strategies").mkdir(parents=True)

    def _dir(category: str) -> Path:
        return root / category

    monkeypatch.setattr(skills_loader, "_skills_dir", _dir)
    return root


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.sqlite"
    db.migrate(path=path).close()
    return path


@pytest.fixture
def job(tmp_path: Path, db_path: Path) -> Job:
    return Job.create(
        {"goal": "Map Project 2025 implementation across federal departments."},
        jobs_root=tmp_path / "jobs",
        db_path=db_path,
    )


def _write_skill(dir_: Path, category: str, name: str, description: str) -> None:
    path = dir_ / category / f"{name}.md"
    path.write_text(
        f'---\ndescription: "{description}"\n---\nBody for {name}.\n',
        encoding="utf-8",
    )


def test_render_planner_prompt_embeds_connector_index(
    isolated_skills_dir: Path, job: Job
) -> None:
    _write_skill(
        isolated_skills_dir,
        "connectors",
        "congress",
        "Congress.gov bills, members, hearings.",
    )
    _write_skill(
        isolated_skills_dir,
        "connectors",
        "edgar",
        "SEC filings — 10-K, 10-Q, 8-K, Form 4.",
    )

    rendered = plan_module._render_planner_prompt(job)

    assert "## Connector skills available" in rendered
    assert "`congress_search`: Congress.gov bills, members, hearings." in rendered
    assert "`edgar_search`: SEC filings — 10-K, 10-Q, 8-K, Form 4." in rendered
    # No unfilled placeholders make it through.
    assert "{{connector_skills_index}}" not in rendered
    assert "{{strategy_skills_index}}" not in rendered
    assert "{{goal}}" not in rendered
    assert job.goal in rendered


def test_render_planner_prompt_embeds_strategy_index(
    isolated_skills_dir: Path, job: Job
) -> None:
    _write_skill(
        isolated_skills_dir,
        "strategies",
        "modern-policy-era-filtering",
        "Filter to current Congress / current administration.",
    )

    rendered = plan_module._render_planner_prompt(job)

    assert "## Strategy skills available" in rendered
    assert (
        "`modern-policy-era-filtering`: Filter to current Congress / current administration."
        in rendered
    )


def test_render_planner_prompt_handles_empty_skills_dirs(
    isolated_skills_dir: Path, job: Job
) -> None:
    rendered = plan_module._render_planner_prompt(job)

    # Both sections present and rendered without crashing or leaving placeholders.
    assert "## Connector skills available" in rendered
    assert "## Strategy skills available" in rendered
    assert "{{connector_skills_index}}" not in rendered
    assert "{{strategy_skills_index}}" not in rendered
    # Empty-section rendering keeps the prompt valid (no orphan trailing
    # bullet that would confuse the model).
    assert "(none)" in rendered


def test_active_strategies_field_default_is_empty(isolated_skills_dir: Path) -> None:
    plan = plan_module.Plan(
        version=1,
        objective="X.",
        subgoals=[plan_module.Subgoal(id=1, description="foo")],
        task_template=[],
        expected_iterations=1,
    )
    assert plan.active_strategies == []


def test_planner_md_documents_active_strategies_in_schema() -> None:
    """Regression for the empty-active_strategies-on-plan_created bug:
    planner.md must list ``active_strategies`` in the formal Schema bullet
    list, not just mention it in the prose intro. Models follow the schema
    section literally and skip prose-only fields, leaving the field empty
    even when strategies are obviously relevant. Discovered on a 60-min
    Project 2025 smoke: scope_class=broad fired correctly, but
    active_strategies stayed [] despite the goal being a textbook fit for
    modern-policy-era-filtering + cornerstone-extraction."""
    body = (
        Path(__file__).resolve().parent.parent
        / "src/research_agent/prompts/planner.md"
    ).read_text()
    schema_section = body.split("### Schema", 1)[-1].split("###", 1)[0]
    assert "`active_strategies`" in schema_section, (
        "planner.md schema bullets must include `active_strategies` so the "
        "planner emits the field in its YAML output"
    )
    # Concrete guidance — without it the model can't decide when to populate.
    assert "modern-policy-era-filtering" in schema_section


def test_active_strategies_threaded_into_payload_on_enqueue(
    isolated_skills_dir: Path, job: Job
) -> None:
    plan = plan_module.Plan(
        version=1,
        objective="X.",
        subgoals=[plan_module.Subgoal(id=1, description="foo")],
        task_template=[
            plan_module.TaskSpec(
                kind="congress_search",
                payload={"query": "Inflation Reduction Act", "sub_question": "scope"},
            ),
        ],
        expected_iterations=1,
        active_strategies=["modern-policy-era-filtering"],
    )

    from research_agent.storage.markdown import write_plan

    write_plan(job, plan.model_dump())
    plan_module._enqueue_plan_tasks(job, plan)

    from research_agent.storage.tasks import next_pending

    pending = next_pending(job)
    assert pending is not None
    assert pending["payload"]["_active_strategies"] == ["modern-policy-era-filtering"]
    assert pending["payload"]["query"] == "Inflation Reduction Act"
