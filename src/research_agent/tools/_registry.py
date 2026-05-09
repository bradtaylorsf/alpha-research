"""Connector kind registry — single source of truth for direct connector kinds.

Each connector module that exposes a ``<name>_search`` direct kind declares
its registration at module scope, so importing :mod:`research_agent.tools`
is enough to populate the registry. Three downstream surfaces read from it:

* the planner prompt — ``Direct connector kinds`` table, ``Hard rules``
  allowlist sentence, and the tactical-replan preference list (rendered at
  prompt-load time, see :func:`render_direct_kinds_table` /
  :func:`render_kinds_allowlist` / :func:`render_tactical_replan_kinds`);
* the orchestrator — ``default_handlers`` walks :func:`iter_kinds` to wire
  one search handler + (optional) fetch handler per kind;
* ``research doctor`` — coherence checks assert the planner prompt and the
  ``skills/connectors/`` folder agree with the registry.

Issue #223: hand-maintaining the connector list across prompt + orchestrator
+ README guarantees drift and produces simultaneous merge conflicts on
every concurrent connector PR. The registry is the foundation for the rest
of the open-archives epic and the composable-research-service epic's MCP
auto-registration.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict


SearchFn = Callable[..., Awaitable[Any]]
FetchFn = Callable[..., Awaitable[Any]]

# Sentinel for ``register_kind(skill_name=...)`` so we can distinguish
# "caller omitted the kwarg → default to the short name" from "caller
# explicitly passed None → grandfathered, no skill file expected yet."
_SKILL_NAME_UNSET: Any = object()


class BaseSearchPayload(BaseModel):
    """Shared payload contract for every direct connector ``<name>_search``.

    Connector-specific schemas extend this with their optional knobs (e.g.
    ``kind``, ``state``, ``form_type``). ``extra="ignore"`` so orchestrator-
    threaded payload fields like ``_active_strategies`` and ``expand_top_k``
    pass through without tripping validation — the connector's own kwargs
    are the only thing that matter to validation.
    """

    model_config = ConfigDict(extra="ignore")

    query: str
    sub_question: str


@dataclass(frozen=True)
class KindEntry:
    """One registered direct-connector ``<name>_search`` kind."""

    name: str
    payload_schema: type[BaseModel]
    search_fn: SearchFn
    fetch_fn: FetchFn | None
    host_patterns: tuple[str, ...]
    skill_name: str | None
    description: str
    optional_payload_knobs: str
    example_query: str
    module_name: str = field(default="")

    @property
    def short_name(self) -> str:
        """``congress`` for ``congress_search``."""
        return self.name.removesuffix("_search")


_REGISTRY: dict[str, KindEntry] = {}


class RegistryError(ValueError):
    """Raised when registry operations violate invariants."""


def register_kind(
    name: str,
    *,
    payload_schema: type[BaseModel],
    search_fn: SearchFn,
    fetch_fn: FetchFn | None = None,
    host_patterns: tuple[str, ...] = (),
    skill_name: str | None = _SKILL_NAME_UNSET,
    description: str = "",
    optional_payload_knobs: str = "",
    example_query: str = "",
    module_name: str = "",
) -> KindEntry:
    """Register a direct-connector ``<name>_search`` kind.

    Raises :class:`RegistryError` if ``name`` is already registered.
    Omitting ``skill_name`` defaults to ``name.removesuffix('_search')`` so
    a connector named ``loc_search`` looks up ``skills/connectors/loc.md``;
    passing ``skill_name=None`` *explicitly* opts out of the coherence
    check (used while a skill file is being backfilled).
    """
    if not name.endswith("_search"):
        raise RegistryError(
            f"register_kind: kind names must end with '_search' (got {name!r})"
        )
    if name in _REGISTRY:
        raise RegistryError(f"register_kind: {name!r} is already registered")

    short = name.removesuffix("_search")
    if skill_name is _SKILL_NAME_UNSET:
        resolved_skill: str | None = short
    else:
        resolved_skill = skill_name

    entry = KindEntry(
        name=name,
        payload_schema=payload_schema,
        search_fn=search_fn,
        fetch_fn=fetch_fn,
        host_patterns=tuple(host_patterns),
        skill_name=resolved_skill,
        description=description,
        optional_payload_knobs=optional_payload_knobs,
        example_query=example_query,
        module_name=module_name or short,
    )
    _REGISTRY[name] = entry
    return entry


def iter_kinds() -> list[KindEntry]:
    """Return every registered kind in deterministic alphabetical order.

    The planner prompt, the orchestrator handler registry, the README
    table, and the doctor coherence checks all walk this list — alphabetical
    ordering keeps diffs stable when a new connector slides in.
    """
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def get_kind(name: str) -> KindEntry | None:
    """Return the entry for ``name`` or ``None`` if unregistered."""
    return _REGISTRY.get(name)


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def validate_payload(name: str, payload: dict[str, Any]) -> BaseModel:
    """Validate ``payload`` against the registered kind's pydantic schema.

    Raises :class:`RegistryError` when the kind is unknown. Pydantic raises
    its own ``ValidationError`` when payload fields are missing or wrong-
    typed; the registry intentionally lets that bubble so the loop can
    surface it as a ``FatalError`` against the offending task.
    """
    entry = _REGISTRY.get(name)
    if entry is None:
        raise RegistryError(f"validate_payload: unknown kind {name!r}")
    return entry.payload_schema.model_validate(payload)


# ---------------------------------------------------------------------------
# Planner-prompt rendering helpers.
# ---------------------------------------------------------------------------

_TABLE_HEADER = (
    "| Kind | What it covers | Optional payload knobs | Example query |\n"
    "|---|---|---|---|"
)


def render_direct_kinds_table() -> str:
    """Render the **Direct connector kinds** markdown table.

    Each row is ``| `<kind>` | <description> | <optional knobs> | `<example>` |``.
    Missing knobs render as ``—`` so the column stays visually balanced
    without leaving an empty cell that confuses model parsers.
    """
    rows: list[str] = [_TABLE_HEADER]
    for entry in iter_kinds():
        knobs = entry.optional_payload_knobs.strip() or "—"
        example = entry.example_query.strip() or ""
        example_cell = f"`{example}`" if example else "—"
        rows.append(
            f"| `{entry.name}` | {entry.description} | {knobs} | {example_cell} |"
        )
    return "\n".join(rows)


def render_kinds_allowlist() -> str:
    """Render the comma-joined ``\\`<name>_search\\``` allowlist sentence.

    Used in the planner prompt's **Hard rules** section. Every line breaks
    softly in the rendered markdown but the registry-driven contract is the
    single comma-joined string returned here.
    """
    quoted = [f"`{entry.name}`" for entry in iter_kinds()]
    return ", ".join(quoted)


def render_tactical_replan_kinds() -> str:
    """Render the tactical-replan preference list.

    The drill-down rule names a handful of direct connector kinds the
    planner should reach for when a sub-question targets a specific subject.
    Pre-#223 this was a hand-maintained subset of the full kind list; the
    refactor renders the full registry so the replan section never falls
    out of sync with the table above. Returns a comma-joined backticked
    list ready to drop into the prompt.
    """
    return render_kinds_allowlist()


def registered_skill_pairs() -> list[tuple[KindEntry, str]]:
    """Return ``(entry, skill_name)`` for every kind whose ``skill_name`` is set.

    Used by ``research doctor`` and the integration coherence script to
    iterate the kinds that *expect* a skill file in ``skills/connectors/``.
    Kinds with ``skill_name=None`` (grandfathered, backfill pending) are
    excluded.
    """
    return [
        (entry, entry.skill_name)
        for entry in iter_kinds()
        if entry.skill_name
    ]


def _reset_for_tests() -> None:
    """Clear the registry. Tests only — never call from production code."""
    _REGISTRY.clear()


__all__ = [
    "BaseSearchPayload",
    "KindEntry",
    "RegistryError",
    "get_kind",
    "is_registered",
    "iter_kinds",
    "register_kind",
    "registered_skill_pairs",
    "render_direct_kinds_table",
    "render_kinds_allowlist",
    "render_tactical_replan_kinds",
    "validate_payload",
]
