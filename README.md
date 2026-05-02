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
| `RESEARCH_DAEMON_PROGRESS` | no | Set to `0` to suppress the foreground Rich progress bar the daemon writes to stdout when run interactively. |

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
# Register a new job and spawn the background daemon.
research start --skip-intake --goal "Investigate Widget Co" \
    [--budget-usd 5.0] [--time-cap 24] [--corpus path/to/notes] [--disk-cap-gb 10]

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

research stop <job-id>             # graceful: drop STOP flag, daemon finishes current task
research stop <job-id> --kill      # hard: SIGTERM then SIGKILL after 10s, unlinks daemon.pid

research resume <job-id>           # respawn the daemon; restores from last checkpoint
research resume <job-id> --force   # resume even when job is completed/failed

research search "<query>"          # FTS5 over findings + sources (cross-job)
research search "<query>" --job <job-id>     # scope to one job
research search "<query>" --kind findings    # findings only (default: both)
research search "<query>" --kind sources     # sources only
research search "<query>" --json             # machine-readable list

research export <job-id> --zip               # bundle jobs/<id>/ into <job-id>.zip in cwd
research export <job-id> --md-bundle         # one markdown file: report + findings + sources
research export <job-id> --zip --out PATH    # write to PATH (file) or PATH/<job-id>.zip (dir)
research export <job-id> --md-bundle --include-history   # also inline report.history/
```

`research search` runs the user's query through SQLite FTS5 against
`findings_fts` (claim text) and `sources_fts` (source titles), ordered by
the BM25 score (lower is better). Snippet highlights come from the FTS5
`snippet()` function — the Rich table renders matched terms in bold yellow;
the `--json` payload preserves them as literal `[`/`]` markers. Source rows
join through `job_sources`, so the `--job` filter correctly excludes
sources fetched only by other jobs even when the underlying content is
shared. FTS5 syntax errors (e.g. unbalanced quotes) exit `1` with a clear
`FTS5 query error: ...` line on stderr.

`research export` bundles a job for sharing. `--zip` walks
`jobs/<job-id>/` and emits a single `ZIP_DEFLATED` archive whose entries
are rooted at `<job-id>/...` so unzipping reproduces the full folder.
`--md-bundle` concatenates the intake front matter, `report.md`, every
finding (ordered by id), and the source list (with `archive_url` links)
into one navigable markdown file. Exactly one mode flag is required —
omitting both, or passing both, exits `2`. `--out` accepts either a file
path or an existing directory (in which case `<job-id>.{zip,md}` is
appended); when omitted the file lands in the current working directory.
`--include-history` adds `report.history/` to either format. Both writes
use the project's atomic `*.tmp` + `os.replace` convention, so a crash
mid-export never leaves a half-written archive on disk.

### Config verbs

```bash
research config cache-clear        # wipe data/llm_cache.sqlite (LLM response cache)
```

The LLM response cache lives in its own SQLite file (`data/llm_cache.sqlite`),
keyed on `(provider, model, prompt, sampling-params, tool-defs)` with a 30-day
default TTL. The router opts in per call (`cache=True`) — deterministic
extractions opt in, exploratory synthesis opts out. `cache-clear` removes the
file (and its `-wal`/`-shm` sidecars) without touching the main index DB.

`research start` runs the interactive intake (or accepts `--skip-intake
--goal "..."` as a non-interactive testing back door), creates the job
folder + DB row, and then spawns a detached daemon via
`subprocess.Popen(start_new_session=True)`. Control returns immediately
with `Started job <id> (daemon pid <pid>). Tail logs with: research logs
<id> -f`. The PID is written atomically to `jobs/<id>/daemon.pid`; the
daemon's stdout/stderr are appended to `jobs/<id>/daemon.{out,err}.log`.
On clean shutdown (including SIGTERM/SIGINT) the daemon's atexit hook
removes `daemon.pid`. `daemon.is_daemon_alive(<id>)` checks liveness via
`kill -0` plus a `/proc/<pid>/cmdline` peek on Linux, so a recycled PID
won't false-positive.

`research stop --graceful` (the default) atomically writes a `STOP` flag
under `jobs/<id>/`; the daemon's between-task watcher picks it up, lets
the in-flight task finish, runs a final synthesis pass, then exits. The
command returns immediately with `Stop requested; daemon will finish
current task and synthesize.` `research stop --kill` SIGTERMs the PID,
escalates to SIGKILL after 10 s, and unlinks `daemon.pid` so a follow-up
`research resume` won't trip the alive-check. `research resume <id>`
refuses if a live daemon is already running (PID file present and the
process responds to `kill -0`), or if the job is in a terminal
`completed`/`failed` state — pass `--force` to override the latter.
Otherwise it spawns a fresh daemon, which restores from the last
checkpoint at startup.

## Troubleshooting

### Disk cap

Each job has a per-job disk cap (default `10` GB, override with
`--disk-cap-gb`). The daemon polls `jobs/<id>/` every 5 minutes; when
total on-disk usage exceeds the cap, it scores every linked source by
`5 * findings_usage + 1 * fts_title_hits − 0.1 * age_days` and prunes
the lowest-scored 10 % until usage drops below 90 % of the cap. A
single `WARN`/`warning` event marks the cap crossing; one
`INFO`/`source_pruned` event fires per file removed. Pruned ≠ banned:
the `sources` row stays in the cross-job index with `md_path = NULL`,
and a future fetch with the same sha256 transparently re-creates the
file under the current job (see `storage/sources.py`).

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

### Long-running soaks (macOS)

Multi-hour `research start` runs (Phase 5's 4-hour close-terminal soak,
Phase 6's 24-hour soak) need the laptop to stay awake. macOS will idle
sleep on default Energy Saver settings after ~10–30 minutes of no user
input, which freezes the daemon's loop and times out any in-flight
OpenRouter HTTP keepalives — long activity gaps in `events.jsonl` are
usually idle-sleep, not a bug in the daemon.

After `research start` returns, capture the daemon PID and tie a
`caffeinate` to it from a second terminal:

```bash
DAEMON_PID=$(cat jobs/<job-id>/daemon.pid)
caffeinate -i -w "$DAEMON_PID" &
```

- `-i` blocks **idle sleep** specifically (the display can still dim,
  which is fine — the soak doesn't need pixels).
- `-w <pid>` ties caffeinate's lifetime to the daemon. When the daemon
  exits — graceful stop, kill, or crash — caffeinate auto-exits with
  it, so there's no orphan process holding the system awake. No manual
  cleanup required.

On non-macOS hosts, use the equivalent for your OS (e.g. Linux:
`systemd-inhibit --what=idle`) and document the choice in the
postmortem so the next operator knows what worked.

The Phase 6 soak playbook walks through this end-to-end —
prerequisites, launch, sleep prevention, walk-away health checks,
graceful stop, and per-AC verification. See
`tests/integration/test_phase6_soak_24h.md`.

## End-to-end testing

The Phase 4 "done when" gate is exercised manually — too heavy and too
cost-bearing for CI. See `tests/integration/test_phase4_e2e.md` for the
playbook (canonical fixture goal, driver script, AC verification commands,
and a triage table for common failures).

The Phase 5 (4-hour daemon-lifecycle soak) and Phase 6 (24-hour real-goal
soak) gates have their own playbooks alongside it:
`tests/integration/test_phase5_lifecycle.md` and
`tests/integration/test_phase6_soak_24h.md`. Phase 6 also captures its
results in `tests/integration/soak_24h_postmortem.md`.
