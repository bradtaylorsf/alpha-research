"""Event logging, progress reporting, and optional OTEL telemetry (implementation guide §4)."""

from research_agent.observability.events import (
    Event,
    EventKind,
    EventLevel,
    emit,
    tail_events,
)

__all__ = [
    "Event",
    "EventKind",
    "EventLevel",
    "emit",
    "tail_events",
]
