"""Prompt registry — load Markdown prompts from ``src/research_agent/prompts/``.

Per §16 anti-pattern: prompts NEVER live in code. They sit alongside the
package as ``<name>.md`` files with a YAML frontmatter block carrying
``version``, ``model_tier``, and ``description``. The body is the prompt
template with optional ``{{var}}`` placeholders.

Loading is two-step: first call parses frontmatter, computes the file hash,
caches a :class:`Prompt`, and emits a ``prompt_loaded`` event (or logs).
Subsequent calls render from the cached template — the hash is computed
exactly once per ``name``.
"""

from __future__ import annotations

import hashlib
import re
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import structlog
import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_agent.observability.events import emit
from research_agent.storage.jobs import Job

ModelTier = Literal[
    "fast",
    "general",
    "reasoner",
    "frontier",
    "frontier_alt",
    "frontier_speed",
]

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<fm>.*?)\n---\s*\n?(?P<body>.*)\Z",
    re.DOTALL,
)
_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

_log = structlog.get_logger(__name__)


class PromptNotFoundError(FileNotFoundError):
    """Raised when ``load_prompt(name)`` cannot find ``<name>.md``."""


class PromptVariableMissing(KeyError):
    """Raised when a template references a ``{{var}}`` not provided by the caller."""


class _Frontmatter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    model_tier: ModelTier
    description: str


class Prompt(BaseModel):
    """Parsed prompt with frontmatter, raw template, and content hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    model_tier: ModelTier
    description: str
    template: str
    sha256: str
    path: Path = Field(...)


_CACHE: dict[str, Prompt] = {}


def _prompts_dir() -> Path:
    """Return the on-disk directory holding ``<name>.md`` files.

    Uses :mod:`importlib.resources` so it works for both editable installs
    (paths under ``src/``) and built wheels (paths under ``site-packages``).
    """
    return Path(str(files("research_agent.prompts")))


def _available_names() -> list[str]:
    try:
        return sorted(p.stem for p in _prompts_dir().glob("*.md"))
    except (FileNotFoundError, OSError):
        return []


def _parse(name: str, path: Path) -> Prompt:
    raw_bytes = path.read_bytes()
    sha = hashlib.sha256(raw_bytes).hexdigest()
    raw_text = raw_bytes.decode("utf-8")

    match = _FRONTMATTER_RE.match(raw_text)
    if match is None:
        raise ValueError(
            f"prompt {name!r} at {path} is missing a YAML frontmatter block "
            "(must start with '---' on its own line)"
        )
    fm_data = yaml.safe_load(match.group("fm")) or {}
    if not isinstance(fm_data, dict):
        raise ValueError(
            f"prompt {name!r} frontmatter must be a YAML mapping, got {type(fm_data).__name__}"
        )
    try:
        meta = _Frontmatter(**fm_data)
    except ValidationError as exc:
        raise ValueError(
            f"prompt {name!r} has invalid frontmatter: {exc.errors(include_url=False)}"
        ) from exc

    return Prompt(
        name=name,
        version=meta.version,
        model_tier=meta.model_tier,
        description=meta.description,
        template=match.group("body"),
        sha256=sha,
        path=path,
    )


def _get_or_load(name: str, *, job: Job | None = None) -> Prompt:
    cached = _CACHE.get(name)
    if cached is not None:
        return cached

    base = _prompts_dir()
    path = base / f"{name}.md"
    if not path.exists():
        available = _available_names()
        raise PromptNotFoundError(
            f"prompt {name!r} not found at {path}. "
            f"Available: {', '.join(available) if available else '(none)'}"
        )

    prompt = _parse(name, path)
    _CACHE[name] = prompt

    payload: dict[str, Any] = {
        "name": prompt.name,
        "version": prompt.version,
        "model_tier": prompt.model_tier,
        "sha256": prompt.sha256,
        "path": str(prompt.path),
    }
    if job is not None:
        emit(job, "INFO", "prompts", "prompt_loaded", payload)
    else:
        _log.info("prompt_loaded", **payload)

    return prompt


def load_prompt_meta(name: str, *, job: Job | None = None) -> Prompt:
    """Return the cached :class:`Prompt` for ``name`` without rendering."""
    return _get_or_load(name, job=job)


def load_prompt(name: str, *, job: Job | None = None, **vars: object) -> str:
    """Load ``<name>.md`` and render its ``{{var}}`` placeholders.

    On first load, parses frontmatter, computes the SHA-256 of the raw file,
    caches the :class:`Prompt`, and emits a ``prompt_loaded`` event (when
    ``job`` is supplied) or logs at INFO. Subsequent calls reuse the cache.

    Substitution is whitespace-tolerant: ``{{ goal }}`` and ``{{goal}}`` both
    resolve to the same key. Missing keys raise :class:`PromptVariableMissing`.
    Unused ``vars`` are silently ignored — the caller may pass a superset.
    """
    prompt = _get_or_load(name, job=job)

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in vars:
            raise PromptVariableMissing(
                f"prompt {name!r} references {{{{{key}}}}} but no value was "
                f"provided (got: {sorted(vars.keys()) or 'none'})"
            )
        return str(vars[key])

    return _VAR_RE.sub(_sub, prompt.template)


def clear_cache() -> None:
    """Reset the in-process prompt cache. For tests."""
    _CACHE.clear()


__all__ = [
    "ModelTier",
    "Prompt",
    "PromptNotFoundError",
    "PromptVariableMissing",
    "clear_cache",
    "load_prompt",
    "load_prompt_meta",
]
