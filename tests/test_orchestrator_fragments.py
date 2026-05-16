"""Tests for canonical synthesis fragment registry."""

from __future__ import annotations

import pytest

from research_agent.orchestrator import fragments
from research_agent.orchestrator.fragments import FragmentSpec


def test_registry_contains_canonical_fragment_ids() -> None:
    expected = {
        "executive-summary",
        "hypotheses",
        "timeline",
        "stakeholder-map",
        "connections",
        "departmental-tracker",
        "confirmed-gaps",
        "open-questions",
        "recommended-human-followups",
        "paid-resources",
        "sources",
    }

    assert fragments.fragment_ids() == expected


def test_registry_is_ordered_and_titles_match_prompt_expectations() -> None:
    ordered = fragments.all_fragments()

    assert [fragment.id for fragment in ordered] == [
        "executive-summary",
        "hypotheses",
        "timeline",
        "stakeholder-map",
        "connections",
        "departmental-tracker",
        "confirmed-gaps",
        "open-questions",
        "recommended-human-followups",
        "paid-resources",
        "sources",
    ]
    assert [fragment.order for fragment in ordered] == sorted(
        fragment.order for fragment in ordered
    )
    assert fragments.get_fragment("executive-summary").title == "Executive Summary"
    assert fragments.get_fragment("hypotheses").title == "Working Hypotheses"
    assert (
        fragments.get_fragment("departmental-tracker").title
        == "Departmental Policy Tracker"
    )


def test_get_fragment_lookup_and_unknown_id() -> None:
    assert fragments.get_fragment("connections").title == "Connections"

    with pytest.raises(KeyError):
        fragments.get_fragment("does-not-exist")


def test_synthesis_order_respects_dependencies() -> None:
    order = fragments.synthesis_order()

    assert order.index("stakeholder-map") < order.index("connections")
    assert order.index("confirmed-gaps") < order.index("recommended-human-followups")
    assert order.index("open-questions") < order.index("recommended-human-followups")
    assert order.index("confirmed-gaps") < order.index("paid-resources")
    assert order[-1] == "sources"


def test_synthesis_order_subset_validates_ids() -> None:
    assert fragments.synthesis_order({"sources", "connections", "stakeholder-map"}) == (
        "stakeholder-map",
        "connections",
        "sources",
    )

    with pytest.raises(KeyError):
        fragments.synthesis_order({"unknown"})


def test_dependency_closure_traversal() -> None:
    assert fragments.dependency_closure("recommended-human-followups") == (
        "confirmed-gaps",
        "open-questions",
    )
    assert "connections" in fragments.dependency_closure("sources")
    assert fragments.dependency_closure("connections") == ("stakeholder-map",)


def test_constructed_registry_dependency_order_can_override_numeric_order() -> None:
    registry = (
        FragmentSpec(id="child", title="Child", order=10, depends_on=("parent",)),
        FragmentSpec(id="parent", title="Parent", order=20),
    )

    ordered = fragments._ordered_fragments(registry)

    assert [fragment.id for fragment in ordered] == ["parent", "child"]


def test_registry_validation_rejects_duplicate_ids() -> None:
    registry = (
        FragmentSpec(id="dup", title="One", order=1),
        FragmentSpec(id="dup", title="Two", order=2),
    )

    with pytest.raises(ValueError, match="duplicate fragment id"):
        fragments._validate_registry(registry)


def test_registry_validation_rejects_missing_titles() -> None:
    registry = (FragmentSpec(id="untitled", title=" ", order=1),)

    with pytest.raises(ValueError, match="missing a title"):
        fragments._validate_registry(registry)


def test_registry_validation_rejects_unknown_dependencies() -> None:
    registry = (FragmentSpec(id="child", title="Child", order=1, depends_on=("parent",)),)

    with pytest.raises(ValueError, match="unknown fragment dependencies"):
        fragments._validate_registry(registry)


def test_registry_validation_rejects_dependency_cycles() -> None:
    registry = (
        FragmentSpec(id="one", title="One", order=1, depends_on=("two",)),
        FragmentSpec(id="two", title="Two", order=2, depends_on=("one",)),
    )

    with pytest.raises(ValueError, match="dependency cycle"):
        fragments._validate_registry(registry)
