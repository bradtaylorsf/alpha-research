"""Tool/connector implementations: search, fetch, corpus, GitHub, arXiv, news, reddit, archive."""

from __future__ import annotations

from collections.abc import Callable

from research_agent.tools.models import SearchResult, Source, SourceKind

# Registered tool callables, keyed by the name `research _smoke-tool` and the
# orchestrator dispatch by. Empty until Phase 3 lands the actual connectors;
# the smoke verb checks against this dict so the dispatch surface is in place
# from day one.
TOOL_REGISTRY: dict[str, Callable[[str], object]] = {}

__all__ = ["TOOL_REGISTRY", "SearchResult", "Source", "SourceKind"]
