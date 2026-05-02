"""Tool/connector implementations: search, fetch, corpus, GitHub, arXiv, news, reddit, archive."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from research_agent.tools.models import SearchResult, Source, SourceKind


def _smoke_web_search(query: str) -> list[SearchResult]:
    """Smoke wrapper: run DDG then Google, return top-5 hits from each combined.

    The CLI smoke verb calls this synchronously and prints ``repr`` of the
    return value, so the output shows results from both engines per AC #14.
    """
    from research_agent.tools import web_search

    async def _run() -> list[SearchResult]:
        ddg = await web_search.search(query, max_results=5, engine="ddg")
        google = await web_search.search(query, max_results=5, engine="google")
        return ddg + google

    return asyncio.run(_run())


# Registered tool callables, keyed by the name `research _smoke-tool` and the
# orchestrator dispatch by. The smoke verb checks against this dict so the
# dispatch surface is in place from day one.
TOOL_REGISTRY: dict[str, Callable[[str], object]] = {
    "web_search": _smoke_web_search,
}

__all__ = ["TOOL_REGISTRY", "SearchResult", "Source", "SourceKind"]
