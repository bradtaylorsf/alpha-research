"""Tests for `research_agent.orchestrator.plan`."""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from research_agent.orchestrator.plan import Plan, Subgoal, TaskKind, TaskSpec

ALL_TASK_KINDS: tuple[str, ...] = get_args(TaskKind)


# ---------------------------------------------------------------------------
# TaskKind enumeration
# ---------------------------------------------------------------------------


def test_task_kind_covers_expected_set() -> None:
    expected = {
        "web_search",
        "web_fetch",
        "arxiv_search",
        "arxiv_fetch",
        "github_search",
        "github_fetch",
        "news_search",
        "reddit_search",
        "local_corpus_query",
        "extract_findings",
        "summarize_source",
        "synthesize",
        "critique",
    }
    assert set(ALL_TASK_KINDS) == expected


@pytest.mark.parametrize("kind", ALL_TASK_KINDS)
def test_task_spec_accepts_each_enumerated_kind(kind: str) -> None:
    spec = TaskSpec(kind=kind)  # type: ignore[arg-type]
    assert spec.kind == kind


def test_task_spec_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        TaskSpec(kind="not_a_real_kind")  # type: ignore[arg-type]


def test_task_spec_defaults() -> None:
    spec = TaskSpec(kind="web_search")
    assert spec.payload == {}
    assert spec.priority == 0
    assert spec.depends_on == []


def test_task_spec_round_trip() -> None:
    spec = TaskSpec(
        kind="web_fetch",
        payload={"url": "https://example.com"},
        priority=5,
        depends_on=[1, 2],
    )
    dumped = spec.model_dump()
    again = TaskSpec.model_validate(dumped)
    assert again == spec


def test_task_spec_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(
            {"kind": "web_search", "unknown": "boom"},
        )


# ---------------------------------------------------------------------------
# Subgoal
# ---------------------------------------------------------------------------


def test_subgoal_requires_non_empty_description() -> None:
    with pytest.raises(ValidationError):
        Subgoal(id=1, description="")


def test_subgoal_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError):
        Subgoal.model_validate({"id": 1, "description": "do thing", "extra": True})


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def _sample_plan(**overrides) -> Plan:
    base = {
        "version": 1,
        "objective": "Investigate the target",
        "subgoals": [
            Subgoal(id=1, description="Gather background"),
            Subgoal(id=2, description="Cross-reference sources"),
        ],
        "task_template": [
            TaskSpec(kind="web_search", payload={"q": "target"}),
            TaskSpec(kind="arxiv_search", payload={"q": "target"}, depends_on=[0]),
        ],
        "expected_iterations": 3,
    }
    base.update(overrides)
    return Plan(**base)


def test_plan_round_trip_through_model_dump_and_validate() -> None:
    plan = _sample_plan()
    dumped = plan.model_dump()
    rebuilt = Plan.model_validate(dumped)
    assert rebuilt == plan


def test_plan_is_complete_false_when_subgoals_empty() -> None:
    plan = _sample_plan(subgoals=[])
    assert plan.is_complete() is False


def test_plan_is_complete_false_when_any_not_done() -> None:
    plan = _sample_plan(
        subgoals=[
            Subgoal(id=1, description="a", done=True),
            Subgoal(id=2, description="b", done=False),
        ]
    )
    assert plan.is_complete() is False


def test_plan_is_complete_true_when_all_done() -> None:
    plan = _sample_plan(
        subgoals=[
            Subgoal(id=1, description="a", done=True),
            Subgoal(id=2, description="b", done=True),
        ]
    )
    assert plan.is_complete() is True


def test_plan_rejects_version_below_one() -> None:
    with pytest.raises(ValidationError):
        _sample_plan(version=0)


def test_plan_rejects_empty_objective() -> None:
    with pytest.raises(ValidationError):
        _sample_plan(objective="")


def test_plan_rejects_expected_iterations_below_one() -> None:
    with pytest.raises(ValidationError):
        _sample_plan(expected_iterations=0)


def test_plan_forbids_extra_keys() -> None:
    payload = _sample_plan().model_dump()
    payload["surprise"] = "boom"
    with pytest.raises(ValidationError):
        Plan.model_validate(payload)
