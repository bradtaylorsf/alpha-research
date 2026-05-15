"""Unit tests for the connector kind registry (issue #223)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from research_agent.tools import _registry as registry
from research_agent.tools._registry import (
    BaseSearchPayload,
    KindEntry,
    RegistryError,
    iter_kinds,
    register_kind,
    render_direct_kinds_table,
    render_kinds_allowlist,
    validate_payload,
)


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, KindEntry]:
    """Swap the module-level registry for an empty dict per test."""
    fresh: dict[str, KindEntry] = {}
    monkeypatch.setattr(registry, "_REGISTRY", fresh)
    return fresh


class _DummyPayload(BaseSearchPayload):
    pass


async def _dummy_search(query: str, **_: Any) -> list[Any]:
    return []


async def _dummy_fetch(url: str, **_: Any) -> Any:
    return None


def test_register_kind_rejects_duplicates(empty_registry: dict[str, KindEntry]) -> None:
    register_kind(
        "x_search",
        payload_schema=_DummyPayload,
        search_fn=_dummy_search,
    )
    with pytest.raises(RegistryError, match="already registered"):
        register_kind(
            "x_search",
            payload_schema=_DummyPayload,
            search_fn=_dummy_search,
        )


def test_register_kind_rejects_non_search_suffix(
    empty_registry: dict[str, KindEntry],
) -> None:
    with pytest.raises(RegistryError, match="must end with '_search'"):
        register_kind(
            "x_lookup",
            payload_schema=_DummyPayload,
            search_fn=_dummy_search,
        )


def test_skill_name_defaults_to_short_name(
    empty_registry: dict[str, KindEntry],
) -> None:
    entry = register_kind(
        "loc_search",
        payload_schema=_DummyPayload,
        search_fn=_dummy_search,
    )
    assert entry.skill_name == "loc"


def test_skill_name_explicit_none_grandfathers(
    empty_registry: dict[str, KindEntry],
) -> None:
    entry = register_kind(
        "loc_search",
        payload_schema=_DummyPayload,
        search_fn=_dummy_search,
        skill_name=None,
    )
    assert entry.skill_name is None


def test_skill_name_can_be_overridden(
    empty_registry: dict[str, KindEntry],
) -> None:
    entry = register_kind(
        "loc_search",
        payload_schema=_DummyPayload,
        search_fn=_dummy_search,
        skill_name="custom",
    )
    assert entry.skill_name == "custom"


def test_iter_kinds_returns_alphabetical(
    empty_registry: dict[str, KindEntry],
) -> None:
    register_kind(
        "zzz_search",
        payload_schema=_DummyPayload,
        search_fn=_dummy_search,
    )
    register_kind(
        "aaa_search",
        payload_schema=_DummyPayload,
        search_fn=_dummy_search,
    )
    register_kind(
        "mmm_search",
        payload_schema=_DummyPayload,
        search_fn=_dummy_search,
    )
    names = [e.name for e in iter_kinds()]
    assert names == ["aaa_search", "mmm_search", "zzz_search"]


def test_validate_payload_accepts_well_formed(
    empty_registry: dict[str, KindEntry],
) -> None:
    class _PS(BaseSearchPayload):
        max_results: int | None = None

    register_kind(
        "x_search",
        payload_schema=_PS,
        search_fn=_dummy_search,
    )
    parsed = validate_payload(
        "x_search",
        {"query": "abc", "sub_question": "what?", "max_results": 5},
    )
    assert isinstance(parsed, _PS)
    assert parsed.query == "abc"


def test_validate_payload_rejects_missing_required(
    empty_registry: dict[str, KindEntry],
) -> None:
    register_kind(
        "x_search",
        payload_schema=_DummyPayload,
        search_fn=_dummy_search,
    )
    with pytest.raises(ValidationError):
        validate_payload("x_search", {"query": "abc"})  # missing sub_question


def test_validate_payload_unknown_kind_raises(
    empty_registry: dict[str, KindEntry],
) -> None:
    with pytest.raises(RegistryError, match="unknown kind"):
        validate_payload("nope_search", {"query": "abc", "sub_question": "x"})


def test_validate_payload_ignores_orchestrator_extras(
    empty_registry: dict[str, KindEntry],
) -> None:
    """Orchestrator-injected fields like ``_active_strategies`` must pass."""
    register_kind(
        "x_search",
        payload_schema=_DummyPayload,
        search_fn=_dummy_search,
    )
    parsed = validate_payload(
        "x_search",
        {
            "query": "abc",
            "sub_question": "what?",
            "_active_strategies": ["modern-policy-era-filtering"],
            "expand_top_k": 7,
        },
    )
    assert parsed.query == "abc"


def test_render_direct_kinds_table_one_row_per_kind(
    empty_registry: dict[str, KindEntry],
) -> None:
    register_kind(
        "alpha_search",
        payload_schema=_DummyPayload,
        search_fn=_dummy_search,
        description="Alpha description",
        optional_payload_knobs="`kind: a\\|b`",
        example_query="alpha example",
    )
    register_kind(
        "beta_search",
        payload_schema=_DummyPayload,
        search_fn=_dummy_search,
        description="Beta description",
    )
    rendered = render_direct_kinds_table()
    assert "| `alpha_search` | Alpha description | `kind: a\\|b` | `alpha example` |" in rendered
    # No knobs / example for beta — table renders ``—`` to keep cells aligned.
    assert "| `beta_search` | Beta description | — | — |" in rendered
    # Header is intact.
    assert rendered.startswith("| Kind | What it covers | Optional payload knobs | Example query |")


def test_render_kinds_allowlist_alphabetical(
    empty_registry: dict[str, KindEntry],
) -> None:
    register_kind("zzz_search", payload_schema=_DummyPayload, search_fn=_dummy_search)
    register_kind("aaa_search", payload_schema=_DummyPayload, search_fn=_dummy_search)
    rendered = render_kinds_allowlist()
    assert rendered == "`aaa_search`, `zzz_search`"


def test_module_name_defaults_to_short_name(
    empty_registry: dict[str, KindEntry],
) -> None:
    entry = register_kind(
        "loc_search",
        payload_schema=_DummyPayload,
        search_fn=_dummy_search,
    )
    assert entry.module_name == "loc"


# ---------------------------------------------------------------------------
# End-to-end registry sanity (against the live, production registry).
# ---------------------------------------------------------------------------


def test_live_registry_has_all_registered_connectors() -> None:
    """The shipped direct-connector kinds all register."""
    import research_agent.tools  # noqa: F401 — ensure registration ran

    expected = {
        "bbb_search",
        "bne_search",
        "calaccess_search",
        "commons_search",
        "congress_search",
        "cspan_search",
        "courtlistener_search",
        "dpla_search",
        "edgar_search",
        "europeana_search",
        "fec_search",
        "fedregister_search",
        "gallica_search",
        "gdelt_search",
        "iarchive_search",
        "iwm_search",
        "lda_search",
        "licensing_search",
        "linkedin_search",
        "littlesis_search",
        "loc_search",
        "nara_search",
        "nonprofits_search",
        "openalex_search",
        "openlibrary_search",
        "opencorporates_search",
        "persee_search",
        "sanctions_search",
        "scholar_search",
        "si_search",
        "sos_search",
        "state_election_search",
        "trove_search",
        "ukna_search",
        "usaspending_search",
        "wikidata_search",
        "wikisource_search",
    }
    actual = {entry.name for entry in iter_kinds()}
    assert expected == actual


def test_live_registry_skill_name_assignment() -> None:
    """Connectors with shipping skills wire ``skill_name``."""
    import research_agent.tools  # noqa: F401

    skilled = {
        entry.name: entry.skill_name
        for entry in iter_kinds()
        if entry.skill_name is not None
    }
    assert skilled == {
        "congress_search": "congress",
        "bne_search": "bne",
        "commons_search": "commons",
        "courtlistener_search": "courtlistener",
        "cspan_search": "cspan",
        "dpla_search": "dpla",
        "edgar_search": "edgar",
        "europeana_search": "europeana",
        "fec_search": "fec",
        "fedregister_search": "fedregister",
        "gallica_search": "gallica",
        "iarchive_search": "iarchive",
        "iwm_search": "iwm",
        "loc_search": "loc",
        "nara_search": "nara",
        "openalex_search": "openalex",
        "openlibrary_search": "openlibrary",
        "persee_search": "persee",
        "si_search": "smithsonian",
        "state_election_search": "state_election",
        "trove_search": "trove",
        "ukna_search": "ukna",
        "wikidata_search": "wikidata",
        "wikisource_search": "wikisource",
    }
