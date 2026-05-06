"""Tests for `research_agent.tools.models`.

Locks the connector return-shape contract from §7 of the implementation guide.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic import ValidationError

from research_agent.tools.models import SearchResult, Source, SourceKind

EXPECTED_SOURCE_KINDS = (
    "web",
    "pdf",
    "audio",
    "image",
    "github",
    "arxiv",
    "news",
    "reddit",
    "hn",
    "local",
    "gdelt",
    "sec",
    "fec",
    "courtlistener",
    "fedregister",
)


# ---------------------------------------------------------------------------
# SourceKind enum coverage
# ---------------------------------------------------------------------------


def test_source_kind_covers_all_verticals() -> None:
    assert set(get_args(SourceKind)) == set(EXPECTED_SOURCE_KINDS)
    assert len(get_args(SourceKind)) == len(EXPECTED_SOURCE_KINDS)


@pytest.mark.parametrize("kind", EXPECTED_SOURCE_KINDS)
def test_search_result_accepts_each_source_kind(kind: str) -> None:
    sr = SearchResult(url="https://e.com", title="t", snippet="s", source_kind=kind)  # type: ignore[arg-type]
    assert sr.source_kind == kind


@pytest.mark.parametrize("kind", EXPECTED_SOURCE_KINDS)
def test_source_accepts_each_source_kind(kind: str) -> None:
    src = Source(
        url="https://e.com",
        title="t",
        cleaned_text="body",
        fetched_at=datetime.now(UTC),
        source_kind=kind,  # type: ignore[arg-type]
    )
    assert src.source_kind == kind


# ---------------------------------------------------------------------------
# SearchResult
# ---------------------------------------------------------------------------


def test_search_result_minimal_construction() -> None:
    sr = SearchResult(
        url="https://example.com/a",
        title="Example A",
        snippet="An example snippet.",
        source_kind="web",
    )
    assert sr.url == "https://example.com/a"
    assert sr.title == "Example A"
    assert sr.snippet == "An example snippet."
    assert sr.source_kind == "web"
    assert sr.published_at is None
    assert sr.score is None
    assert sr.extras == {}


def test_search_result_full_construction() -> None:
    pub = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    sr = SearchResult(
        url="https://example.com/b",
        title="Example B",
        snippet="Snippet",
        published_at=pub,
        source_kind="news",
        score=0.87,
        extras={"author": "j.doe", "tags": ["politics"]},
    )
    assert sr.published_at == pub
    assert sr.score == 0.87
    assert sr.extras == {"author": "j.doe", "tags": ["politics"]}


def test_search_result_published_at_accepts_iso_string() -> None:
    sr = SearchResult(
        url="https://e.com",
        title="t",
        snippet="s",
        published_at="2025-06-01T12:00:00+00:00",  # type: ignore[arg-type]
        source_kind="web",
    )
    assert sr.published_at == datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


def test_search_result_invalid_source_kind_raises() -> None:
    with pytest.raises(ValidationError):
        SearchResult(
            url="https://e.com",
            title="t",
            snippet="s",
            source_kind="twitter",  # type: ignore[arg-type]
        )


def test_search_result_extras_default_is_independent_dict() -> None:
    a = SearchResult(url="https://a", title="a", snippet="a", source_kind="web")
    b = SearchResult(url="https://b", title="b", snippet="b", source_kind="web")
    a.extras["k"] = "v"
    assert b.extras == {}
    assert a.extras is not b.extras


def test_search_result_round_trip_json() -> None:
    pub = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    sr = SearchResult(
        url="https://e.com",
        title="t",
        snippet="s",
        published_at=pub,
        source_kind="arxiv",
        score=0.5,
        extras={"arxiv_id": "2501.00001"},
    )
    parsed = SearchResult.model_validate_json(sr.model_dump_json())
    assert parsed == sr


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


def test_source_minimal_construction() -> None:
    fetched = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    src = Source(
        url="https://example.com/x",
        title="X",
        cleaned_text="hello world",
        fetched_at=fetched,
        source_kind="web",
    )
    assert src.url == "https://example.com/x"
    assert src.cleaned_text == "hello world"
    assert src.fetched_at == fetched
    assert src.raw_html is None
    assert src.archive_url is None
    assert src.metadata == {}


def test_source_full_construction() -> None:
    fetched = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    src = Source(
        url="https://example.com/x",
        title="X",
        cleaned_text="cleaned",
        raw_html="<html>...</html>",
        fetched_at=fetched,
        source_kind="pdf",
        archive_url="https://web.archive.org/web/2025/https://example.com/x",
        metadata={"sha256": "abc123", "page_count": 7},
    )
    assert src.raw_html == "<html>...</html>"
    assert src.archive_url is not None
    assert src.metadata["page_count"] == 7


def test_source_fetched_at_accepts_iso_string() -> None:
    src = Source(
        url="https://e.com",
        title="t",
        cleaned_text="body",
        fetched_at="2025-06-01T12:00:00+00:00",  # type: ignore[arg-type]
        source_kind="web",
    )
    assert src.fetched_at == datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


def test_source_invalid_source_kind_raises() -> None:
    with pytest.raises(ValidationError):
        Source(
            url="https://e.com",
            title="t",
            cleaned_text="body",
            fetched_at=datetime.now(UTC),
            source_kind="bogus",  # type: ignore[arg-type]
        )


def test_source_metadata_default_is_independent_dict() -> None:
    fetched = datetime.now(UTC)
    a = Source(url="https://a", title="a", cleaned_text="a", fetched_at=fetched, source_kind="web")
    b = Source(url="https://b", title="b", cleaned_text="b", fetched_at=fetched, source_kind="web")
    a.metadata["k"] = "v"
    assert b.metadata == {}
    assert a.metadata is not b.metadata


def test_source_round_trip_json() -> None:
    fetched = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    src = Source(
        url="https://e.com",
        title="t",
        cleaned_text="body",
        raw_html="<p>body</p>",
        fetched_at=fetched,
        source_kind="github",
        archive_url=None,
        metadata={"repo": "x/y", "stars": 42},
    )
    parsed = Source.model_validate_json(src.model_dump_json())
    assert parsed == src


# ---------------------------------------------------------------------------
# extra="forbid"
# ---------------------------------------------------------------------------


def test_search_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SearchResult(
            url="https://e.com",
            title="t",
            snippet="s",
            source_kind="web",
            surprise="boom",  # type: ignore[call-arg]
        )


def test_source_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Source(
            url="https://e.com",
            title="t",
            cleaned_text="body",
            fetched_at=datetime.now(UTC),
            source_kind="web",
            surprise="boom",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Re-exports from `research_agent.tools`
# ---------------------------------------------------------------------------


def test_symbols_importable_from_tools_package() -> None:
    from research_agent import tools

    assert tools.SearchResult is SearchResult
    assert tools.Source is Source
    assert tools.SourceKind is SourceKind
    assert "SearchResult" in tools.__all__
    assert "Source" in tools.__all__
    assert "SourceKind" in tools.__all__
