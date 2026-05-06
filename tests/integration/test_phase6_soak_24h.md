# Phase 6 24-hour soak test (manual playbook)

This is **not** a pytest test. The Phase 6 "done when" gate exercises the
real daemon over a full unattended day on a real research goal. It is too
slow and too cost-bearing for CI; run it by hand whenever Phase 6 needs a
green check (e.g. before tagging the milestone, or after a substantive
change to anything in the planner/loop/synth/daemon hot path).

This is the long-haul counterpart to Phase 5's
`test_phase5_lifecycle.md` (4-hour close-terminal soak), which itself
layers on Phase 4's `test_phase4_e2e.md`. Phase 6 keeps everything Phase
5 verified and adds an idle-sleep-prevention step plus a structured
postmortem so surprises feed back into the milestone instead of
disappearing with the run.

Closes the gate from issue #40.

## 1. Goal (canonical fixture)

Pick a real but low-stakes target sized for a 24-hour soak. The
default fixture (per #40 Acceptance Criteria):

> **Comprehensive technical comparison of Pydantic AI vs LangGraph vs
> CrewAI.**

Why this goal:

- All sources are public docs, framework READMEs, blog posts, conference
  talks, and benchmark write-ups — connectors (`web_fetch`, `web_search`,
  `arxiv`, `news`) won't get rate-limited or hit paywalls, so the run
  exercises the loop instead of error paths.
- Cite-friendly by nature — every comparison claim traces to docs or a
  named benchmark, so the report is easy to eyeball for citation
  correctness.
- Scope is broad enough that the planner will keep emitting tasks for a
  full day without prematurely converging; narrow enough that the
  synthesis pass produces a coherent report.
- **Explicitly NOT Santos.** The Tim Santos investigation is reserved
  for the v1-complete validation in milestone 008. Phase 6 needs a
  known-answer non-controversial target so a bad result is unambiguous
  ("the report is wrong") rather than ambiguous ("the report is
  unflattering and we can't tell if it's right").

## 2. Prerequisites

Run from the repo root.

```bash
# 2.1 LM Studio reachable on the configured port (default :1234) with
#     the local tiers from config/models.yaml loaded; OpenRouter key
#     loaded from .env. Both must be PASS, not WARN — a 24-hour run on
#     a half-wired stack is a wasted day.
research doctor

# 2.2 OPENROUTER_API_KEY present (re-check explicitly; doctor's check
#     is necessary but not sufficient — keys can be empty strings).
grep -q '^OPENROUTER_API_KEY=.\+$' .env && echo "key present in .env"

# 2.3 No stale LLM cache. Cache hits return $0, which makes "did the
#     budget cap actually engage?" ambiguous over a long run, and an
#     accidentally-cached prior run on the same goal would short-circuit
#     most of the soak.
research config cache-clear

# 2.4 Smoke verbs (cheap; ~$0.01 total).
research _smoke-llm general "Say hello"     # local
research _smoke-llm frontier "Say hello"    # cloud
research _smoke-tool web_search "pydantic ai vs langgraph"

# 2.5 No other research daemon is running. A second daemon racing on
#     the shared SQLite DB will produce confusing event interleaving.
pgrep -fl 'research_agent.daemon' || echo "none"

# 2.6 Free disk on the volume holding jobs/. The default per-job
#     disk cap is 10 GB; the soak should comfortably fit, but a nearly
#     full disk will trip the cap-driven pruning path and conflate
#     "expected pruning" with "operator under-provisioned".
df -h .
```

Stop here if any of these fail — fix the underlying wiring before
starting the timed run. A 24-hour soak that dies in the first 20 minutes
on infra issues is the most expensive way to find a wiring bug.

Caps for a 24h run on this fixture (per #40):

- `--time-cap 24` (hours)
- `--budget-usd 25`
- `aggressiveness=balanced`

## 3. Step-by-step run

The run has three phases: **launch**, **walk away**, **return**. Phase 6
adds an explicit idle-sleep-prevention step between launch and walk
away so a sleeping Mac doesn't stall the daemon mid-soak.

### 3.1 Launch (Terminal A — the "launching" terminal)

`aggressiveness=balanced` is set by the interactive intake (default of
the three-choice picker in `intake.py`). The `--skip-intake` testing
back door does **not** pass aggressiveness through — `cli.py`'s
`intake_data` dict only carries `goal/domain/time_cap_hours/budget_cap_usd/disk_cap_gb`,
and `Job.create` stores whatever it's given (`storage/jobs.py` line ~195
forwards `intake.get("aggressiveness")`, which is `None` in the
skip-intake path).

For Phase 6, run **interactive intake** so the `balanced` tag is
captured in `intake.json` and the `jobs.aggressiveness` column. The
intake's three-choice picker defaults to `balanced`, so accept the
default at that prompt.

```bash
# Clear cache one more time; record T0 immediately after.
research config cache-clear

# Interactive intake. Goal / time-cap / budget-usd are pre-filled via
# flags so the only judgement call left for the operator is to accept
# the `balanced` default at the aggressiveness prompt.
START_OUTPUT=$(research start \
    --time-cap 24 \
    --budget-usd 25)
# (Answer the intake prompts: paste the canonical goal verbatim, accept
#  the `balanced` aggressiveness default, leave corpus blank.)

echo "$START_OUTPUT"
JOB_ID=$(echo "$START_OUTPUT" | awk '/^Started job/ {print $3}')
echo "JOB_ID=$JOB_ID"

T0=$(date +%s)
echo "T0=$T0  ($(date -u -r "$T0" '+%Y-%m-%dT%H:%M:%SZ'))"

# Confirm the daemon was spawned and the PID file is on disk.
DAEMON_PID=$(cat "jobs/$JOB_ID/daemon.pid")
echo "DAEMON_PID=$DAEMON_PID"
ps -p "$DAEMON_PID" -o pid,ppid,stat,command

# Confirm aggressiveness=balanced landed in the job row (catches the
# "operator skipped the prompt" case).
sqlite3 data/index.sqlite \
    "SELECT id, aggressiveness, time_cap_hours, budget_cap_usd
     FROM jobs WHERE id='$JOB_ID';"
```

If the operator must use `--skip-intake` (e.g. unattended re-run after
an aborted soak), the override path is to either (a) hand-edit
`jobs/<id>/intake.json` to add `"aggressiveness": "balanced"` and
re-run `sqlite3 data/index.sqlite "UPDATE jobs SET aggressiveness='balanced' WHERE id='$JOB_ID';"`,
or (b) extend `cli.py:start_command` to pass `--aggressiveness` through
to `intake_data`. Don't silently leave it `NULL` for a Phase 6 run —
the AC requires `balanced` and the postmortem evidence cell needs a
non-NULL value.

Record `JOB_ID`, `DAEMON_PID`, and `T0` somewhere outside Terminal A
(scratch file, sticky note, second terminal's scrollback) — closing the
terminal will lose its history.

### 3.2 Sleep prevention (Terminal B — the "babysitter" terminal)

**This step is the Phase-6-specific addition** and the difference
between a successful soak and a 4-hour wasted afternoon on a Mac that
went idle at 3 a.m.

```bash
# In a SECOND terminal (Terminal B), tied to the daemon's PID. -i
# blocks idle sleep; -w ties caffeinate's lifetime to the daemon PID
# so caffeinate auto-exits when the daemon stops (graceful or
# otherwise). Run it in the background with & so Terminal B is free
# for `research status` / `research logs -f` checks during the soak.
JOB_ID=...      # paste from §3.1
DAEMON_PID=...  # paste from §3.1
caffeinate -i -w "$DAEMON_PID" &
CAFFEINATE_PID=$!
echo "CAFFEINATE_PID=$CAFFEINATE_PID"
ps -p "$CAFFEINATE_PID" -o pid,ppid,command
```

Why each flag matters:

- `-i` — prevents *idle* sleep specifically (display can still dim).
  Without this, a Mac on default Energy Saver settings will sleep after
  ~10–30 minutes of no user input, freezing the daemon and timing out
  any in-flight OpenRouter HTTP keepalives.
- `-w <pid>` — caffeinate exits the moment the daemon dies. So at the
  end of the soak (graceful stop, kill, or crash), there's no orphan
  caffeinate left holding the system awake. No manual cleanup.

If `caffeinate` isn't installed (non-macOS, or `which caffeinate` is
empty), Phase 6 still runs but the operator must guarantee no idle
sleep some other way (Linux: `systemd-inhibit --what=idle`; Windows:
PowerToys Awake or `powercfg /requestsoverride`). Document the chosen
mechanism in §4 / postmortem evidence so a future run on a different
machine knows what worked.

### 3.3 Close Terminal A (the SIGHUP-survival check, inherited from Phase 5)

Literally close the window/tab, or `exit` the shell. Do **not** Ctrl-C
the daemon — the point of this step is that closing the controlling
terminal sends SIGHUP, and `start_new_session=True` in `spawn_daemon`
should mean the daemon doesn't see it.

```bash
# From Terminal B.
ps -p "$DAEMON_PID" -o pid,ppid,stat,command
ps -o ppid= -p "$DAEMON_PID" | tr -d ' '   # expect: 1
ps -p "$CAFFEINATE_PID" -o pid,ppid,command  # expect: still alive
```

If the daemon died on HUP, abort the soak — Phase 5's gate has
regressed and Phase 6's gate is moot until #35 is re-greened.

### 3.4 Walk away (~24 hours)

Drop in occasionally from any terminal. Useful one-liners (same set as
Phase 5):

```bash
research status "$JOB_ID"
tail -n 20 "jobs/$JOB_ID/events.jsonl"  # last 20 raw event lines
research logs "$JOB_ID" -f              # live tail (Ctrl-C to exit)

# Quick "is it still healthy?" check.
ps -p "$DAEMON_PID" >/dev/null && echo alive || echo DEAD
sqlite3 data/index.sqlite \
    "SELECT status, cost_so_far_usd, last_activity_at FROM jobs WHERE id='$JOB_ID';"
```

Don't `stop` the job during this window unless something is clearly
broken — let the loop run on its own schedule. If you absolutely must
intervene mid-soak, note the time and why; that goes in the postmortem
under "Surprises".

A 24h run will usually self-terminate at the time-cap before the
operator has to call it. If the cost cap fires first, the cost-cap
final-pass enforcement (#39) should still synthesize a usable report
before the daemon exits — that's not a failure, that's the cap working.

### 3.5 Return: graceful stop (only if the run hasn't self-terminated)

If `research status` shows the job already in a terminal state
(`completed`/`stopped`/`failed`), skip the `stop` command and jump to
the cleanup checks.

```bash
# Only if status is still 'running'.
research stop "$JOB_ID" --graceful
echo "stop exit code: $?"

# The daemon polls the STOP flag every 2 s, finishes its current task,
# runs final synthesis (which can be slow — synth on a 24h goal has
# the most context to work with), flips status to 'stopped'/'completed',
# and exits. Wait until the process is gone (give it up to ~10 minutes;
# longer than Phase 5 because final synth is summarizing more findings).
until ! ps -p "$DAEMON_PID" >/dev/null 2>&1; do
    echo "still running ($(date -u +%H:%M:%S))..."
    sleep 30
done
echo "daemon exited at $(date -u +%H:%M:%S)"

T1=$(date +%s)
WALL=$(( T1 - T0 ))
echo "wall-clock: ${WALL}s  ($(printf '%dh%02dm' $((WALL/3600)) $(((WALL%3600)/60))))"

# caffeinate should have auto-exited when the daemon died (-w flag).
ps -p "$CAFFEINATE_PID" >/dev/null 2>&1 \
    && echo "WARN: caffeinate still running (PID $CAFFEINATE_PID) — kill it manually" \
    || echo "PASS: caffeinate auto-exited"

# Confirm cleanup: PID file gone, status terminal.
test -e "jobs/$JOB_ID/daemon.pid" \
    && echo "WARN: daemon.pid still present" \
    || echo "PASS: daemon.pid removed"

sqlite3 data/index.sqlite \
    "SELECT status, cost_so_far_usd FROM jobs WHERE id='$JOB_ID';"
```

## 4. Acceptance verification

Each acceptance criterion from #40 maps to a concrete shell check
below. Run them in order; record PASS/FAIL with the actual values
captured. The same values feed the postmortem template
(`soak_24h_postmortem.template.md` — copy to a dated file for each
soak; the template stays unmodified in git).

```bash
JOB_ROOT="jobs/$JOB_ID"
EVENTS="$JOB_ROOT/events.jsonl"
```

### AC1 — goal selected and documented

```bash
# Goal text exactly matches the canonical fixture.
diff <(jq -r '.goal' "$JOB_ROOT/intake.json") \
     <(echo "Comprehensive technical comparison of Pydantic AI vs LangGraph vs CrewAI.")
echo "exit $?  (0 = match, non-zero = goal drifted from the fixture)"

# goal.md mirrors the intake (sanity-check that Job.create wrote both).
test -s "$JOB_ROOT/goal.md" && echo "PASS: goal.md present + non-empty" \
    || echo "FAIL: goal.md missing or empty"
```

If the operator deliberately ran a different goal (e.g. re-run after a
fixture update), record it verbatim in the postmortem — but the
default fixture is the one #40 anchors against.

### AC2 — caps applied (time 24h, budget $25, aggressiveness balanced)

```bash
sqlite3 data/index.sqlite \
    "SELECT id, time_cap_hours, budget_cap_usd, aggressiveness
     FROM jobs WHERE id='$JOB_ID';"
# Expected exact values: time_cap_hours=24, budget_cap_usd=25.0,
# aggressiveness='balanced'.
```

Pass if all three match. A NULL aggressiveness means the operator went
through `--skip-intake` without applying the override path documented
in §3.1 — fix per §3.1 before declaring AC2 PASS.

### AC3 — caffeinate ran for the full soak

`caffeinate` doesn't write its own logs, so this AC is operator-attested
plus an indirect check (no idle-sleep symptoms in the event stream).

```bash
# Operator attestation: caffeinate -i -w $DAEMON_PID was started in §3.2
# and was still alive (or had auto-exited because the daemon exited)
# at the time of §3.5.
echo "caffeinate -i -w $DAEMON_PID started at: <fill in postmortem>"
echo "caffeinate exit observed at:           <fill in postmortem>"

# Indirect check: an idle-sleep stall would show up as a long
# task_done gap with no obvious connector cause. Look for gaps > 30 min.
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
print("PASS" if mx <= 1800 else "WARN: gap > 30 min — investigate idle-sleep / connector hang")
'
```

Pass if operator attests caffeinate covered the run and no gap > 30
min is unexplained. A 30+ min gap with a known cause (e.g. OpenRouter
outage in the event stream) is not a caffeinate failure — note it in
the postmortem.

### AC4 — run completed unattended

The whole soak must complete without the operator stopping or
resuming the job.

```bash
# Terminal status persisted to SQLite.
STATUS=$(sqlite3 data/index.sqlite \
    "SELECT status FROM jobs WHERE id='$JOB_ID';")
echo "status=$STATUS"
case "$STATUS" in
    completed|stopped) echo "PASS: terminal status=$STATUS" ;;
    *)                 echo "FAIL: status=$STATUS (expected completed|stopped)" ;;
esac

# completion_reason should be one of the expected self-terminations,
# not 'user_stopped' (unless the operator notes why in the postmortem).
sqlite3 data/index.sqlite \
    "SELECT completion_reason FROM jobs WHERE id='$JOB_ID';"
# Expected: goal_complete | budget_cap | task_cap (the values written by
# daemon.run_daemon, validated against ALLOWED_COMPLETION_REASONS in
# storage/jobs.py). NB: time_cap is in the allow-list but is not yet
# enforced by the loop — a 24h soak self-terminates via task_cap (the
# 10k-task anti-runaway guard) or budget_cap, not a wall-clock cap.
# Anything else → record under 'Surprises' in postmortem.

# No mid-run resumes. The loop's `job_started` checkpoint (which fires
# at the top of run_loop) is the canonical "loop started" signal —
# emitted once per `research start` *and* once per `research resume`.
# It surfaces in events.jsonl as `kind=checkpoint` with
# `payload.checkpoint_kind=job_started`.
jq -r 'select(.kind=="checkpoint" and .payload.checkpoint_kind=="job_started") | .ts' \
    "$EVENTS" | wc -l
# Expected: 1 (single launch). >1 means at least one resume happened.
```

Pass if status ∈ {completed, stopped}, completion_reason is a
self-termination, and there's exactly one `job_started` checkpoint.

### AC5 — events.jsonl ERROR/WARN cluster scan

Per #40: "events.jsonl scanned for unexpected ERROR/WARN clusters; each
cluster filed as a follow-up issue or fixed in this milestone".

```bash
# Cluster ERROR/WARN events by stage (the most useful axis — a tight
# cluster on one stage usually indicates a real bug or fragile
# connector). Sort descending by count.
jq -r 'select(.level=="ERROR" or .level=="WARN") | .payload.stage // .payload.kind // "<unknown>"' \
    "$EVENTS" \
    | sort | uniq -c | sort -rn

# Top-line counts.
jq -r '.level' "$EVENTS" | sort | uniq -c | sort -rn

# Sample the actual messages for the top cluster (substitute STAGE).
# jq -r 'select((.level=="ERROR" or .level=="WARN") and (.payload.stage=="<STAGE>")) | .payload' "$EVENTS" | head -5
```

For each cluster with > 5 occurrences, the operator must do one of:

1. Fix it in this milestone (small, in-scope, e.g. a config typo).
2. File a follow-up issue with the cluster's symptom + count + sample
   payload. Link the new issue in the postmortem under "Surprises".

A clean run with zero ERROR-level events is great but not required —
the gate is "no *unexplained* clusters", not "zero noise".

### AC6 — final report coherent + cites sources

```bash
test -s "$JOB_ROOT/report.md" && echo "PASS: report present + non-empty" \
    || echo "FAIL: report missing or empty"

# Inline [N] citations and trailing http(s) URLs (per the synthesizer
# prompt contract, same as Phase 4 §5/AC2 and Phase 5 §AC3).
grep -E '^\[[0-9]+\]|https?://' "$JOB_ROOT/report.md" | wc -l
# Expected: dozens to hundreds for a 24h goal. Single-digit count
# means the synth tier didn't pull citations through — file as a
# synth/citations issue.

# Cross-check: report mentions all three frameworks by name.
for kw in "Pydantic AI" "LangGraph" "CrewAI"; do
    grep -c "$kw" "$JOB_ROOT/report.md" \
        | xargs -I{} echo "$kw: {} mentions"
done

# Eyeball the first 80 lines for coherence (executive summary +
# section structure should be visible).
research view "$JOB_ID" --report | head -80
```

If `report.md` starts with `# Report (truncated)` the cost cap fired
mid-synth — see triage table. The Phase 6 quality bar is "Phase 4
short-run report scaled up": coherent narrative, dense citations,
section structure, and no obvious hallucinated frameworks/versions.

### AC7 — cost stays under cap; total Opus spend recorded

```bash
sqlite3 data/index.sqlite \
    "SELECT id, status, cost_so_far_usd, budget_cap_usd AS cap
     FROM jobs WHERE id='$JOB_ID';"

# Per-model breakdown — Opus spend goes in the postmortem header.
sqlite3 -header -column data/index.sqlite \
    "SELECT model, COUNT(*) AS calls,
            ROUND(SUM(cost_usd), 4) AS total_cost_usd,
            ROUND(SUM(input_tokens)/1000.0, 1) AS k_in,
            ROUND(SUM(output_tokens)/1000.0, 1) AS k_out
     FROM llm_calls
     WHERE job_id='$JOB_ID'
     GROUP BY model
     ORDER BY total_cost_usd DESC;"

research status "$JOB_ID" | grep -E 'Cost so far|Budget cap'
```

Pass if `cost_so_far_usd > 0` (a $0 total means cache pollution — see
§2.3) **and** `cost_so_far_usd <= cap`. Record the exact Opus row from
the per-model breakdown in the postmortem.

### AC8 — postmortem written to tests/integration/soak_24h_postmortem.md

```bash
test -s tests/integration/soak_24h_postmortem.md \
    && echo "PASS: postmortem present + non-empty" \
    || echo "FAIL: postmortem missing or empty"

# All <…> placeholders should be replaced.
grep -nE '<JOB_ID>|<T0>|<T1>|<TOTAL_COST>|<OPUS_COST>|<WALL_CLOCK>|<…>|<...>' \
    tests/integration/soak_24h_postmortem.md \
    && echo "FAIL: placeholders remain in postmortem" \
    || echo "PASS: no placeholders remain"
```

The postmortem is committed alongside the run, so the next operator
(or a future Claude session) can diff against it to see what changed
between soaks.

## 5. Tear-down + clean re-run

Same shape as Phases 4 and 5. The job folder + DB row are kept by
default for inspection (the postmortem references them); only nuke when
you're ready to start fresh.

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
ps -p "$DAEMON_PID" 2>/dev/null && echo "STILL ALIVE — investigate"
# caffeinate -w should auto-exit, but double-check.
ps -p "$CAFFEINATE_PID" 2>/dev/null && kill "$CAFFEINATE_PID"
```

## 6. Triage table

Phase-5-shape table extended with 24h-specific symptoms. Start here
before opening a new issue.

| Symptom | Diagnostic command | Most likely cause | File-under label |
|---|---|---|---|
| `research doctor` reports `lm_studio` FAIL/WARN | `curl -s "$LMSTUDIO_BASE_URL/models"` | LM Studio not running, wrong port, or no model loaded | `infra/local-llm` |
| Cloud calls fail with 429 RateLimitError | `jq 'select(.level=="ERROR")' "$EVENTS"` | OpenRouter rate-limit during burst; router retries on `fallback_model` | `llm/router` (only if `fallback_model` mis-wired) |
| All cloud calls cost $0 | `sqlite3 data/index.sqlite "SELECT COUNT(*) FROM llm_calls WHERE job_id='$JOB_ID' AND finish_reason='cache';"` | LLM cache wasn't cleared (§2.3); rerun after `research config cache-clear` | (operator error, no issue needed) |
| Report ≠ empty but starts with `# Report (truncated)` | `head -1 "$JOB_ROOT/report.md"` | Frontier + frontier_speed both hit `BudgetExceeded`; final-pass enforcement (#39) wrote the stub | `synth/budget` (only if final-pass didn't trigger; see #39) |
| Daemon dead immediately after closing Terminal A | `ps -p "$DAEMON_PID"` (right after §3.3) | `start_new_session=True` regression — daemon inherits controlling tty and dies on SIGHUP | `daemon/spawn` |
| Long activity gap mid-run (no `task_done` events for > 15 min) | `jq -r 'select(.kind=="task_done") | .ts' "$EVENTS" | sort` then eyeball | Connector stuck (web_fetch on a hung page), network blip, or a single very slow synth pass | `connectors/<name>` or `loop/perf` |
| Mac slept mid-run (large unexplained gap, daemon resumed afterwards) | gap analysis from AC3 + check `pmset -g log` for `Sleep`/`Wake` entries inside the gap window | `caffeinate -i -w` not running — operator skipped §3.2, or caffeinate exited early because `-w` saw a stale PID | (operator error; re-run with §3.2 confirmed before walking away) |
| caffeinate exited within minutes of launch | `ps -p "$CAFFEINATE_PID"` shortly after §3.2 | `-w <pid>` was given the wrong PID (e.g. the *parent* of the daemon, which exits after spawn) | re-run §3.2 with the value from `cat jobs/$JOB_ID/daemon.pid` |
| Disk-cap pruning fired during soak | `jq 'select(.kind=="source_pruned")' "$EVENTS" | wc -l` | Per-job disk cap (default 10 GB) hit on a content-heavy 24h run; expected behavior per #38 | (informational; if pruning was disruptive, raise `--disk-cap-gb`) |
| Cost-cap final-pass synth fired | `jq 'select(.kind=="synthesis_written" and .payload.post_cap==true)' "$EVENTS"` | Budget cap hit; #39 enforcement (`final_synthesis_after_cap` in `orchestrator/synth.py`) wrote the report from cached findings via `frontier_speed`, or from the on-disk template stub if even that tier was out of budget | (informational; verify the report is still coherent) |
| OpenRouter network blip | `jq 'select(.level=="WARN" and .payload.stage=="openrouter") | .payload' "$EVENTS"` | Transient network issue; tenacity backoff (#37) should have absorbed it | `llm/openrouter` (only if backoff didn't recover and the run died) |
| LM Studio crashed mid-run | `jq 'select(.payload.stage=="lm_studio_health" and .level=="WARN")' "$EVENTS"` | Local model server crash; auto-recovery health check (#36) should have detected + waited | `infra/local-llm` (only if auto-recovery didn't kick in) |
| `daemon.pid` present after `research stop --graceful` returned cleanly | `ls -l "$JOB_ROOT/daemon.pid"; ps -p "$DAEMON_PID"` | Daemon still running (final synth slow on a 24h goal — give it up to 15 min), or atexit hook never fired (crash) | `daemon/atexit` (only after the wait window expires) |
| Report missing all three frameworks | `for kw in "Pydantic AI" "LangGraph" "CrewAI"; do grep -c "$kw" "$JOB_ROOT/report.md"; done` | Planner converged on a sub-topic instead of the full comparison; aggressiveness=balanced may have throttled exploration | `planner/coverage` |
| Operator had to manually intervene mid-soak | (event log around the intervention) | AC4 violation by definition; record in postmortem under "Surprises" with the trigger | (depends on trigger; file under the relevant module) |

If the triage table doesn't cover the symptom: open a new issue with
the job folder zipped (sans secrets), the failing AC's command output,
and the timestamps of T0/T1/the last healthy check. Reference the
postmortem so the next soak knows the precedent.
