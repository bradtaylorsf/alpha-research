"""Skills registry — load Markdown skill files from ``src/research_agent/skills/``.

A *skill* is a Markdown file with a YAML frontmatter block that carries
per-connector or per-strategy guidance the planner uses for routing and the
orchestrator deep-loads at task-emit time. Skills live in two categories:

* ``skills/connectors/<name>.md`` — query-construction guidance, knob
  explanations, output-shape contracts for one connector module.
* ``skills/strategies/<name>.md`` — cross-cutting guidance multiple
  connectors share (e.g. modern-policy-era filtering).

Two-step loading mirrors :mod:`research_agent.prompts.loader`: the first
load parses frontmatter, hashes the file bytes, caches a :class:`Skill`,
and emits ``skills/skill_loaded``; subsequent loads hit the cache.

The frontmatter ``description`` is the *only* field the planner sees in
its index — it must be short, sortable, and the routing signal. The body
is what the orchestrator deep-loads at the moment a connector is about to
fire so the system prompt across all 18 connectors stays small.
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

SkillCategory = Literal["connectors", "strategies"]

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<fm>.*?)\n---\s*\n?(?P<body>.*)\Z",
    re.DOTALL,
)

_log = structlog.get_logger(__name__)


class SkillParseError(ValueError):
    """Raised when a skill file has missing or malformed frontmatter."""


class _Frontmatter(BaseModel):
    # Authors may add fields the loader doesn't know about (e.g. ``tags``,
    # ``examples``) without breaking the planner index — extras are ignored.
    model_config = ConfigDict(extra="ignore")

    description: str = Field(min_length=1)
    when_to_use: str | None = None
    when_not_to_use: str | None = None


class Skill(BaseModel):
    """Parsed skill with frontmatter, body markdown, and content hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    category: SkillCategory
    description: str
    when_to_use: str | None = None
    when_not_to_use: str | None = None
    body: str
    sha256: str
    path: Path = Field(...)


_CACHE: dict[tuple[str, str], Skill] = {}
_INDEX_CACHE: dict[str, list[Skill]] = {}
_INDEX_EMITTED: set[tuple[int, str]] = set()


def _skills_dir(category: SkillCategory) -> Path:
    """Return the on-disk directory holding ``skills/<category>/<name>.md``.

    Uses :mod:`importlib.resources` so it works for both editable installs
    (paths under ``src/``) and built wheels (paths under ``site-packages``).
    """
    return Path(str(files("research_agent.skills"))) / category


def _parse(category: SkillCategory, name: str, path: Path) -> Skill:
    raw_bytes = path.read_bytes()
    sha = hashlib.sha256(raw_bytes).hexdigest()
    raw_text = raw_bytes.decode("utf-8")

    match = _FRONTMATTER_RE.match(raw_text)
    if match is None:
        raise SkillParseError(
            f"skill {category}/{name!r} at {path} is missing a YAML frontmatter "
            "block (must start with '---' on its own line)"
        )
    try:
        fm_data = yaml.safe_load(match.group("fm")) or {}
    except yaml.YAMLError as exc:
        raise SkillParseError(
            f"skill {category}/{name!r} at {path} has invalid YAML frontmatter: {exc}"
        ) from exc
    if not isinstance(fm_data, dict):
        raise SkillParseError(
            f"skill {category}/{name!r} frontmatter must be a YAML mapping, "
            f"got {type(fm_data).__name__}"
        )
    try:
        meta = _Frontmatter(**fm_data)
    except ValidationError as exc:
        raise SkillParseError(
            f"skill {category}/{name!r} at {path} has invalid frontmatter: "
            f"{exc.errors(include_url=False)}"
        ) from exc

    return Skill(
        name=name,
        category=category,
        description=meta.description,
        when_to_use=meta.when_to_use,
        when_not_to_use=meta.when_not_to_use,
        body=match.group("body"),
        sha256=sha,
        path=path,
    )


def _load_index(category: SkillCategory) -> list[Skill]:
    cached = _INDEX_CACHE.get(category)
    if cached is not None:
        return cached
    base = _skills_dir(category)
    skills: list[Skill] = []
    try:
        paths = sorted(base.glob("*.md"))
    except (FileNotFoundError, OSError):
        paths = []
    for path in paths:
        skills.append(_parse(category, path.stem, path))
        _CACHE[(category, path.stem)] = skills[-1]
    _INDEX_CACHE[category] = skills
    return skills


def list_skills(category: SkillCategory, *, job: Job | None = None) -> list[dict[str, Any]]:
    """Return the planner-facing index for ``category``.

    Each entry is ``{name, description, when_to_use, when_not_to_use, path}``,
    sorted by ``name`` for deterministic output. Emits ``skills/index_loaded``
    once per (job, category) when ``job`` is supplied — subsequent calls
    within the same process hit the cache silently.
    """
    skills = _load_index(category)

    payload: dict[str, Any] = {
        "category": category,
        "count": len(skills),
        "total_chars": sum(len(s.body) for s in skills),
        "sha256s": [s.sha256 for s in skills],
    }
    if job is not None:
        key = (id(job), category)
        if key not in _INDEX_EMITTED:
            _INDEX_EMITTED.add(key)
            emit(job, "INFO", "skills", "index_loaded", payload)
    else:
        _log.info("skills_index_loaded", **payload)

    return [
        {
            "name": s.name,
            "description": s.description,
            "when_to_use": s.when_to_use,
            "when_not_to_use": s.when_not_to_use,
            "path": str(s.path),
        }
        for s in skills
    ]


def load_skill(category: SkillCategory, name: str, *, job: Job | None = None) -> str:
    """Return the body markdown for ``skills/<category>/<name>.md``.

    Returns ``""`` when the skill file is missing — a planner that names a
    not-yet-shipped connector/strategy must not break the loop. On a hit,
    emits ``skills/skill_loaded`` with the body's sha256, mirroring the
    ``prompt_loaded`` event shape.
    """
    cache_key = (category, name)
    cached = _CACHE.get(cache_key)
    if cached is None:
        path = _skills_dir(category) / f"{name}.md"
        if not path.exists():
            _log.debug("skill_missing", category=category, name=name, path=str(path))
            return ""
        cached = _parse(category, name, path)
        _CACHE[cache_key] = cached

    payload = {
        "category": cached.category,
        "name": cached.name,
        "sha256": cached.sha256,
        "total_chars": len(cached.body),
        "path": str(cached.path),
    }
    if job is not None:
        emit(job, "INFO", "skills", "skill_loaded", payload)
    else:
        _log.info("skills_skill_loaded", **payload)

    return cached.body


def load_strategies(names: list[str], *, job: Job | None = None) -> str:
    """Concatenate strategy bodies for ``names`` in caller order.

    Empty bodies (skill file missing) are skipped. The separator
    ``\\n\\n---\\n\\n`` keeps each strategy visually distinct when the
    result is concatenated into a connector context block.
    """
    bodies: list[str] = []
    for name in names:
        body = load_skill("strategies", name, job=job)
        if body:
            bodies.append(body)
    return "\n\n---\n\n".join(bodies)


def clear_cache() -> None:
    """Reset the in-process skill caches. For tests."""
    _CACHE.clear()
    _INDEX_CACHE.clear()
    _INDEX_EMITTED.clear()


__all__ = [
    "Skill",
    "SkillCategory",
    "SkillParseError",
    "clear_cache",
    "list_skills",
    "load_skill",
    "load_strategies",
]
