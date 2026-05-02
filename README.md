# research-agent

Autonomous CLI research agent. See `research-agent-implementation-guide.md` for the
full architecture and `CLAUDE.md` for the issue-driven workflow.

## Install

Requires Python 3.12+.

```bash
# editable install with dev extras
pip install -e ".[dev]"

# one-time browser bootstrap (binaries are not pip-installable)
playwright install chromium
```

## Configuration

`.env` is the only place runtime secrets and operator overrides live — no
`export` required. Copy the template and fill in what you need:

```bash
cp .env.example .env
```

Lookup order, highest precedence first:

1. Existing process env vars (CI, one-shot `OPENROUTER_API_KEY=... research ...`).
2. `./.env.local` (gitignored, dev-only overrides).
3. `./.env` (or the nearest ancestor walking up to repo root).

The full list of recognized keys lives in `src/research_agent/config.py`
(`EXPECTED_ENV_KEYS`). `.env.example` and `research doctor` both read from that
list, so there is no drift.

| Key | Required | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | yes | Cloud synthesis tier (Claude Opus / Haiku via OpenRouter). |
| `RESEARCH_USER_AGENT` | no | Override default UA sent by httpx + Playwright. |
| `RESEARCH_HEADFUL` | no | Set to `1` to launch Playwright in headed mode for debugging. |
| `RESEARCH_IGNORE_ROBOTS` | no | Set to `1` to bypass robots.txt checks in `web_fetch`. |
| `LMSTUDIO_BASE_URL` | no | Override the default `http://localhost:1234/v1`. |

## CLI

```bash
research --help
research --version          # print package version
research doctor             # environment readiness checks (Rich table)
research doctor --json      # same report as machine-readable JSON
```

`research doctor` exits non-zero if any required check fails — safe to wire
into CI as a pre-flight gate. Optional checks (LM Studio reachability, optional
env keys) never affect the exit code.

### Job verbs

```bash
# Register a new job (testing back door — the daemon is not yet wired up).
research start --skip-intake --goal "Investigate Widget Co" \
    [--budget-usd 5.0] [--time-cap 24] [--corpus path/to/notes]

research list                      # newest first; Rich table on a TTY, JSON otherwise
research list --json               # force JSON output
research list --status running     # filter by job status

research status <job-id>           # detailed Rich panel
research status <job-id> --watch   # refresh every 2 seconds

research view <job-id>             # open report.md in $EDITOR (or print on non-TTY)
research view <job-id> --report    # same as default
research view <job-id> --findings  # latest findings/NNNNNN.md
research view <job-id> --sources   # generated list of recorded sources

research logs <job-id>             # print existing events.jsonl entries
research logs <job-id> -f          # follow appended events
research logs <job-id> --level ERROR
```

### Config verbs

```bash
research config cache-clear        # wipe data/llm_cache.sqlite (LLM response cache)
```

The LLM response cache lives in its own SQLite file (`data/llm_cache.sqlite`),
keyed on `(provider, model, prompt, sampling-params, tool-defs)` with a 30-day
default TTL. The router opts in per call (`cache=True`) — deterministic
extractions opt in, exploratory synthesis opts out. `cache-clear` removes the
file (and its `-wal`/`-shm` sidecars) without touching the main index DB.

The interactive intake (`research start` without `--skip-intake`) lands in a
later issue; until then `--skip-intake --goal "..."` is the supported entry
point and the testing back door used throughout phases 1–4.

## Troubleshooting

Two hidden verbs (`_smoke-llm` and `_smoke-tool`) exist for operators and CI
to verify the LLM stack and the tool registry without spinning up a job.
They are intentionally hidden from `--help` (the leading underscore plus
`hidden=True` keeps them out of the standard surface) but are stable enough
to script against.

### `research _smoke-llm <tier> "<prompt>"`

Runs a single structured-output call against one tier in `config/models.yaml`
and prints the response, token counts, and (for cloud tiers) computed cost.
Exits non-zero on any failure with the underlying error written to stderr.

```bash
# Local tiers — require LM Studio at $LMSTUDIO_BASE_URL (default :1234).
research _smoke-llm fast "Say hello"
research _smoke-llm general "Say hello"
research _smoke-llm reasoner "Say hello"

# Cloud tiers — require OPENROUTER_API_KEY.
research _smoke-llm frontier "Say hello"
research _smoke-llm frontier_alt "Say hello"
research _smoke-llm frontier_speed "Say hello"
```

Two tiers behave specially:

- **`vision`**: skipped (exit 0) unless you pass `--image PATH`. The skip is
  reported as `output: skipped: vision: no image provided` so CI can tell
  apart a green skip from a green call.
- **`embeddings`**: bypasses Pydantic AI and hits the configured provider's
  `/embeddings` endpoint directly, reporting the vector dimension as
  `output: dim=<N>` instead of a chat completion.

### `research _smoke-tool <tool_name> <query>`

Looks up `<tool_name>` in the in-process `TOOL_REGISTRY` (Phase 3 connectors
register here) and invokes it with `<query>`. Exits with code 2 and a
`tool not registered` message on stderr when the name is unknown.

```bash
research _smoke-tool web_search "alpha research project"
research _smoke-tool web_fetch "https://example.com/article"
research _smoke-tool arxiv "transformer interpretability"
research _smoke-tool news "federal reserve"
```

`web_fetch` prints the resolved title, the path that served the fetch
(`httpx` vs `playwright`), HTTP status code, word count, the Wayback
archive URL (when Save Page Now completed in time), and the first 200
characters of cleaned text. Background Wayback archival is fire-and-forget,
so a missing `archive_url` is not a fetch failure.

`arxiv` prints the top 5 hits as `- <title>\n  <abs URL>\n  <abstract
preview>`. The connector honours arXiv's 3 s request-spacing recommendation
and runs the synchronous `arxiv` lib through `asyncio.to_thread`.

`news` aggregates every RSS feed and Playwright scrape recipe declared in
`config/sources.yaml` under the `news:` key, prints the total hit count,
a per-source breakdown grouped by `fetched_via` (`rss` vs `scrape`), and
the top 5 hits. RSS is preferred; sites without a public feed get a
per-source CSS selector recipe under `scrape:`. No paid news APIs.
