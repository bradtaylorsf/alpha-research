"""Plan + Task Pydantic models.

The ``Plan`` is the versioned, structured document the planner agent emits
each iteration of the research loop. It captures the objective, the list of
subgoals (which drive completion), a template of tasks to enqueue, and the
expected number of loop iterations.

A ``TaskSpec`` is the typed shape that the planner emits and that
:mod:`research_agent.storage.tasks` persists into the ``tasks`` queue. It is
deliberately a *spec* — not the queue row itself — because the row also
carries lifecycle state (``status``, ``retry_count``, timestamps, the
parent task pointer) that the planner does not own.

All models use ``extra='forbid'`` so a typo in a planner prompt surfaces as
a validation error at the boundary rather than silently dropping a field.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TaskKind = Literal[
    "web_search",
    "web_fetch",
    "arxiv_search",
    "arxiv_fetch",
    "github_search",
    "github_fetch",
    "news_search",
    "reddit_search",
    "local_corpus_query",
    "extract_findings",
    "summarize_source",
    "synthesize",
    "critique",
]


class Subgoal(BaseModel):
    """A single subgoal within a Plan. ``done=True`` retires it from the loop."""

    model_config = ConfigDict(extra="forbid")

    id: int
    description: str = Field(min_length=1)
    done: bool = False


class TaskSpec(BaseModel):
    """A planner-emitted task to enqueue.

    ``depends_on`` references other ``TaskSpec`` entries by their *index*
    inside the same ``Plan.task_template`` list — this is intentionally
    decoupled from DB rowids so a plan can be validated before any rows
    exist.
    """

    model_config = ConfigDict(extra="forbid")

    kind: TaskKind
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    depends_on: list[int] = Field(default_factory=list)


class Plan(BaseModel):
    """A versioned planning document.

    ``is_complete()`` is the single source of truth for "should the loop
    stop?" — it returns True only when at least one subgoal exists and all
    are marked done. An empty ``subgoals`` list returns False so a planner
    that emits no subgoals never accidentally terminates the loop.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    objective: str = Field(min_length=1)
    subgoals: list[Subgoal]
    task_template: list[TaskSpec]
    expected_iterations: int = Field(ge=1)

    def is_complete(self) -> bool:
        if not self.subgoals:
            return False
        return all(sg.done for sg in self.subgoals)


__all__ = [
    "Plan",
    "Subgoal",
    "TaskKind",
    "TaskSpec",
]
