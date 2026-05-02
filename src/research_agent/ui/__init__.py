"""UI helpers — see implementation guide §11.

In v1 this is a thin pure-Python rendering layer used by the CLI verbs
(`research list/status/logs`). A future TUI or web UI can sit alongside
these helpers without refactoring the core.
"""

from research_agent.ui.render import (
    format_event_line,
    jobs_to_json,
    load_status_data,
    render_jobs_table,
    render_status_panel,
    tail_events_jsonl,
)

__all__ = [
    "format_event_line",
    "jobs_to_json",
    "load_status_data",
    "render_jobs_table",
    "render_status_panel",
    "tail_events_jsonl",
]
