# research-agent

> 🚧 **Status: actively developed.** v1 baseline is shipped and validated
> end-to-end ($0 local-mode runs produce real, sourced reports). The
> [open issues](../../issues) are the roadmap — see
> [#107](../../issues/107) for the connector buildout epic. Expect rough
> edges; PRs and issue reports welcome.

Autonomous CLI research agent. Run `research start` against a goal and walk
away — the daemon plans, fetches, synthesizes, and cites until the goal is
met (or the budget / time cap fires).

This README is the entry point. It walks an operator from "fresh laptop" to
"24-hour soak running unattended". Deeper detail (architecture, tier
routing, connector design) lives in the three foundational research docs in
this repo:

- [`ai-agent-research-setup.md`](ai-agent-research-setup.md) — model
  routing, hardware sizing, LM Studio ergonomics.
- [`ai-agent-investigation-playbook.md`](ai-agent-investigation-playbook.md)
  — investigation patterns and source taxonomy.
- [`research-agent-implementation-guide.md`](research-agent-implementation-guide.md)
  — the locked-in v1 architecture (Pydantic AI, SQLite, per-job folder,
  Typer CLI, model tiers).

`CLAUDE.md` describes the issue-driven build loop.

## Install

Requires Python 3.12+.

```bash
# editable install with dev extras
pip install -e ".[dev]"

# one-time browser bootstrap (binaries are not pip-installable)
playwright install chromium

# create your local .env from the template
cp .env.example .env
```

`.env` is the only place runtime secrets and operator overrides live — no
`export` required. Lookup order, highest precedence first:

1. Existing process env vars (CI, one-shot `OPENROUTER_API_KEY=... research ...`).
2. `./.env.local` (gitignored, dev-only overrides).
3. `./.env` (or the nearest ancestor walking up to repo root).

The full list of recognized keys lives in `src/research_agent/config.py`
(`EXPECTED_ENV_KEYS`). `.env.example` and `research doctor` both read from
that list, so there is no drift.

| Key | Required | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | yes | Cloud synthesis tier (Claude Opus / Haiku via OpenRouter). |
| `BRAVE_SEARCH_API_KEY` | no | Brave Search API key (free tier ~2000 queries/month). When set, `web_search` engine `auto` picks Brave over the DDG-Playwright scraper. |
| `RESEARCH_USER_AGENT` | no | Override default UA sent by httpx + Playwright. |
| `RESEARCH_HEADFUL` | no | Set to `1` to launch Playwright in headed mode for debugging. |
| `RESEARCH_IGNORE_ROBOTS` | no | Set to `1` to bypass robots.txt checks in `web_fetch`. |
| `RESEARCH_PDF_VLM_ESCALATION` | no | Set to `1` to enable Opus 4.7 vision escalation for PDFs that fail every cheaper layer. Off by default — costs real money; emits a `pdf_vlm_escalation` WARN event when fired. |
| `RESEARCH_OCR_VLM_ESCALATION` | no | Set to `1` to enable Opus 4.7 vision escalation for image OCR when Tesseract and the local VLM both fail. Off by default — costs real money; emits an `ocr_vlm_escalation` WARN event when fired. |
| `LMSTUDIO_BASE_URL` | no | Override the default `http://localhost:1234/v1`. |
| `YOUTUBE_API_KEY` | no | YouTube Data API v3 key (free quota: 10,000 units/day). When set, `tools/youtube.py:search` uses the official API; absent, it falls back to scraping the public results page via Playwright. |
| `RESEARCH_DAEMON_PROGRESS` | no | Set to `0` to suppress the foreground Rich progress bar the daemon writes to stdout when run interactively. |
| `COURTLISTENER_API_TOKEN` | no | CourtListener API token (free w/ signup) — required by `tools/courtlistener.py`. Authenticated tier is 5,000 req/hr; anonymous traffic is throttled to the point of unusability. |
| `DATA_GOV_API_KEY` | no | api.data.gov key (free w/ signup at <https://api.data.gov/signup/>) — used by `tools/fec.py` (OpenFEC). Authenticated tier is 1,000 req/hr; falls back to `DEMO_KEY` (~40 req/hr per IP) when unset. |
| `LDA_API_KEY` | no | Senate Lobbying Disclosure Act API key (free, optional, register at <https://lda.senate.gov/api/register/>) — used by `tools/lda.py`. Anonymous works for low-volume; authenticated raises rate limits. Sent via `Authorization: Token <key>`. |

## LM Studio

Local tiers run through [LM Studio](https://lmstudio.ai/) at
`http://localhost:1234/v1`. The exact model identifiers the router maps to
each tier live in [`config/models.yaml`](config/models.yaml); never
hardcode model names elsewhere — pick a tier.

**Models to download** (LM Studio UI → Discover → search by exact ID):

| Tier | Model ID | Purpose |
|---|---|---|
| `fast` | `qwen3-4b-instruct-q4_k_m` | Classification, dedup, language detection. |
| `general` | `qwen3-32b-instruct-q6_k` | Worker default — query rewriting, extraction, summarization. |
| `reasoner` | `deepseek-r1-distill-32b-q6_k` | Hypothesis ranking, contradiction detection. |
| `vision` | `qwen3-vl-8b-instruct` | PDF page screenshots, chart reading. |
| `embeddings` | `qwen3-embedding-4b` | Semantic search across findings + sources. |

After downloading, start the LM Studio local server (Developer tab →
Server → Start). The default port is `1234` and the OpenAI-compatible
endpoint mounts at `/v1`. Override with `LMSTUDIO_BASE_URL` if you've
moved it (e.g. `http://192.168.1.10:1234/v1` for a workstation across the
LAN).

`embeddings` intentionally has no cloud fallback — a stall surfaces as a
hard error rather than silently rerouting to a chat model. Keep
`qwen3-embedding-4b` loaded any time you plan to use `research search` or
the daemon's hybrid retrieval.

## OpenRouter

Cloud tiers go through [OpenRouter](https://openrouter.ai/). Create a key
(Dashboard → Keys → Create Key) and paste it into `.env`:

```bash
OPENROUTER_API_KEY=sk-or-v1-...
```

The key drives three tiers:

| Tier | Model | When it fires |
|---|---|---|
| `frontier` | `anthropic/claude-opus-4-7` | Major synthesis, critique, final report, planner rewrites. |
| `frontier_alt` | `moonshotai/kimi-k2-1t` | Critique pass — diverse second opinion. |
| `frontier_speed` | `anthropic/claude-haiku-4-5` | Fast cloud calls when local isn't enough but Opus is overkill; intake follow-ups; tier fallback. |

`research doctor` sanity-checks the key shape (`sk-or-` prefix) without
hitting the network. List prices live in `config/models.yaml` under
`pricing:` and feed the budget tracker (`src/research_agent/llm/budgets.py`).

## `research doctor`

```bash
research --help
research --version          # print package version
research doctor             # environment readiness checks (Rich table)
research doctor --json      # same report as machine-readable JSON
```

`research doctor` is the canonical wiring check. It verifies:

- Python ≥ 3.12 and the `.env` files that were loaded.
- Every key in `EXPECTED_ENV_KEYS` (presence + masked tail).
- `OPENROUTER_API_KEY` shape (`sk-or-` prefix).
- LM Studio reachability at `LMSTUDIO_BASE_URL` (optional check, never required).
- `data/` and `jobs/` exist and are writable.
- SQLite WAL mode is selectable.
- `config/models.yaml` parses.

Required failures exit non-zero (safe to wire into CI as a pre-flight
gate). Optional skips (LM Studio unreachable, optional env keys missing)
never affect the exit code.

## Walk-through

End-to-end: from clean repo to a finished report.

```bash
# 1. Verify the stack.
research doctor
# All required checks should be green. LM Studio "skip" is fine if you're
# only running cloud tiers; "fail" on OPENROUTER_API_KEY is not.

# 2. Start a job. The daemon runs detached; control returns immediately.
research start --skip-intake \
    --goal "Compare Pydantic AI, LangGraph, and CrewAI" \
    --budget-usd 5.00 \
    --time-cap 24 \
    --disk-cap-gb 10
# → Started job 2026-05-02-compare-pydantic-ai- (daemon pid 12345).
#   Tail logs with: research logs 2026-05-02-compare-pydantic-ai- -f

# 3. See what's running.
research list                          # newest first

# 4. Watch progress live.
JOB=$(research list --json | jq -r '.[0].id')
research status "$JOB" --watch         # Rich panel, refreshes every 2s

# 5. Tail events as they fire.
research logs "$JOB" -f

# 6. Read the report when synthesis lands (auto-rewrites as it iterates).
research view "$JOB" --report          # opens $EDITOR on a TTY

# 7. Stop early if you want — graceful by default.
research stop "$JOB"                   # daemon finishes current task, then synthesizes
research stop "$JOB" --kill            # hard SIGTERM/SIGKILL escalation

# 8. Resume from the last checkpoint after a crash or a clean stop.
research resume "$JOB"
```

For long unattended runs, see [macOS hygiene](#macos-hygiene) below.

## CLI surface

### Job verbs

```bash
research start --skip-intake --goal "<goal>" \
    [--budget-usd 5.0] [--time-cap 24] [--corpus path/to/notes] [--disk-cap-gb 10]

research list                      # newest first; Rich on a TTY, JSON otherwise
research list --json
research list --status running

research status <job-id>           # detailed Rich panel
research status <job-id> --watch   # refresh every 2s

research view <job-id>             # report.md in $EDITOR (or stdout off-TTY)
research view <job-id> --report
research view <job-id> --findings  # latest findings/NNNNNN.md
research view <job-id> --sources

research logs <job-id>             # print existing events.jsonl entries
research logs <job-id> -f          # follow appended events
research logs <job-id> --level ERROR

research stop <job-id>             # graceful: drop STOP flag
research stop <job-id> --kill      # SIGTERM, then SIGKILL after 10s

research resume <job-id>           # respawn daemon, restore from checkpoint
research resume <job-id> --force   # resume even when completed/failed

research search "<query>"          # hybrid FTS5 + semantic (cross-job)
research search "<query>" --fts-only
research search "<query>" --job <job-id>
research search "<query>" --kind findings
research search "<query>" --kind sources
research search "<query>" --json

research export <job-id> --zip
research export <job-id> --md-bundle
research export <job-id> --zip --out PATH
research export <job-id> --md-bundle --include-history
```

`research start` runs interactive intake (or accepts `--skip-intake --goal
"..."` as a non-interactive testing back door), creates the job folder +
DB row, and spawns a detached daemon via
`subprocess.Popen(start_new_session=True)`. The PID is written atomically
to `jobs/<id>/daemon.pid`; the daemon's stdout/stderr land in
`jobs/<id>/daemon.{out,err}.log`.

`research search` defaults to a hybrid pass: FTS5 on `findings_fts` /
`sources_fts` plus semantic cosine over `embeddings` blobs, deduped and
fused via reciprocal-rank fusion (k=60). Pass `--fts-only` for a
keyword-only escape hatch (useful when LM Studio is offline or for
debugging FTS5 syntax).

`research export` bundles a job for sharing. `--zip` walks
`jobs/<job-id>/` into a `ZIP_DEFLATED` archive; `--md-bundle` concatenates
intake, `report.md`, every finding, and the source list into one navigable
markdown file. Exactly one mode flag is required.

### Config verbs

```bash
research config cache-clear        # wipe data/llm_cache.sqlite
```

The LLM response cache lives in its own SQLite file, keyed on
`(provider, model, prompt, sampling-params, tool-defs)` with a 30-day
default TTL. The router opts in per call (`cache=True`) — deterministic
extractions opt in, exploratory synthesis opts out.

### Hidden smoke verbs

`_smoke-llm` and `_smoke-tool` are operator/CI helpers, hidden from
`--help` but stable enough to script against. See
[Troubleshooting](#troubleshooting) for usage.

## Costs

Local LM Studio inference is free at the wallet (the cost is the GPU/CPU
time on your laptop). All dollar spend goes through OpenRouter via the
`frontier`, `frontier_alt`, and `frontier_speed` tiers.

### Realistic per-run dollar ranges

The default cap on `research start` is `--budget-usd 5.00`. Typical
spend for a single run, depending on goal scope and aggressiveness:

| Run shape | Typical spend | Notes |
|---|---|---|
| Quick recon (≤ 1 hr, ~50 tasks) | $0.10 – $1.00 | Mostly local; one or two cloud syntheses. |
| Half-day investigation (~4 hr) | $1 – $5 | Several synth + critique passes; cap defaults handle this. |
| 24-hour soak (Phase 6 fixture) | $5 – $25 | Set `--budget-usd 25.00` for the full soak per `tests/integration/test_phase6_soak_24h.md`. |

The exact ratio depends on how often the planner triggers cloud calls,
which models actually serve them (Opus is ~25× the price of Haiku per
output token), and whether the LLM cache returns hits.

### What triggers cloud calls

In rough order of frequency:

- **Synthesis passes** — `frontier` for major checkpoints, `frontier_speed`
  as a budget-aware fallback if `frontier` would tip over the cap.
- **Critique** — `frontier_alt` is preferred so the synthesizer and critic
  disagree productively.
- **Adaptive intake follow-ups** — `frontier_speed` for short clarifying
  turns during interactive intake.
- **Local-tier fallbacks** — when an LM Studio tier times out or returns a
  `RateLimitError`, the router routes to the tier's `fallback_tier`. This
  is the only path where a "local-looking" task quietly costs money;
  watch for `tier_fallback` events in `events.jsonl` if your spend looks
  high.

### How the budget cap behaves

The cap is enforced in `src/research_agent/llm/budgets.py` at the
OpenRouter wrapper — every cloud call passes through it; no direct
OpenRouter clients exist elsewhere.

- **Soft warning at 90 %.** A single `WARNING` log fires the first time
  spend crosses 90 % of the cap. Use it as your "wrap up" signal if
  watching live.
- **Hard stop at 100 %.** `BudgetTracker.precheck()` raises
  `BudgetExceeded` before the next cloud call ships. The loop catches it,
  emits `cap_hit`, and triggers a **final-pass synthesis** on the cheaper
  `frontier_speed` tier so the user gets a report. If even
  `frontier_speed` would blow the cap, a template stub is rendered from
  on-disk findings without any LLM call (per issue #39).
- **State survives restarts.** `BudgetTracker` re-hydrates `spent` from
  `jobs.cost_so_far_usd` at construction, so a daemon restart picks up
  the same running total.
- **Local tiers are free.** `cost_usd = 0.0` for local rows in
  `llm_calls`; only OpenRouter tiers are priced. Pricing is read from
  the `pricing:` block in `config/models.yaml` (manually maintained).

## Directory layout

### Per-job folder (`jobs/<job-id>/`)

Every job is a self-contained folder. The cross-job DB only mirrors
metadata for fast queries — the folder is the source of truth.

```
jobs/<job-id>/
├── job.json              # canonical metadata (id, goal, status, timestamps)
├── intake.json           # frozen intake answers
├── goal.md               # human-readable goal + scope
├── plan/                 # planner state (versioned)
├── findings/             # findings/NNNNNN.md (zero-padded, monotonic)
├── sources/              # symlinks/copies of canonical source markdown
├── synthesis/            # synthesis/NNNN.md (versioned)
├── critique/             # critique/NNNN.md (versioned)
├── report.md             # current report (rotated to report.history/ on rewrite)
├── report.history/       # archived prior reports
├── events.jsonl          # append-only event log
├── daemon.pid            # written on spawn, removed on clean exit
├── daemon.out.log        # daemon stdout
├── daemon.err.log        # daemon stderr
└── STOP                  # presence signals graceful stop request
```

Job IDs are deterministic: `YYYY-MM-DD-<slug>` derived from the intake
goal. All on-disk writes go through atomic `*.tmp` + `os.replace` so a
crashed process never leaves half-written sidecars.

### Cross-job state (`data/`)

```
data/
├── index.sqlite          # WAL-mode; jobs, findings, sources, llm_calls, FTS5, embeddings
├── index.sqlite-wal
├── index.sqlite-shm
└── llm_cache.sqlite      # LLM response cache (separate file for safe wipe)
```

`research config cache-clear` wipes `llm_cache.sqlite` (and its `-wal`/
`-shm` sidecars) without touching `index.sqlite`.

### Gitignored regenerable dirs

`jobs/`, `runs/`, `data/`, `logs/`, `sessions/`, `.alpha-loop/`, `.venv/`.
Lockfiles (`uv.lock`) are committed.

## macOS hygiene

A 24-hour soak on a laptop has three failure modes that aren't bugs in
the daemon: idle sleep, OS auto-reboot, and you closing the lid in the
wrong way. Address them once, up front.

### Prevent idle sleep — `caffeinate -i -w <pid>`

After `research start` returns, capture the daemon PID and tie a
`caffeinate` to it from a second terminal:

```bash
DAEMON_PID=$(cat jobs/<job-id>/daemon.pid)
caffeinate -i -w "$DAEMON_PID" &
```

- `-i` blocks **idle sleep** specifically (the display can still dim,
  which is fine — the soak doesn't need pixels).
- `-w <pid>` ties caffeinate's lifetime to the daemon. When the daemon
  exits — graceful stop, kill, or crash — caffeinate auto-exits with it,
  so there's no orphan process holding the system awake.

Long activity gaps in `events.jsonl` are usually idle-sleep, not a bug
in the daemon. On non-macOS hosts, use the equivalent for your OS (e.g.
Linux `systemd-inhibit --what=idle`).

### Disable auto-reboot for system updates

macOS will silently install and reboot for security updates by default.
A reboot mid-soak loses the daemon and leaves a stale PID file behind.
Either:

- **GUI:** System Settings → General → Software Update → Automatic
  Updates → toggle off "Install macOS updates" and "Install Security
  Responses and system files".
- **CLI:**
  ```bash
  sudo softwareupdate --schedule off
  ```

Re-enable after the soak finishes if you want the OS to keep itself
patched.

### Optional `launchd` plist for auto-resume on boot

If a reboot does happen (power loss, manual restart), you can have the
most recent running job auto-resume. Drop a launch agent at
`~/Library/LaunchAgents/com.alpha.research.resume.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.alpha.research.resume</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>cd /path/to/alpha-research &amp;&amp; \
            JOB=$(./.venv/bin/research list --json --status running 2>/dev/null \
                  | /usr/bin/python3 -c 'import json,sys; jobs=json.load(sys.stdin); print(jobs[0]["id"]) if jobs else None') &amp;&amp; \
            [ -n "$JOB" ] &amp;&amp; ./.venv/bin/research resume "$JOB"</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/research-resume.out.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/research-resume.err.log</string>
</dict>
</plist>
```

Load it once: `launchctl load ~/Library/LaunchAgents/com.alpha.research.resume.plist`.
Edit `/path/to/alpha-research` to your checkout. Inspect
`/tmp/research-resume.{out,err}.log` if a boot doesn't pick up the job
you expected.

## Troubleshooting

### `research doctor` failures

| Failure | What to do |
|---|---|
| `python: fail` | Install Python 3.12+ (`brew install python@3.12`) and rebuild the venv. |
| `env:OPENROUTER_API_KEY: missing (required)` | Add the key to `.env`. Restart any open shell so the new value is picked up. |
| `openrouter_key_shape: fail` | Key doesn't start with `sk-or-` — copy it again from the OpenRouter dashboard. |
| `lm_studio: skip ... not reachable` | Optional, but local tiers won't work. Start LM Studio, click Server → Start, confirm port `1234`. |
| `writable_dirs: fail` | `data/` or `jobs/` permissions issue. `mkdir -p data jobs && chmod u+rwx data jobs`. |
| `sqlite_wal: fail` | Stdlib SQLite is too old or the temp dir is read-only. Re-run on a writable partition. |
| `models_yaml: fail` | `config/models.yaml` was edited and no longer parses. `git diff config/models.yaml` to inspect. |

When in doubt: `research doctor --json | jq .` for a structured view that
omits the Rich formatting.

### Smoke commands

Two hidden verbs verify the LLM stack and tool registry without spinning
up a job:

```bash
# Single structured-output call against one tier in config/models.yaml.
research _smoke-llm fast "Say hello"
research _smoke-llm general "Say hello"
research _smoke-llm reasoner "Say hello"
research _smoke-llm frontier "Say hello"
research _smoke-llm frontier_alt "Say hello"
research _smoke-llm frontier_speed "Say hello"

# Skipped (exit 0) without --image; reports `output: skipped: vision: no image provided`.
research _smoke-llm vision "Describe this" --image path/to/page.png

# Bypasses Pydantic AI; hits /embeddings directly. Reports `output: dim=<N>`.
research _smoke-llm embeddings "vector me"

# Tool registry probes (Phase 3 connectors).
research _smoke-tool web_search "alpha research project"
research _smoke-tool web_fetch "https://example.com/article"
research _smoke-tool arxiv "transformer interpretability"
research _smoke-tool news "federal reserve"
```

`web_fetch` prints the resolved title, the path that served the fetch
(`httpx` vs `playwright`), HTTP status, word count, the Wayback archive
URL (when Save Page Now completed in time), and the first 200 characters
of cleaned text. A missing `archive_url` is not a fetch failure —
Wayback archival is fire-and-forget.

### Where to read events

- `research logs <job-id> -f` — formatted tail of `events.jsonl` (level
  filter via `--level ERROR`).
- `jobs/<job-id>/events.jsonl` — raw append-only JSON, one event per
  line. `jq` is your friend (`jq 'select(.level=="ERROR")' events.jsonl`).
- `jobs/<job-id>/daemon.err.log` — daemon stderr (uncaught exceptions,
  process-level errors that didn't make it to `events.jsonl`).
- `data/index.sqlite` — cross-job mirror of events / findings / sources /
  llm_calls. Open with `sqlite3 data/index.sqlite` for ad-hoc queries.

### Disk cap

Each job has a per-job disk cap (default `10` GB, override with
`--disk-cap-gb`). The daemon polls `jobs/<id>/` every 5 minutes; when
total usage exceeds the cap, it scores every linked source by
`5 * findings_usage + 1 * fts_title_hits − 0.1 * age_days` and prunes
the lowest-scored 10 % until usage drops below 90 % of the cap. A single
`WARN`/`warning` event marks the cap crossing; one `INFO`/`source_pruned`
event fires per file removed. Pruned ≠ banned: the `sources` row stays
in the cross-job index with `md_path = NULL`, and a future fetch with
the same sha256 transparently re-creates the file under the current job.

## End-to-end testing

The Phase 4 "done when" gate is exercised manually — too heavy and too
cost-bearing for CI. See `tests/integration/test_phase4_e2e.md` for the
playbook (canonical fixture goal, driver script, AC verification
commands, and a triage table for common failures).

The Phase 5 (4-hour daemon-lifecycle soak) and Phase 6 (24-hour
real-goal soak) gates have their own playbooks alongside it:
`tests/integration/test_phase5_lifecycle.md` and
`tests/integration/test_phase6_soak_24h.md`. Phase 6 also captures its
results in `tests/integration/soak_24h_postmortem.template.md` (copy
the `.template.md` to a dated file for your specific run rather than
overwriting the template).
