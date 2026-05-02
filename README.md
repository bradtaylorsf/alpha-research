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

More subcommands land here as later issues introduce them.
