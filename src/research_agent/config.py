"""Environment + config loading.

`.env` is the only config surface for secrets and operator overrides. This
module is the single place that knows which env vars exist, whether they
are required, and how to load them. `load_env()` is invoked once at CLI
entry, before Typer dispatches to any subcommand.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class EnvKey:
    name: str
    required: bool
    description: str
    default: str | None = None


EXPECTED_ENV_KEYS: tuple[EnvKey, ...] = (
    EnvKey(
        name="OPENROUTER_API_KEY",
        required=True,
        description="OpenRouter API key for cloud synthesis tier (Claude Opus / Haiku).",
    ),
    EnvKey(
        name="RESEARCH_USER_AGENT",
        required=False,
        description="Override default User-Agent sent by httpx and Playwright.",
        default="research-agent/0.1 (+local; contact unset)",
    ),
    EnvKey(
        name="RESEARCH_HEADFUL",
        required=False,
        description="Set to '1' to launch Playwright in headed mode for debugging.",
    ),
    EnvKey(
        name="RESEARCH_IGNORE_ROBOTS",
        required=False,
        description="Set to 1 to bypass robots.txt checks in web_fetch.",
    ),
    EnvKey(
        name="LMSTUDIO_BASE_URL",
        required=False,
        description="Override the default LM Studio base URL.",
        default="http://localhost:1234/v1",
    ),
)


_loaded = False


def _candidate_dirs(start: Path) -> list[Path]:
    dirs: list[Path] = []
    current = start.resolve()
    dirs.append(current)
    for parent in current.parents:
        dirs.append(parent)
    return dirs


def _find_env_files(start: Path) -> list[Path]:
    """Return the first `.env.local` and `.env` found walking up from `start`.

    `.env.local` is searched and loaded first (dev overrides), then `.env`.
    For each filename, only the first hit walking upward is used.
    """
    found: list[Path] = []
    for filename in (".env.local", ".env"):
        for directory in _candidate_dirs(start):
            candidate = directory / filename
            if candidate.is_file():
                found.append(candidate)
                break
    return found


def load_env(start: Path | None = None, *, force: bool = False) -> list[Path]:
    """Load `.env.local` then `.env` from cwd or nearest ancestor.

    Precedence (highest first): existing process env > `.env.local` > `.env`.
    Idempotent — repeated calls are no-ops unless `force=True`.

    Returns the list of files that were loaded (in load order).
    """
    global _loaded
    if _loaded and not force:
        return []

    start = start or Path.cwd()
    files = _find_env_files(start)
    for path in files:
        load_dotenv(path, override=False)

    _loaded = True
    return files


def reset_for_tests() -> None:
    """Reset module state so tests can re-invoke `load_env()` cleanly."""
    global _loaded
    _loaded = False


def get(name: str) -> str | None:
    """Return the env value, falling back to the declared default if any."""
    value = os.environ.get(name)
    if value is not None:
        return value
    for key in EXPECTED_ENV_KEYS:
        if key.name == name:
            return key.default
    return None
