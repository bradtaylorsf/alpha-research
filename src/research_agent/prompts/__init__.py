"""Prompt registry — Markdown prompts loaded from disk (implementation guide §16).

Prompt files live next to this module as ``<name>.md`` with YAML frontmatter.
Use :func:`load_prompt` to render with ``{{var}}`` substitution and
:func:`load_prompt_meta` for the parsed :class:`Prompt` (version, hash, etc.).
"""

from research_agent.prompts.loader import (
    ModelTier,
    Prompt,
    PromptNotFoundError,
    PromptVariableMissing,
    clear_cache,
    load_prompt,
    load_prompt_meta,
)

__all__ = [
    "ModelTier",
    "Prompt",
    "PromptNotFoundError",
    "PromptVariableMissing",
    "clear_cache",
    "load_prompt",
    "load_prompt_meta",
]
