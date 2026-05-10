"""Compatibility alias for the Smithsonian Open Access connector.

The direct task kind is ``si_search`` while the implementation module is
``smithsonian.py`` per issue #227. Some registry coherence tests and ad-hoc
imports resolve modules by task-kind prefix, so this module re-exports the
public connector surface without owning a separate registration.
"""

from __future__ import annotations

from research_agent.tools.smithsonian import KIND, fetch, reset_for_tests, search

__all__ = ["KIND", "fetch", "reset_for_tests", "search"]
