"""Uniform Pydantic shapes every connector returns.

Locks in the `SearchResult` / `Source` contract from §7 of the implementation
guide. Connectors live in sibling modules (`web.py`, `github.py`, …) and each
expose ``async def search(query, **kwargs) -> list[SearchResult]`` and, where
applicable, ``async def fetch(url, **kwargs) -> Source | None``. The planner
and orchestrator only ever see these two shapes — connector implementations
are interchangeable.

The ``source_kind`` literal is the closed set the planner branches on; adding
a new vertical requires extending it here so type-checkers flag callers that
forgot to update.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SourceKind = Literal[
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
    "nonprofits",
    "congress",
    "lda",
    "usaspending",
    "littlesis",
    "opencorporates",
    "sanctions",
    "sos",
    "licensing",
    "bbb",
    "calaccess",
    "scholar",
]


class SearchResult(BaseModel):
    """One hit from a connector's ``search()`` method.

    Lightweight by design — full content lives in :class:`Source` after a
    follow-up ``fetch()``. ``extras`` is an escape hatch for connector-specific
    metadata (e.g. arXiv ids, GitHub stars) that downstream re-rankers can
    optionally consume.
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    title: str
    snippet: str
    published_at: datetime | None = None
    source_kind: SourceKind
    score: float | None = None
    extras: dict[str, Any] = Field(default_factory=dict)


class Source(BaseModel):
    """A fetched, cleaned document — the unit of citation."""

    model_config = ConfigDict(extra="forbid")

    url: str
    title: str
    cleaned_text: str
    raw_html: str | None = None
    fetched_at: datetime
    source_kind: SourceKind
    archive_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


__all__ = ["SearchResult", "Source", "SourceKind"]
