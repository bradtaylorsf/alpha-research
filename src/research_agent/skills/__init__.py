"""Skills registry — Markdown skill files, lazy-loaded by the orchestrator.

Skills live next to this module under ``connectors/<name>.md`` and
``strategies/<name>.md`` with YAML frontmatter. Use :func:`list_skills`
for the planner-facing index and :func:`load_skill` /
:func:`load_strategies` to deep-load bodies at task-emit time.
"""

from research_agent.skills.loader import (
    Skill,
    SkillCategory,
    SkillParseError,
    clear_cache,
    list_skills,
    load_skill,
    load_strategies,
)

__all__ = [
    "Skill",
    "SkillCategory",
    "SkillParseError",
    "clear_cache",
    "list_skills",
    "load_skill",
    "load_strategies",
]
