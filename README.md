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
