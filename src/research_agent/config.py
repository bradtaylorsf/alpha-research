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
        name="BRAVE_SEARCH_API_KEY",
        required=False,
        description="Brave Search API key. When set, web_search 'auto' picks Brave over DDG-Playwright.",
    ),
    EnvKey(
        name="RESEARCH_USER_AGENT",
        required=False,
        description=(
            "Override default User-Agent sent by httpx and Playwright."
            " Required for SEC EDGAR (must include a contact email);"
            " optional for everything else. The EDGAR smoke verb"
            " gracefully skips when unset so unrelated runs aren't blocked."
        ),
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
    EnvKey(
        name="RESEARCH_PDF_VLM_ESCALATION",
        required=False,
        description=(
            "Set to '1' to enable Opus 4.7 vision escalation for PDFs that fail"
            " every cheaper extraction layer. Off by default — costs real money."
        ),
    ),
    EnvKey(
        name="RESEARCH_OCR_VLM_ESCALATION",
        required=False,
        description=(
            "Set to '1' to enable Opus 4.7 vision escalation for image OCR when"
            " Tesseract and the local VLM both fail. Off by default — costs real"
            " money; emits an ocr_vlm_escalation WARN event when fired."
        ),
    ),
    EnvKey(
        name="YOUTUBE_API_KEY",
        required=False,
        description=(
            "YouTube Data API v3 key — enables youtube.search; planner falls back"
            " to SERP scraping (and ultimately web_search) when absent."
        ),
    ),
    EnvKey(
        name="RESEARCH_DAEMON_PROGRESS",
        required=False,
        description=(
            "Set to '0' to suppress the foreground Rich progress bar that the daemon"
            " writes to stdout when run interactively. The spawned-daemon path is"
            " unaffected (its stdout is a log file, so the bar stays dormant)."
        ),
    ),
    EnvKey(
        name="COURTLISTENER_API_TOKEN",
        required=False,
        description=(
            "CourtListener API token (free w/ signup). Required by"
            " tools/courtlistener.py — anonymous tier is rate-limited to the"
            " point of unusability."
        ),
    ),
    EnvKey(
        name="DATA_GOV_API_KEY",
        required=False,
        description=(
            "api.data.gov key used by tools/fec.py (OpenFEC),"
            " tools/congress.py, and tools/smithsonian.py. Authenticated"
            " tier varies by API; falls back to DEMO_KEY for FEC/Congress/"
            "Smithsonian smoke when unset. Free signup at"
            " https://api.data.gov/signup/."
        ),
    ),
    EnvKey(
        name="LDA_API_KEY",
        required=False,
        description=(
            "Senate Lobbying Disclosure Act API key (free, lda.senate.gov)."
            " Anonymous access works for tools/lda.py; setting a registered"
            " key raises rate limits. Sent as `Authorization: Token <key>`."
        ),
    ),
    EnvKey(
        name="OPENCORPORATES_API_KEY",
        required=False,
        description=(
            "OpenCorporates API token (?api_token=...). Required for any"
            " live OpenCorporates request — anonymous v0.4 access is now"
            " gated (returns HTTP 401). Without a key, the connector"
            " returns no results and smoke skips cleanly. Public-benefit"
            " access by emailing service desk; commercial pricing"
            " £2,250–£12,000/yr."
        ),
    ),
    EnvKey(
        name="OPENALEX_API_KEY",
        required=False,
        description=(
            "Free OpenAlex API key for tools/openalex.py. Optional for"
            " low-volume smoke/demos, recommended for regular use since the"
            " February 2026 free-key policy. Sent as `api_key=<key>`."
        ),
    ),
    EnvKey(
        name="TROVE_API_KEY",
        required=False,
        description=(
            "Trove API key for tools/trove.py (National Library of"
            " Australia). Keys expire after 12 months. Sent as X-API-KEY;"
            " connector stays metadata-only because NLA has revoked keys"
            " for default full-text downloading."
        ),
    ),
    EnvKey(
        name="NARA_API_KEY",
        required=False,
        description=(
            "National Archives Catalog OPA v2 API key for tools/nara.py."
            " Request by emailing Catalog_API@nara.gov; registration takes"
            " about 24h. Sent as x-api-key. Default limit is 10,000"
            " queries/month; connector skips live calls when unset."
        ),
    ),
    EnvKey(
        name="DPLA_API_KEY",
        required=False,
        description=(
            "Digital Public Library of America API key for tools/dpla.py."
            " Request with curl -X POST https://api.dp.la/v2/api_key/<your-email>;"
            " the emailed 32-character key is sent as api_key=<key>."
        ),
    ),
    EnvKey(
        name="SERPAPI_KEY",
        required=False,
        description=(
            "SERPAPI key for tools/scholar.py (Google Scholar engine, case law"
            " + academic). Plans start at $75/mo for 5k searches across all"
            " engines; per-query ≈ $0.015. Sign up at https://serpapi.com/."
        ),
    ),
    EnvKey(
        name="LINKEDIN_DATA_API_KEY",
        required=False,
        description=(
            "LinkedIn data-broker key for tools/linkedin.py (default broker:"
            " Proxycurl). Per-lookup billing ≈ $0.01–$0.05; gate fetches"
            " behind explicit planner tasks. Sign up at https://nubela.co/proxycurl/."
        ),
    ),
    EnvKey(
        name="LINKEDIN_BROKER",
        required=False,
        description=(
            "Broker recipe used by tools/linkedin.py. 'proxycurl' (default)"
            " or 'lix'. Switching to lix consults LIX_API_KEY instead of"
            " LINKEDIN_DATA_API_KEY."
        ),
        default="proxycurl",
    ),
    EnvKey(
        name="LIX_API_KEY",
        required=False,
        description=(
            "Lix data-broker key (https://lix-it.com/) consulted only when"
            " LINKEDIN_BROKER=lix. Similar per-lookup pricing to Proxycurl."
        ),
    ),
    EnvKey(
        name="RESEARCH_REDDIT_USER_AGENT",
        required=False,
        description=(
            "Override the User-Agent that tools/reddit.py sends. Reddit's"
            " anonymous JSON endpoint 403s the project's descriptive UA, so"
            " the connector defaults to a Chrome UA. Set this when you have"
            " a registered Reddit OAuth app or want a different override"
            " than RESEARCH_USER_AGENT (which is consulted next)."
        ),
    ),
    EnvKey(
        name="RESEARCH_MODELS_CONFIG",
        required=False,
        description=(
            "Path to the models routing YAML the daemon loads. Defaults to"
            " 'config/models.yaml' relative to cwd. Set this when running"
            " out-of-tree or pointing at a packaged config."
        ),
        default="config/models.yaml",
    ),
    EnvKey(
        name="RESEARCH_DB_PATH",
        required=False,
        description=(
            "Override the SQLite index path the daemon uses. Unset uses the"
            " repo default ('data/index.sqlite'). Useful for isolating runs"
            " under test or pointing at a writable disk."
        ),
    ),
    EnvKey(
        name="RESEARCH_JOBS_ROOT",
        required=False,
        description=(
            "Override the directory that holds per-job folders. Unset uses"
            " the repo default ('jobs/'). Useful for redirecting big runs"
            " onto a larger disk."
        ),
    ),
    EnvKey(
        name="SANCTIONS_DB_PATH",
        required=False,
        description=(
            "Override where tools/sanctions.py writes its SDN/EU index"
            " sqlite. Unset uses the module default under 'data/sanctions/'."
            " Useful when refreshing into a staging path before atomic swap."
        ),
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
