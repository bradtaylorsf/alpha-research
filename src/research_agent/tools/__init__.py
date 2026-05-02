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


def _smoke_web_fetch(url: str) -> str:
    """Smoke wrapper for web_fetch: print word count, path, preview, archive URL.

    Returns a multi-line plain string so the CLI can print it verbatim.
    """
    from research_agent.tools import web_fetch

    async def _run() -> str:
        source = await web_fetch.fetch(url)
        if source is None:
            return f"web_fetch returned None for {url}"
        # Best-effort: give the archive task a brief window to finish so the
        # smoke output shows the URL when SPN is fast. We don't wait long;
        # callers in the live agent never block on archive at all.
        await asyncio.sleep(0)

        text = source.cleaned_text or ""
        word_count = len(text.split())
        preview = text[:200].replace("\n", " ")
        if len(text) > 200:
            preview += "…"
        fetched_via = source.metadata.get("fetched_via", "?")
        status = source.metadata.get("status_code")
        return (
            f"url: {source.url}\n"
            f"title: {source.title}\n"
            f"fetched_via: {fetched_via}\n"
            f"status_code: {status}\n"
            f"word_count: {word_count}\n"
            f"archive_url: {source.archive_url}\n"
            f"preview: {preview}"
        )

    return asyncio.run(_run())


# Registered tool callables, keyed by the name `research _smoke-tool` and the
# orchestrator dispatch by. The smoke verb checks against this dict so the
# dispatch surface is in place from day one.
TOOL_REGISTRY: dict[str, Callable[[str], object]] = {
    "web_search": _smoke_web_search,
    "web_fetch": _smoke_web_fetch,
}

__all__ = ["TOOL_REGISTRY", "SearchResult", "Source", "SourceKind"]
