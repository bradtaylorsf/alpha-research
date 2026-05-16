"""Canonical report fragment registry for section-level synthesis.

The registry is the single in-code source of truth for report sections that
can be synthesized independently.  It intentionally carries only stable
section metadata and prompt/resource hints; model selection stays in prompt
frontmatter and router configuration.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

_FRAGMENT_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class FragmentSpec(BaseModel):
    """One canonical report fragment supported by the synthesizer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(..., description="Stable kebab-case fragment slug.")
    title: str = Field(..., description="Display heading expected in report prompts.")
    order: int = Field(..., description="Canonical report/synthesis ordering key.")
    depends_on: tuple[str, ...] = Field(default_factory=tuple)
    prompt_hint: str | None = None
    resource_hint: str | None = None


FRAGMENT_REGISTRY: tuple[FragmentSpec, ...] = (
    FragmentSpec(
        id="executive-summary",
        title="Executive Summary",
        order=10,
        prompt_hint="Summarize the strongest sourced conclusions in three to six bullets.",
    ),
    FragmentSpec(
        id="hypotheses",
        title="Working Hypotheses",
        order=20,
        prompt_hint="Update hypothesis status, confidence, support, and contradictions.",
    ),
    FragmentSpec(
        id="timeline",
        title="Timeline",
        order=30,
        prompt_hint="Extract dated events and preserve uncertainty around ambiguous dates.",
    ),
    FragmentSpec(
        id="stakeholder-map",
        title="Stakeholder Map",
        order=40,
        prompt_hint="Map people, organizations, agencies, roles, and relationships.",
    ),
    FragmentSpec(
        id="connections",
        title="Connections",
        order=50,
        depends_on=("stakeholder-map",),
        prompt_hint="Surface relationship inferences supported by multiple findings.",
    ),
    FragmentSpec(
        id="departmental-tracker",
        title="Departmental Policy Tracker",
        order=60,
        prompt_hint="Track agency or department-specific policy and implementation evidence.",
    ),
    FragmentSpec(
        id="confirmed-gaps",
        title="Confirmed Gaps",
        order=70,
        resource_hint="Use confirmed source-gap records from task and connector events.",
    ),
    FragmentSpec(
        id="open-questions",
        title="Open Questions",
        order=80,
        prompt_hint="List unresolved questions and the evidentiary reason each remains open.",
    ),
    FragmentSpec(
        id="recommended-human-followups",
        title="Recommended Human Follow-Ups",
        order=90,
        depends_on=("confirmed-gaps", "open-questions"),
        resource_hint="Use followup_recipes.md for names, statutes, and channels.",
    ),
    FragmentSpec(
        id="paid-resources",
        title="Paid Resources That Would Unblock This Investigation",
        order=100,
        depends_on=("confirmed-gaps",),
        resource_hint="Use paid_unblock_recipes.md for services and cost ranges.",
    ),
    FragmentSpec(
        id="sources",
        title="Sources",
        order=110,
        depends_on=(
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
        ),
        prompt_hint="Enumerate only cited sources with stable source IDs.",
    ),
)


def _validate_registry(registry: Sequence[FragmentSpec]) -> tuple[FragmentSpec, ...]:
    """Validate registry invariants and return an immutable copy."""

    seen: set[str] = set()
    for fragment in registry:
        if not _FRAGMENT_ID_RE.fullmatch(fragment.id):
            raise ValueError(f"fragment id must be kebab-case: {fragment.id!r}")
        if fragment.id in seen:
            raise ValueError(f"duplicate fragment id: {fragment.id!r}")
        seen.add(fragment.id)
        if not fragment.title.strip():
            raise ValueError(f"fragment {fragment.id!r} is missing a title")

    unknown: dict[str, list[str]] = {}
    for fragment in registry:
        missing = [dep for dep in fragment.depends_on if dep not in seen]
        if missing:
            unknown[fragment.id] = missing
    if unknown:
        details = ", ".join(f"{fid}: {deps}" for fid, deps in sorted(unknown.items()))
        raise ValueError(f"unknown fragment dependencies: {details}")

    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {fragment.id: fragment for fragment in registry}

    def visit(fragment_id: str, path: tuple[str, ...]) -> None:
        if fragment_id in visited:
            return
        if fragment_id in visiting:
            cycle = " -> ".join((*path, fragment_id))
            raise ValueError(f"fragment dependency cycle: {cycle}")
        visiting.add(fragment_id)
        fragment = by_id[fragment_id]
        for dep in fragment.depends_on:
            visit(dep, (*path, fragment_id))
        visiting.remove(fragment_id)
        visited.add(fragment_id)

    for fragment in registry:
        visit(fragment.id, ())

    return tuple(registry)


def _ordered_fragments(registry: Sequence[FragmentSpec]) -> tuple[FragmentSpec, ...]:
    """Return registry fragments sorted by order while honoring dependencies."""

    validated = _validate_registry(registry)
    by_id = {fragment.id: fragment for fragment in validated}
    remaining = set(by_id)
    emitted: list[FragmentSpec] = []
    emitted_ids: set[str] = set()

    while remaining:
        ready = [
            by_id[fragment_id]
            for fragment_id in remaining
            if set(by_id[fragment_id].depends_on).issubset(emitted_ids)
        ]
        if not ready:
            raise ValueError("fragment dependency cycle")
        ready.sort(key=lambda fragment: (fragment.order, fragment.id))
        next_fragment = ready[0]
        emitted.append(next_fragment)
        emitted_ids.add(next_fragment.id)
        remaining.remove(next_fragment.id)

    return tuple(emitted)


_REGISTRY: tuple[FragmentSpec, ...] = _validate_registry(FRAGMENT_REGISTRY)
_BY_ID: dict[str, FragmentSpec] = {fragment.id: fragment for fragment in _REGISTRY}
_IDS: frozenset[str] = frozenset(_BY_ID)
_ORDERED: tuple[FragmentSpec, ...] = _ordered_fragments(_REGISTRY)


def all_fragments() -> tuple[FragmentSpec, ...]:
    """Return every registered fragment in canonical registry order."""

    return _REGISTRY


def get_fragment(fragment_id: str) -> FragmentSpec:
    """Return a fragment spec by stable ID.

    Raises ``KeyError`` for unsupported fragments so downstream callers do
    not silently synthesize or persist unknown section names.
    """

    return _BY_ID[fragment_id]


def fragment_ids() -> frozenset[str]:
    """Return supported fragment IDs for validation at storage/model boundaries."""

    return _IDS


def synthesis_order(fragment_ids_subset: Iterable[str] | None = None) -> tuple[str, ...]:
    """Return fragment IDs in dependency-safe synthesis order."""

    ordered = tuple(fragment.id for fragment in _ORDERED)
    if fragment_ids_subset is None:
        return ordered
    requested = set(fragment_ids_subset)
    unknown = requested.difference(_IDS)
    if unknown:
        raise KeyError(f"unknown fragment id(s): {', '.join(sorted(unknown))}")
    return tuple(fragment_id for fragment_id in ordered if fragment_id in requested)


def dependency_closure(fragment_id: str) -> tuple[str, ...]:
    """Return transitive dependencies for ``fragment_id`` in synthesis order."""

    if fragment_id not in _BY_ID:
        raise KeyError(fragment_id)

    found: set[str] = set()

    def walk(current: str) -> None:
        for dep in _BY_ID[current].depends_on:
            if dep in found:
                continue
            found.add(dep)
            walk(dep)

    walk(fragment_id)
    return synthesis_order(found)


__all__ = [
    "FRAGMENT_REGISTRY",
    "FragmentSpec",
    "all_fragments",
    "dependency_closure",
    "fragment_ids",
    "get_fragment",
    "synthesis_order",
]
