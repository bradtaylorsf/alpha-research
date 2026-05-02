# Phase 4 end-to-end test (manual playbook)

This is **not** a pytest test. The Phase 4 "done when" gate exercises real
LM Studio + real OpenRouter and a full plan/loop/synth cycle, which is too
heavy and too cost-bearing for CI. Run it by hand whenever Phase 4 needs
a green check (e.g. before tagging a milestone, or after a substantive
change to `orchestrator/{plan,loop,synth}.py`).

Closes the gate from issue #30.

## 1. Goal (canonical fixture)

Use a low-stakes, public-info, synthesis-friendly target so the run is
reproducible and the report quality is easy to eyeball:

> **Summarize the history of the LangGraph framework**

Why this goal:

- Public, well-documented, no controversy → connectors won't get blocked
  and the synth pass has plenty to cite.
- Narrative ("history of") plays to the synth tier's strength and produces
  output that's easy to scan for citation correctness.
- Small enough to fit comfortably under the 30 min wall-clock cap and the
  $5 budget.

## 2. Prerequisites

Run from the repo root.

```bash
# 2.1 LM Studio reachable on the configured port (default :1234) with the
#     local tiers from config/models.yaml loaded. The simplest sanity check
#     is `research doctor` — it probes the lm_studio check directly.
research doctor
# Required checks must all pass; the lm_studio check is OPTIONAL but for
# the e2e it must be PASS too (the planner critique loop hits local tiers).

# 2.2 OPENROUTER_API_KEY is loaded from .env (or the process env). The
#     doctor check `env:OPENROUTER_API_KEY` should be PASS, not WARN.
grep -q '^OPENROUTER_API_KEY=' .env && echo "key present in .env"

# 2.3 No stale LLM cache contaminates the run. Cache hits return zero-cost
#     stand-ins, which makes "did frontier really synthesize?" ambiguous.
research config cache-clear

# 2.4 Confirm the smoke verbs work end-to-end (cheap; ~$0.01).
research _smoke-llm general "Say hello"     # local
research _smoke-llm frontier "Say hello"    # cloud
```

Stop here if any of these fail — fix the underlying wiring before starting
the timed run, otherwise you'll burn time on infra issues instead of
exercising the loop.

## 3. Driver script

`research start` only **registers** a job in Phase 4 — the daemon ships in
Phase 5. Until then we drive the plan + loop manually with a one-shot
Python invocation. Copy this verbatim, substituting the `JOB_ID` printed
by `research start`:

```bash
JOB_ID="<paste from `research start`>"

uv run python -c "
import asyncio
from pathlib import Path

from research_agent.llm.budgets import BudgetTracker
from research_agent.llm.cache import DEFAULT_CACHE_PATH, LLMCache
from research_agent.llm.router import Router, load_models_config
from research_agent.orchestrator.loop import run_loop
from research_agent.orchestrator.plan import initial_plan
from research_agent.storage import db
from research_agent.storage.jobs import Job

async def main(job_id: str) -> None:
    db.migrate().close()
    job = Job.load(job_id)
    cap = job.intake.get('budget_cap_usd')
    budget = BudgetTracker(job.id, cap_usd=float(cap) if cap is not None else None)
    cache = LLMCache(DEFAULT_CACHE_PATH)
    cfg = load_models_config(Path('config/models.yaml'))
    router = Router(cfg, budget, job=job, db_path=job.db_path, cache=cache)

    job.set_status('running')
    try:
        await initial_plan(job, router=router)
        result = await run_loop(job, router)
        print('run_loop result:', result)
        job.set_status('completed' if result['completed'] else 'stopped')
    except Exception as exc:
        job.set_status('failed')
        raise

asyncio.run(main('$JOB_ID'))
"
```

The driver script:

- Re-runs `db.migrate()` defensively in case the schema added rows since
  `research start` was called.
- Loads the job, builds a `BudgetTracker` from the intake's `budget_cap_usd`,
  and constructs a `Router` exactly the way the future daemon will.
- Calls `initial_plan(job, router=router)` to emit the v1 `plan_created`,
  then drains the queue with `run_loop(job, router)`.
- Updates `jobs.status` so `research list` reflects the terminal state.

## 4. Step-by-step run

In one terminal (the timer):

```bash
# 4.1 Register the job. Capture stdout — the job id is the first line.
research config cache-clear

START_OUTPUT=$(research start --skip-intake \
    --goal "Summarize the history of the LangGraph framework" \
    --time-cap 1 \
    --budget-usd 5)
echo "$START_OUTPUT"
JOB_ID=$(echo "$START_OUTPUT" | awk '/^Started job/ {print $3}')
echo "JOB_ID=$JOB_ID"

# 4.2 Start the wall-clock timer.
T0=$(date +%s)

# 4.3 Drive the loop (paste the §3 script here, with $JOB_ID set).
```

In a second terminal (live event tail):

```bash
research logs "$JOB_ID" -f
```

Leave the tail running until the driver script exits.

## 5. Acceptance verification

After the driver script returns, capture the wall-clock and walk the
checklist below. Each line maps an acceptance criterion to a concrete
shell check.

```bash
T1=$(date +%s)
WALL=$(( T1 - T0 ))
echo "wall-clock: ${WALL}s"
JOB_ROOT="jobs/$JOB_ID"
```

### AC1 — wall-clock under 30 min

```bash
test "$WALL" -lt 1800 && echo "PASS: wall-clock=${WALL}s" || echo "FAIL: wall-clock=${WALL}s >= 1800s"
```

### AC2 — `report.md` exists, non-empty, has citations

```bash
test -s "$JOB_ROOT/report.md" && echo "PASS: report present + non-empty" \
    || echo "FAIL: report missing or empty"

# A real synth pass cites sources as [N] inline + lists URLs in the trailing
# 'Sources' section (per src/research_agent/prompts/synthesizer.md §31).
grep -E '^\[[0-9]+\]|https?://' "$JOB_ROOT/report.md" | head -5
research view "$JOB_ID" --report
```

If `report.md` starts with `# Report (truncated)` the budget cap was hit
mid-synth — see triage table below.

### AC3 — events.jsonl has the expected sequence

The actual event ordering (real implementation, not the wording in #30) is:

1. `plan_created` (kind=`plan_created`, emitted by `initial_plan`)
2. `kind=checkpoint, payload.checkpoint_kind=job_started` (run_loop entry)
3. many `task_pulled` / `task_done`
4. `synthesis_written` (every `HEURISTIC_CHECK_EVERY_N=25` tasks, plus
   final synth on cap-hit) and `kind=checkpoint, payload.checkpoint_kind=synthesis_done`

There is **no** terminal `completed` event in `EventKind` today — the loop
exits when `plan.is_complete()` is True. We verify completion via the
driver's `run_loop` return dict (`completed=True`) and the presence of
`report.md`.

```bash
EVENTS="$JOB_ROOT/events.jsonl"

# Top-level kinds present, in order of first occurrence.
jq -r '.kind' "$EVENTS" | awk '!seen[$0]++'

# Spot-check the four required milestones.
jq -r 'select(.kind=="plan_created") | .ts' "$EVENTS" | head -1
jq -r 'select(.kind=="checkpoint" and .payload.checkpoint_kind=="job_started") | .ts' "$EVENTS" | head -1
jq -r 'select(.kind=="task_done") | .kind' "$EVENTS" | wc -l        # expect > 0
jq -r 'select(.kind=="synthesis_written") | .payload.report_path' "$EVENTS" | tail -1
```

Pass if: `plan_created` present, `checkpoint(job_started)` present,
`task_done` count > 0, at least one `synthesis_written` whose
`report_path` resolves to the on-disk `report.md`.

### AC4 — `cost_so_far_usd` recorded and under $5

```bash
research status "$JOB_ID" | grep -E 'Cost so far|Budget cap'

# Cross-check against the DB row (in case the panel formatting hides $0.0).
sqlite3 data/index.sqlite \
    "SELECT cost_so_far_usd FROM jobs WHERE id='$JOB_ID';"
```

Pass if the value is > 0 (a $0 total means every cloud call was a cache
hit — likely because §2.3 was skipped) **and** strictly < 5.0.

### AC5 — no unexpected exceptions

Only `RetriableError` traces are tolerated; anything else is a regression.

```bash
# Unexpected ERROR-level events (anything not retry-tagged is a bug).
jq -r 'select(.level=="ERROR") | .payload' "$EVENTS"

# Tracebacks are never logged through `emit()`, so any 'Traceback' in
# events.jsonl means something printed raw to the file via a logger
# misconfiguration — investigate.
grep -E '^.*Traceback' "$EVENTS" || echo "PASS: no raw tracebacks in events.jsonl"
```

Pass if every ERROR row has either `retries_exhausted: true` (handler
retried `RETRY_MAX_ATTEMPTS` times) or `fatal: true` from a
`_not_implemented_handler` kind that the planner shouldn't have emitted —
the latter is a planner-prompt issue, not a loop bug, and should be
filed under `planner` (see triage).

## 6. Tear-down + clean re-run

The job folder + DB row are kept for inspection by default. To run the
playbook again from a clean slate:

```bash
# Drop the job folder, the SQLite rows, and the LLM cache. The DB
# `jobs` row cascades to plans/tasks/findings/sources via FKs, so a
# single DELETE is enough.
rm -rf "jobs/$JOB_ID"
sqlite3 data/index.sqlite "DELETE FROM jobs WHERE id='$JOB_ID';"
research config cache-clear
```

Do **not** delete `data/index.sqlite` outright — other jobs (and the
cross-job FTS index) live there.

## 7. Triage table

When a check fails, start here before opening a new issue.

| Symptom | Diagnostic command | Most likely cause | File-under label |
|---|---|---|---|
| `research doctor` reports `lm_studio` FAIL/WARN | `curl -s "$LMSTUDIO_BASE_URL/models"` | LM Studio not running, wrong port, or no model loaded | `infra/local-llm` |
| Cloud calls fail with 429 RateLimitError | `jq 'select(.level=="ERROR")' "$EVENTS"` | OpenRouter rate-limit during burst; router retries on `fallback_model` | `llm/router` (only if `fallback_model` mis-wired) |
| `plan_created` present but zero `task_pulled` | `sqlite3 data/index.sqlite "SELECT COUNT(*) FROM tasks WHERE job_id='$JOB_ID';"` | Planner emitted no `task_template`; loop has nothing to drain | `planner` |
| `task_done` count = `MAX_TASKS_PER_JOB` (10000) | `jq -r '.payload | select(.cap_hit==true)' "$EVENTS"` | Anti-runaway cap fired; final synth ran best-effort | `loop/cap-hit` |
| Report ≠ empty but starts with `# Report (truncated)` | `head -1 "$JOB_ROOT/report.md"` | Frontier + frontier_speed both hit `BudgetExceeded`; stub written | `synth/budget` |
| All cloud calls cost $0 | `sqlite3 data/index.sqlite "SELECT COUNT(*) FROM llm_calls WHERE job_id='$JOB_ID' AND finish_reason='cache';"` | LLM cache wasn't cleared (§2.3); rerun after `research config cache-clear` | (operator error, no issue needed) |
| Driver script raises `OPENROUTER_API_KEY environment variable is required` | `printenv OPENROUTER_API_KEY` | `.env` not loaded by the bare `python -c …` invocation | use `uv run` (which loads `.env` via `research_agent.config`) |
| Wall-clock blew past 30 min | `jq -r 'select(.kind=="task_done") | .ts' "$EVENTS" | tail -1` minus the start ts | Slow connector (web_fetch on a stiff page), or planner emitting too many sequential tasks | `loop/perf` or `connectors/<name>` |

If the triage table doesn't cover the symptom: open a new issue with the
job folder zipped (sans secrets) and the failing AC's command output.
