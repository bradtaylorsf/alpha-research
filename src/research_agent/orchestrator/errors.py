"""Loop-control exceptions shared between the orchestrator and connectors.

Lives in its own module so connectors (which raise these) and the loop
(which catches them) can both import without producing an import cycle
through :mod:`research_agent.orchestrator.loop`.

A :class:`RetriableError` is the connector's signal that "this task could
work — just try again". The loop wraps the handler call in a tenacity
backoff (1s/2s/4s/8s/16s/30s/60s, 5 attempts) and only marks the task
``failed`` once the budget is exhausted.

A :class:`FatalError` is the connector's signal that "this task will never
work as currently shaped". The loop marks the task ``failed`` immediately
and continues with the next task — *not* the daemon — because v1's job
state machine treats one bad task as a recoverable event, not a job-killer.
"""

from __future__ import annotations


class RetriableError(Exception):
    """Connector-level error that the loop should retry with exponential backoff."""


class FatalError(Exception):
    """Connector-level error that should not be retried; mark task failed and continue."""


__all__ = ["FatalError", "RetriableError"]
