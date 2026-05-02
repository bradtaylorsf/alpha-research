# Phase 5 daemon-lifecycle test (manual playbook)

This is **not** a pytest test. The Phase 5 "done when" gate exercises the
real daemon over a multi-hour wall-clock soak with the launching terminal
closed mid-run. It is too slow and too cost-bearing for CI; run it by
hand whenever Phase 5 needs a green check (e.g. before tagging the
milestone, or after a substantive change to `daemon.py`,
`storage/jobs.py`, or `orchestrator/loop.py`'s STOP-flag handling).

This is the daemon-lifecycle counterpart to Phase 4's
`test_phase4_e2e.md`, which exercised the plan/loop/synth cycle inline.
Phase 5 layers `research start` (daemon spawn), terminal close
(SIGHUP-survival), and `research stop --graceful` (STOP-flag plumbing)
on top of the same loop.

Closes the gate from issue #35.

## 1. Goal (canonical fixture)

Pick a public, synthesis-friendly target sized for a four-hour soak.
The default fixture:

> **Survey the evolution of agent orchestration frameworks 2022–2026
> (LangChain, LangGraph, AutoGen, CrewAI, Pydantic AI, OpenAI Agents
> SDK): timeline, design choices, and where each landed.**

Why this goal:

- All sources are public docs, blog posts, and release notes — connectors
  (web_fetch, web_search, archive, news) won't get rate-limited or hit
  paywalls, so the run exercises the loop instead of error paths.
- The scope is broad enough that the planner will keep emitting tasks
  for hours rather than wrapping in 20 minutes; narrow enough that
  every task is cite-friendly and the synth pass produces a readable
  report.
- Cite-heavy by nature — the report is easy to eyeball for citation
  correctness once the run completes.

## 2. Prerequisites

Run from the repo root.

```bash
# 2.1 LM Studio reachable on the configured port (default :1234) with
#     the local tiers from config/models.yaml loaded; OpenRouter key
#     loaded from .env. Both must be PASS, not WARN — a 4-hour run on
#     a half-wired stack is wasted time.
research doctor

# 2.2 OPENROUTER_API_KEY present (re-check explicitly; doctor's check
#     is necessary but not sufficient — keys can be empty strings).
grep -q '^OPENROUTER_API_KEY=.\+$' .env && echo "key present in .env"

# 2.3 No stale LLM cache. Cache hits return $0, which makes "did the
#     budget cap actually engage?" ambiguous over a long run.
research config cache-clear

# 2.4 Smoke verbs (cheap; ~$0.01 total).
research _smoke-llm general "Say hello"     # local
research _smoke-llm frontier "Say hello"    # cloud

# 2.5 No other research daemon is running. A second daemon racing on
#     the shared SQLite DB will produce confusing event interleaving.
pgrep -fl 'research_agent.daemon' || echo "none"
```

Stop here if any of these fail — fix the underlying wiring before
starting the timed run, otherwise you'll burn four hours on infra
issues instead of exercising the daemon.

Pick a budget appropriate for a 4-hour run on this fixture: **$20**
is a reasonable cap (frontier synth dominates cost; the loop runs
mostly on local tiers). Adjust upward only if you've watched the cost
graph during a prior run on the same goal.

## 3. Step-by-step run

The run has three phases: **launch**, **walk away**, **return**. The
whole point of Phase 5 is that step 2 doesn't require the operator
to keep a terminal open.

### 3.1 Launch (Terminal A — the "launching" terminal)

```bash
# Clear cache one more time; record T0 immediately after.
research config cache-clear

START_OUTPUT=$(research start --skip-intake \
    --goal "Survey the evolution of agent orchestration frameworks 2022–2026 (LangChain, LangGraph, AutoGen, CrewAI, Pydantic AI, OpenAI Agents SDK): timeline, design choices, and where each landed." \
    --time-cap 4 \
    --budget-usd 20)
echo "$START_OUTPUT"
JOB_ID=$(echo "$START_OUTPUT" | awk '/^Started job/ {print $3}')
echo "JOB_ID=$JOB_ID"

T0=$(date +%s)
echo "T0=$T0  ($(date -u -r "$T0" '+%Y-%m-%dT%H:%M:%SZ'))"

# Confirm the daemon was spawned and the PID file is on disk.
DAEMON_PID=$(cat "jobs/$JOB_ID/daemon.pid")
echo "DAEMON_PID=$DAEMON_PID"
ps -p "$DAEMON_PID" -o pid,ppid,stat,command
```

Record `JOB_ID`, `DAEMON_PID`, and `T0` somewhere outside Terminal A
(scratch file, sticky note, second terminal's scrollback) — closing
the terminal in step 3.2 will lose its history.

Optional sanity tail in a **second** terminal so you can watch the
first few minutes of activity before you walk away:

```bash
research logs "$JOB_ID" -f
```

### 3.2 Close Terminal A (the SIGHUP-survival check)

Literally close the window/tab, or `exit` the shell. Do **not** Ctrl-C
the daemon; the point of this step is that closing the controlling
terminal sends SIGHUP, and `start_new_session=True` in
`spawn_daemon` should mean the daemon doesn't see it.

From any other terminal, immediately verify the daemon survived the
HUP:

```bash
# Pull values from the snapshot you saved in §3.1.
JOB_ID=...      # paste
DAEMON_PID=...  # paste

# Daemon still alive?
ps -p "$DAEMON_PID" -o pid,ppid,stat,command

# Re-parented to launchd (macOS) / init (Linux). PPID == 1 confirms
# the daemon was detached from the original shell.
ps -o ppid= -p "$DAEMON_PID" | tr -d ' '
```

`ps -p` should print the daemon row; `ps -o ppid=` should print `1`.
If either fails, abort the soak — Phase 5's whole point is broken
and the rest of the playbook is moot.

### 3.3 Walk away (~4 hours)

Drop in occasionally from any terminal. Useful one-liners:

```bash
research status "$JOB_ID"
tail -n 20 "jobs/$JOB_ID/events.jsonl"  # last 20 raw event lines
research logs "$JOB_ID" -f              # live tail (Ctrl-C to exit)

# Quick "is it still healthy?" check.
ps -p "$DAEMON_PID" >/dev/null && echo alive || echo DEAD
sqlite3 data/index.sqlite \
    "SELECT status, cost_so_far_usd, last_activity_at FROM jobs WHERE id='$JOB_ID';"
```

Don't `stop` the job during this window — let the loop run on its own
schedule. If you absolutely must intervene mid-soak, note the time and
why; that goes in the triage notes for the run.

### 3.4 Return: graceful stop

```bash
research stop "$JOB_ID" --graceful
echo "stop exit code: $?"

# The CLI returns immediately after writing jobs/<id>/STOP. The daemon
# polls the flag every 2 s, finishes its current task, runs final
# synthesis, flips status to 'stopped'/'completed', and exits. Wait
# until is_daemon_alive flips to false (give it up to ~5 minutes —
# final synth is the slow step).
until ! ps -p "$DAEMON_PID" >/dev/null 2>&1; do
    echo "still running ($(date -u +%H:%M:%S))..."
    sleep 15
done
echo "daemon exited at $(date -u +%H:%M:%S)"

T1=$(date +%s)
WALL=$(( T1 - T0 ))
echo "wall-clock: ${WALL}s  ($(printf '%dh%02dm' $((WALL/3600)) $(((WALL%3600)/60))))"

# Confirm cleanup: PID file gone, STOP flag handled, status terminal.
test -e "jobs/$JOB_ID/daemon.pid" \
    && echo "WARN: daemon.pid still present" \
    || echo "PASS: daemon.pid removed"

sqlite3 data/index.sqlite \
    "SELECT status, cost_so_far_usd FROM jobs WHERE id='$JOB_ID';"
```

## 4. Acceptance verification

Each acceptance criterion from #35 maps to a concrete shell check
below. Run them in order; record PASS/FAIL with the actual values
captured.

```bash
JOB_ROOT="jobs/$JOB_ID"
EVENTS="$JOB_ROOT/events.jsonl"
```

### AC1 — daemon survived terminal close

You captured `ps -p $DAEMON_PID` and `ps -o ppid= -p $DAEMON_PID`
between §3.2 and §3.4. Reproduce the assertions from your notes:

```bash
# Should have been TRUE at the time of the §3.2 check.
echo "alive after HUP? expected: yes  (recorded in §3.2 notes)"
# Should have been '1' at the time of the §3.2 check (re-parented).
echo "PPID after HUP?  expected: 1   (recorded in §3.2 notes)"
```

Pass if both held immediately after closing Terminal A.

### AC2 — `research stop --graceful` succeeded

```bash
# Exit code captured in §3.4. Should be 0.
echo "stop exit code (recorded in §3.4): expected 0"

# Terminal status persisted to SQLite.
STATUS=$(sqlite3 data/index.sqlite \
    "SELECT status FROM jobs WHERE id='$JOB_ID';")
echo "status=$STATUS"
case "$STATUS" in
    stopped|completed) echo "PASS: terminal status=$STATUS" ;;
    *) echo "FAIL: status=$STATUS (expected stopped|completed)" ;;
esac

# PID file cleaned up by the daemon's atexit hook.
test ! -e "$JOB_ROOT/daemon.pid" \
    && echo "PASS: daemon.pid removed" \
    || echo "FAIL: daemon.pid still on disk"

# STOP flag was the trigger. Whether it persists post-exit is an
# implementation detail (resume clears it on the next start), but it
# *was* written — confirm via the events log.
jq -r 'select(.payload.stage=="stop_flag" or .payload.checkpoint_kind=="stop_requested") | .ts' \
    "$EVENTS" | head -1
```

Pass if exit code == 0, status ∈ {stopped, completed}, and `daemon.pid`
is gone.

### AC3 — `report.md` non-empty + cites sources

```bash
test -s "$JOB_ROOT/report.md" && echo "PASS: report present + non-empty" \
    || echo "FAIL: report missing or empty"

# Inline [N] citations and trailing http(s) URLs (per the synthesizer
# prompt contract, same as Phase 4 §5/AC2).
grep -E '^\[[0-9]+\]|https?://' "$JOB_ROOT/report.md" | head -10

research view "$JOB_ID" --report | head -50
```

If `report.md` starts with `# Report (truncated)` the cap fired
mid-synth — see triage table.

### AC4 — `events.jsonl` shows continuous activity across the 4 hours

The acceptance bar is "events in each of the four hour-buckets and
no `task_done` gap > 15 min".

`events.jsonl` records `ts` as an integer Unix epoch (per
`Event.ts` in `observability/events.py`), so the bucketing below floors
each timestamp to the hour in epoch arithmetic and converts only at
display time.

```bash
# Hourly bucket count of *all* events: each hour from T0 onward should
# have a non-trivial number of events. Epoch-floor to the hour, render
# UTC label for human eyeballing.
jq -r '.ts' "$EVENTS" \
    | python3 -c '
import sys, datetime as dt, collections
buckets = collections.Counter()
for s in sys.stdin:
    s = s.strip()
    if not s: continue
    bucket = (int(s) // 3600) * 3600
    buckets[bucket] += 1
for ep, n in sorted(buckets.items()):
    label = dt.datetime.fromtimestamp(ep, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:00Z")
    print(f"{n:6d}  {label}")
'
```

Manually confirm at least four distinct hour-buckets (or three if the
graceful stop landed before the 4h boundary on the same hour) all
have non-zero counts.

```bash
# Largest gap between consecutive task_done timestamps, in seconds.
jq -r 'select(.kind=="task_done") | .ts' "$EVENTS" \
    | sort -n \
    | python3 -c '
import sys, datetime as dt
ts = [int(s.strip()) for s in sys.stdin if s.strip()]
if len(ts) < 2:
    print("FAIL: <2 task_done events"); sys.exit()
gaps = [ts[i+1]-ts[i] for i in range(len(ts)-1)]
mx = max(gaps); mxi = gaps.index(mx)
to_iso = lambda t: dt.datetime.fromtimestamp(t, tz=dt.timezone.utc).isoformat()
print(f"task_done gaps: n={len(gaps)} max={mx}s at index {mxi} ({to_iso(ts[mxi])} -> {to_iso(ts[mxi+1])})")
print("PASS" if mx <= 900 else "FAIL: gap > 15 min")
'
```

Pass if every hour-bucket from T0 to T1 has events and the max
`task_done`-to-`task_done` gap is ≤ 900 s. A single mid-run gap > 15
min is grounds to triage rather than green-light the milestone — see
the "long activity gap" row in §6.

### AC5 — cost stays within configured cap

```bash
# Top-level columns on the `jobs` table — `budget_cap_usd` is mirrored
# from intake at job-creation time (see storage/db.py SCHEMA_SQL), so a
# direct SELECT is enough; no json_extract gymnastics required.
sqlite3 data/index.sqlite \
    "SELECT id, status, cost_so_far_usd, budget_cap_usd AS cap
     FROM jobs WHERE id='$JOB_ID';"

# Cross-check what the panel renders.
research status "$JOB_ID" | grep -E 'Cost so far|Budget cap'
```

Pass if `cost_so_far_usd > 0` (a $0 total means cache pollution — see
§2.3) **and** `cost_so_far_usd <= cap`. A `BudgetExceeded` mid-synth
shows up as `# Report (truncated)` (see AC3 + triage); strictly under
cap is the green path.

### AC6 — no unexpected exceptions

(Inherited from Phase 4 — keep here so a Phase 5 run is a superset.)

```bash
jq -r 'select(.level=="ERROR") | .payload' "$EVENTS"
grep -E '^.*Traceback' "$EVENTS" || echo "PASS: no raw tracebacks in events.jsonl"
```

Tolerate `retries_exhausted: true` and any `RetriableError` family.
Anything else is a regression — file under the appropriate module.

## 5. Tear-down + clean re-run

Same shape as Phase 4. The job folder + DB row are kept by default
for inspection; nuke them when you want to start fresh.

```bash
rm -rf "jobs/$JOB_ID"
sqlite3 data/index.sqlite "DELETE FROM jobs WHERE id='$JOB_ID';"
research config cache-clear
```

Do **not** delete `data/index.sqlite` outright — other jobs and the
cross-job FTS index live there.

If the daemon is somehow still alive when you run tear-down (you
aborted the soak rather than stopping gracefully):

```bash
research stop "$JOB_ID" --kill   # SIGTERM → SIGKILL
# Verify dead before the rm above:
ps -p "$DAEMON_PID" 2>/dev/null && echo "STILL ALIVE — investigate"
```

## 6. Triage table

Phase-4-shape table extended with daemon-specific symptoms. Start
here before opening a new issue.

| Symptom | Diagnostic command | Most likely cause | File-under label |
|---|---|---|---|
| `research doctor` reports `lm_studio` FAIL/WARN | `curl -s "$LMSTUDIO_BASE_URL/models"` | LM Studio not running, wrong port, or no model loaded | `infra/local-llm` |
| Cloud calls fail with 429 RateLimitError | `jq 'select(.level=="ERROR")' "$EVENTS"` | OpenRouter rate-limit during burst; router retries on `fallback_model` | `llm/router` (only if `fallback_model` mis-wired) |
| All cloud calls cost $0 | `sqlite3 data/index.sqlite "SELECT COUNT(*) FROM llm_calls WHERE job_id='$JOB_ID' AND finish_reason='cache';"` | LLM cache wasn't cleared (§2.3); rerun after `research config cache-clear` | (operator error, no issue needed) |
| Report ≠ empty but starts with `# Report (truncated)` | `head -1 "$JOB_ROOT/report.md"` | Frontier + frontier_speed both hit `BudgetExceeded`; stub written | `synth/budget` |
| Daemon dead immediately after closing Terminal A | `ps -p "$DAEMON_PID"` (right after §3.2) | `start_new_session=True` regression — daemon inherits controlling tty and dies on SIGHUP | `daemon/spawn` |
| Daemon alive but PPID ≠ 1 after closing Terminal A | `ps -o ppid= -p "$DAEMON_PID"` | Detach failed — process group still owned by the dead shell; future signals will leak | `daemon/spawn` |
| `daemon.pid` present after `research stop --graceful` returned cleanly | `ls -l "$JOB_ROOT/daemon.pid"; ps -p "$DAEMON_PID"` | Daemon still running (final synth slow), or atexit hook never fired (crash). Re-check after another minute; if PID is dead but file present, atexit was bypassed (SIGKILL or `os._exit`) | `daemon/atexit` |
| `STOP` flag written but daemon kept emitting `task_done` events for > 30 s | `jq 'select(.kind=="task_done") | .ts' "$EVENTS" | tail -5` and compare to mtime of `jobs/$JOB_ID/STOP` | STOP-flag watcher not polling, or `_should_stop` check missing in the inner loop | `daemon/stop-flag` or `loop/stop-check` |
| Long activity gap mid-run (no `task_done` events for > 15 min) | `jq -r 'select(.kind=="task_done") | .ts' "$EVENTS" | sort` then eyeball | Connector stuck (web_fetch on a hung page), network blip, or a single very slow synth pass | `connectors/<name>` or `loop/perf` |
| Cost cap hit before 4h elapsed | `sqlite3 data/index.sqlite "SELECT cost_so_far_usd FROM jobs WHERE id='$JOB_ID';"` and `jq 'select(.payload.cap_hit==true)' "$EVENTS"` | Budget too low for the chosen goal, or planner is over-emitting frontier-synth tasks | re-run with higher `--budget-usd`, or file `planner/synth-budget` |
| Daemon orphaned (PPID == 1 but daemon process is in zombie state) | `ps -o pid,ppid,stat,command -p "$DAEMON_PID"` (look for `Z` in STAT) | Parent reaper missing — typically a launchd config issue, not a research-agent bug | (operator/system; not a project bug) |
| `research stop --graceful` returned 0 but status stuck on `running` | `sqlite3 data/index.sqlite "SELECT status FROM jobs WHERE id='$JOB_ID';"` after daemon exit | Final-status write raced or failed; check `daemon.err.log` for the `failed to write final status` line | `daemon/final-status` |
| `research resume "$JOB_ID"` immediately exits because STOP flag still present | `ls -l "$JOB_ROOT/STOP"` | Resume cleanup of the stale STOP flag regressed (see #34) | `cli/resume` |

If the triage table doesn't cover the symptom: open a new issue with
the job folder zipped (sans secrets), the failing AC's command output,
and the timestamps of T0/T1/the last healthy check.
