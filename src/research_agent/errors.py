"""Public exception types for the programmatic API."""

from __future__ import annotations

from research_agent.llm.budgets import BudgetExceeded


class ResearchAgentError(Exception):
    """Base class for public API errors."""


class JobNotFound(ResearchAgentError):
    """Raised when a requested job id or folder cannot be found."""


class JobAlreadyRunning(ResearchAgentError):
    """Raised when an operation would start a second daemon for one job."""


class InvalidGoal(ResearchAgentError, ValueError):
    """Raised when a goal or related user input is invalid."""


__all__ = [
    "BudgetExceeded",
    "InvalidGoal",
    "JobAlreadyRunning",
    "JobNotFound",
    "ResearchAgentError",
]
