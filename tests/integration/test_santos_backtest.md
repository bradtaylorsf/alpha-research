# Santos backtest — first real validation run (manual playbook)

This is **not** a pytest test. Issue #45 is the v1-complete validation
per the §15 first-run checklist: a public-records-only target with a
known answer, run end-to-end on a real research goal so the operator
can score precision/recall against a gold-standard timeline.

It is the v1 counterpart to Phase 6's `test_phase6_soak_24h.md`
(framework-comparison soak, balanced aggressiveness, 24h cap). Where
Phase 6 stresses the loop on a low-stakes target, Phase 7 / Santos
stresses the loop on a *truthiness-graded* target — every claim in
the report can be checked against the real-world record, so a bad
result is unambiguously bad.

Closes the gate from issue #45.

## 1. Goal (canonical fixture)

The fixture below is fixed by issue #45's acceptance criteria. Do
**not** paraphrase — paste it verbatim at the intake prompt. Drift in
the goal text invalidates the scoring rubric in §5.

> **Investigate George Santos's pre-2022-election public record for
> misrepresentations**

| Field | Value |
|---|---|
| Goal | (text above, verbatim) |
| Domain | `Political / corruption` (the `Political` row in the intake picker) |
| Time cap | 12h (`--time-cap 12`) |
| Budget cap | $25 (`--budget-usd 25`) |
| Aggressiveness | `conservative` (intake's three-choice picker) |
| Corpus | (none — leave blank) |

Why this fixture:

- **Known answer.** The Santos misrepresentations are the most
  thoroughly post-mortemed politician-resume scandal of the decade.
  Three signals are load-bearing for a passing run (see §5):
  (a) the $0→$11M wealth jump between his 2020 and 2022 financial
  disclosures, (b) the Brazilian fraud charges that resurfaced in
  2023 from a 2008 stolen-checkbook case, and (c) the *North Shore
  Leader*'s September/October 2022 reporting that flagged the
  resume holes before national outlets picked them up.
- **Public-records-only.** FEC filings, House financial disclosures,
  Brazilian court records, and a small Long Island weekly are all
  reachable via Playwright + open-web search. No paywalls, no
  data-broker APIs, no subpoenas — exactly the surface area v1 is
  scoped to.
- **Precision/recall is scoreable.** A bad result here looks bad
  (missing wealth jump = clear FAIL); an ambiguous-but-good result
  is rare because the gold-standard timeline is well-published.
- **Conservative aggressiveness, not balanced.** v1 ships with a
  conservative default — this run validates that the conservative
  planner *still* surfaces the load-bearing signals on a
  well-documented target. If conservative misses two of three, the
  default is wrong and follow-up tuning issues land before v1 ships.

## 2. Prerequisites

Run from the repo root.

```bash
# 2.1 LM Studio reachable on the configured port (default :1234) with
#     the local tiers from config/models.yaml loaded; OpenRouter key
#     loaded from .env. Both must be PASS, not WARN.
research doctor

# 2.2 OPENROUTER_API_KEY present (re-check explicitly).
grep -q '^OPENROUTER_API_KEY=.\+$' .env && echo "key present in .env"

# 2.3 No stale LLM cache. Cache hits return $0 and short-circuit
#     identical prompts — a passing-by-accident report is the worst
#     outcome here.
research config cache-clear

# 2.4 Smoke verbs (cheap; ~$0.01 total).
research _smoke-llm general "Say hello"     # local
research _smoke-llm frontier "Say hello"    # cloud
research _smoke-tool web_search "george santos pre-election misrepresentations"

# 2.5 No other research daemon is running.
pgrep -fl 'research_agent.daemon' || echo "none"

# 2.6 Free disk on the volume holding jobs/. Default per-job cap 10 GB.
df -h .
```

Stop here if any of these fail. A 12h validation run that dies in the
first 20 minutes on infra issues is the most expensive way to find a
wiring bug.

## 3. Step-by-step run

Three phases: **launch**, **walk away**, **return**. Same shape as
Phase 6 with one fixture-driven difference: aggressiveness is
`conservative`, which the intake picker does *not* default to — the
operator must explicitly select it at the three-choice prompt.

### 3.1 Launch (Terminal A — the "launching" terminal)

`aggressiveness=conservative` is set by the interactive intake; the
intake's three-choice picker defaults to `balanced`, so the operator
must **arrow up** to `conservative` before pressing Enter. The
`--skip-intake` testing back door does **not** carry aggressiveness
through (`cli.py:start_command` builds `intake_data` from
`goal/domain/time_cap_hours/budget_cap_usd/disk_cap_gb` only). Use
interactive intake so the `conservative` tag lands in `intake.json`
and the `jobs.aggressiveness` column.

```bash
# Clear cache one more time; record T0 immediately after.
research config cache-clear

# Interactive intake. Time-cap and budget-usd are pre-filled via flags
# so the only judgement calls left are: paste goal verbatim, pick
# Political domain, and explicitly select 'conservative' at the
# aggressiveness prompt.
START_OUTPUT=$(research start \
    --time-cap 12 \
    --budget-usd 25)
# Intake answers, in order:
#   - Goal:          paste the canonical text from §1 verbatim
#   - Domain:        Political / corruption
#   - Time cap:      12h           (pre-filled from --time-cap)
#   - Budget cap:    $25           (pre-filled from --budget-usd)
#   - Output:        research dossier (or whichever default fits;
#                    output_orientation is not gated by AC)
#   - Aggressiveness: conservative ← explicitly select; default is balanced
#   - Corpus:        (leave blank)

echo "$START_OUTPUT"
JOB_ID=$(echo "$START_OUTPUT" | awk '/^Started job/ {print $3}')
echo "JOB_ID=$JOB_ID"

T0=$(date +%s)
echo "T0=$T0  ($(date -u -r "$T0" '+%Y-%m-%dT%H:%M:%SZ'))"

# Confirm the daemon was spawned and the PID file is on disk.
DAEMON_PID=$(cat "jobs/$JOB_ID/daemon.pid")
echo "DAEMON_PID=$DAEMON_PID"
ps -p "$DAEMON_PID" -o pid,ppid,stat,command

# Confirm the fixture landed in the job row (catches the "operator
# left aggressiveness on the default" case).
sqlite3 data/index.sqlite \
    "SELECT id, domain, aggressiveness, time_cap_hours, budget_cap_usd
     FROM jobs WHERE id='$JOB_ID';"
# Expected: domain='Political / corruption', aggressiveness='conservative',
# time_cap_hours=12, budget_cap_usd=25.0.

# Confirm intake.json mirrors the same values.
jq '{goal, domain, aggressiveness, time_cap_hours: .time_cap, budget_cap_usd: .budget_usd}' \
    "jobs/$JOB_ID/intake.json"
```

If `aggressiveness` is `balanced` (the picker's default) or `NULL`
(skip-intake path), abort and re-run. The §5 scoring rubric is keyed
to a `conservative` run; mixed-aggressiveness data points are not
comparable.

Record `JOB_ID`, `DAEMON_PID`, and `T0` outside Terminal A (scratch
file, sticky note, second terminal's scrollback).

### 3.2 Live tailing + sleep prevention (Terminal B — the "babysitter" terminal)

Per AC2 of #45, a second terminal must run `research logs <id> -f`
during the run. Phase 7's daemon will outlast a typical operator
attention span, so reuse Phase 6's `caffeinate -i -w <pid>` trick to
keep a Mac from idle-sleeping mid-run.

```bash
# In a SECOND terminal (Terminal B).
JOB_ID=...      # paste from §3.1
DAEMON_PID=...  # paste from §3.1

# Idle-sleep prevention. -i blocks idle sleep; -w ties caffeinate's
# lifetime to the daemon PID so it auto-exits at run end.
caffeinate -i -w "$DAEMON_PID" &
CAFFEINATE_PID=$!
echo "CAFFEINATE_PID=$CAFFEINATE_PID"

# Live log tail (this satisfies AC2). Ctrl-C to detach without
# affecting the daemon; re-attach with the same command.
research logs "$JOB_ID" -f
```

Why each flag matters (same as Phase 6):

- `-i` blocks idle sleep specifically. Without it, a default-config
  Mac sleeps after ~10–30 min of no input, freezing the daemon and
  timing out OpenRouter HTTP keepalives mid-call.
- `-w <pid>` makes caffeinate exit when the daemon dies (graceful
  stop, kill, or crash). No orphan caffeinate, no manual cleanup.

Non-macOS equivalents (document the chosen mechanism in §4 / results
file so future runs know what worked):

- Linux: `systemd-inhibit --what=idle --who=research --why='santos backtest' sleep infinity &`
- Windows: PowerToys Awake, or `powercfg /requestsoverride PROCESS research_agent.daemon SYSTEM`

### 3.3 Close Terminal A (the SIGHUP-survival check)

Literally close the window/tab, or `exit` the shell. Do **not** Ctrl-C
the daemon — the point is that closing the controlling terminal sends
SIGHUP and `start_new_session=True` in `spawn_daemon` should mean the
daemon doesn't see it.

```bash
# From Terminal B.
ps -p "$DAEMON_PID" -o pid,ppid,stat,command
ps -o ppid= -p "$DAEMON_PID" | tr -d ' '   # expect: 1
ps -p "$CAFFEINATE_PID" -o pid,ppid,command  # expect: still alive
```

If the daemon died on HUP, abort the run — Phase 5's gate has
regressed and Phase 7 is moot until #35 is re-greened.

### 3.4 Walk away (~12 hours)

Drop in occasionally from any terminal. Same one-liners as Phase 6:

```bash
research status "$JOB_ID"
tail -n 20 "jobs/$JOB_ID/events.jsonl"
research logs "$JOB_ID" -f

ps -p "$DAEMON_PID" >/dev/null && echo alive || echo DEAD
sqlite3 data/index.sqlite \
    "SELECT status, cost_so_far_usd, last_activity_at FROM jobs WHERE id='$JOB_ID';"
```

Don't `stop` the job during this window unless something is clearly
broken. Mid-run interventions go in the "Surprises" section of the
results file with a timestamp + reason.

A 12h `conservative` run will usually self-terminate via the budget
cap (cost-cap final-pass synth, #39) or the task-cap anti-runaway
guard before the time cap is reached — `time_cap_hours` is recorded
but not enforced by the loop (see anti-pattern note in CLAUDE.md).

### 3.5 Return: graceful stop (only if the run hasn't self-terminated)

If `research status` shows the job already in a terminal state
(`completed`/`stopped`/`failed`), skip the `stop` command and jump to
the cleanup checks.

```bash
# Only if status is still 'running'.
research stop "$JOB_ID" --graceful
echo "stop exit code: $?"

# Wait for the daemon to finish final synth and exit.
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

test -e "jobs/$JOB_ID/daemon.pid" \
    && echo "WARN: daemon.pid still present" \
    || echo "PASS: daemon.pid removed"

sqlite3 data/index.sqlite \
    "SELECT status, completion_reason, cost_so_far_usd FROM jobs WHERE id='$JOB_ID';"
```

## 4. Acceptance verification

Each AC from #45 maps to a concrete shell check below. Run them in
order; record PASS/FAIL with the actual values. The same values feed
the results template (`santos_backtest_results.md`).

```bash
JOB_ROOT="jobs/$JOB_ID"
EVENTS="$JOB_ROOT/events.jsonl"
REPORT="$JOB_ROOT/report.md"
```

### AC1 — intake matches fixture (goal / domain / aggressiveness / caps)

```bash
# Goal text exactly matches the canonical fixture.
diff <(jq -r '.goal' "$JOB_ROOT/intake.json") \
     <(echo "Investigate George Santos's pre-2022-election public record for misrepresentations")
echo "exit $?  (0 = match, non-zero = goal drifted from the fixture)"

# Domain / aggressiveness / caps from the SQLite row (canonical) +
# intake.json (mirror).
sqlite3 -header -column data/index.sqlite \
    "SELECT id, domain, aggressiveness, time_cap_hours, budget_cap_usd
     FROM jobs WHERE id='$JOB_ID';"
# Expected exact values:
#   domain='Political / corruption'
#   aggressiveness='conservative'
#   time_cap_hours=12
#   budget_cap_usd=25.0

jq '{goal, domain, aggressiveness, time_cap: .time_cap, budget_usd: .budget_usd}' \
    "$JOB_ROOT/intake.json"

# goal.md mirrors the intake (sanity-check that Job.create wrote both).
test -s "$JOB_ROOT/goal.md" && echo "PASS: goal.md present + non-empty" \
    || echo "FAIL: goal.md missing or empty"
```

PASS if all five fixture values match exactly. A NULL/balanced
aggressiveness is a §3.1 fail and invalidates §5 scoring.

### AC2 — `research logs <id> -f` ran in another terminal during the run

`research logs -f` does not write its own log, so this AC is operator-
attested. Capture the attestation values for the results file, plus
an indirect check that something was tailing.

```bash
# Operator attestation (fill into the results file):
echo "research logs $JOB_ID -f started in Terminal B at: <fill in>"
echo "still tailing at last operator check-in:           <fill in>"
# Optional: screenshot of Terminal B with a few lines visible. Drop
# the path in the results file under "Appendix — raw command output".

# Indirect check: the events file should be growing during the run.
# A static events.jsonl past T0+5min implies the daemon stalled and
# the operator wouldn't have seen any tail output anyway.
wc -l "$EVENTS"
```

PASS if the operator attests that `research logs $JOB_ID -f` was
running in a second terminal for the duration of §3.4.

### AC3 — known-signal scoring

The pass/fail core of #45. For each of the three load-bearing signals
below, score (a) Surfaced (Y/N), (b) Citation type, (c) page-anchor
evidence. Run the suggested `grep` per row and record the exact line
numbers in the results file.

| # | Signal | Suggested grep |
|---|---|---|
| (a) | $0→$11M wealth jump between 2020 and 2022 financial disclosures | `grep -in -E '\$0|\$11(\.[0-9])?\s?M|\$11,000,000\|disclosure' "$REPORT"` |
| (b) | Brazilian fraud / 2008 stolen-checkbook charges | `grep -in -E 'Brazil(ian)?\|fraud\|checkbook\|estelionato' "$REPORT"` |
| (c) | *North Shore Leader* September/October 2022 reporting | `grep -in -E 'North Shore Leader\|northshoreleader' "$REPORT"` |

For each row, score:

- **Surfaced (Y/N)** — does the report make a factual claim about the
  signal? Not "would have if it had more time" — it must appear in
  the committed `report.md`.
- **Citation type** — for each surfaced signal, classify the
  supporting URL:
  - `primary` — the source itself: FEC.gov / clerk.house.gov / a
    Brazilian court PDF / *northshoreleader.com* article. The URL
    timestamp must be `<= T1` and reachable.
  - `secondary` — a national outlet (NYT, WaPo, AP, Reuters, etc.)
    citing the primary. Acceptable for credit but lowers the row's
    grade.
  - `none` — the claim is unsourced or sourced to a non-authoritative
    page (Wikipedia, opinion blog, Reddit). Counts as Surfaced=N for
    pass-criterion arithmetic.
- **Page-anchor evidence** — record the line number(s) in `report.md`
  *and* the cited URL(s). Both go in the results-file scoring table
  so a future operator can re-read the exact passage.

```bash
# Example: signal (a). Replace pattern + run for (b) and (c).
grep -in -E '\$0|\$11(\.[0-9])?\s?M|\$11,000,000|disclosure' "$REPORT" | head -10

# Verify the URLs cited near each match are reachable and primary.
# Pull URLs from a window around each grep hit:
awk 'NR>=L-3 && NR<=L+3 {print}' L=<line-from-grep> "$REPORT" \
    | grep -Eo 'https?://[^ )]+'
# Spot-check the first URL: was it crawled, and is it a primary source?
sqlite3 data/index.sqlite \
    "SELECT url, retrieved_at, host FROM job_sources
     WHERE job_id='$JOB_ID' AND url='<paste-url>';"
```

### AC4 — results documented in `tests/integration/santos_backtest_results.md`

```bash
test -s tests/integration/santos_backtest_results.md \
    && echo "PASS: results file present + non-empty" \
    || echo "FAIL: results file missing or empty"

# All <…> placeholders should be replaced.
grep -nE '<JOB_ID>|<T0>|<T1>|<TOTAL_COST>|<WALL_CLOCK>|<…>|<...>' \
    tests/integration/santos_backtest_results.md \
    && echo "FAIL: placeholders remain in results file" \
    || echo "PASS: no placeholders remain"
```

### AC5 — follow-up tuning issues filed (NOT in this issue)

Per #45: "Findings drive prompt + `models.yaml` tuning issues (filed
as follow-ups, not in this issue)." Do not edit prompts or
`config/models.yaml` in this PR. Instead, file separate issues — see
§6 for the categories — and link them in the results file.

```bash
# Quick check that at least one follow-up was filed against the
# milestone (this is operator-attested; gh CLI is the canonical check).
gh issue list --milestone "008 - Polish" --state all --search "santos in:title,body" \
    --json number,title,state | jq .
```

PASS if every gap surfaced by §5 scoring has a corresponding open
issue (or an explicit "no gaps" note in the results file).

### AC6 — pass criterion: ≥2 of 3 known signals with primary-source citations

Pass-criterion arithmetic. The §5 rubric says PASS = at least two of
the three signals are Surfaced=Y *and* Citation type=`primary`. A
secondary-only citation does not count toward the pass criterion
(though it is recorded for the scoring table).

```bash
# Manually count from the §5 scoring table:
#   primary_hits = number of rows with Surfaced=Y AND Citation type=primary
#   PASS if primary_hits >= 2
echo "primary_hits = <fill in from §5>"
echo "PASS criterion (>=2 of 3): <PASS | FAIL>"
```

Edge cases:

- Two primary + one secondary → **PASS**, but flag the secondary-only
  signal as a follow-up (likely a connector or planner gap).
- One primary + two secondary → **FAIL** by the strict reading of
  AC6. Re-running with adjusted prompts is allowed; if multiple runs
  fail, that's the v1-not-shippable verdict.
- Three Surfaced=Y but all secondary → **FAIL**. The whole point of
  the validation is that the report cites primary sources.

### AC7 — v1-shippable decision

Only if AC6 is PASS:

```bash
echo "AC6 = PASS, marking v1-shippable in results file."
# The operator updates milestone 008 and opens the v1 ship issue per §7.
```

If AC6 is FAIL:

```bash
echo "AC6 = FAIL — v1 is NOT shippable yet."
# File the follow-up tuning issues per §6, re-run after fixes,
# and only then revisit the §7 ship gate.
```

## 5. Scoring rubric

This is the spec the §4-AC3 grep checks pour into. The results file
mirrors this table verbatim — fill it in there, not here.

| # | Known signal | What "primary" means here | What "secondary" means here |
|---|---|---|---|
| (a) | $0→$11M wealth jump between 2020 and 2022 House financial disclosures | A House Clerk financial-disclosure URL (clerk.house.gov / fd.house.gov / disclosures-clerk.house.gov) for both the 2020 and 2022 filings, OR direct quotes from the filings with retrieval timestamps | NYT / WaPo / AP / Reuters / CNN coverage that cites the disclosures |
| (b) | Brazilian fraud charges (the 2008 Niterói stolen-checkbook / *estelionato* case) | A Brazilian court / public-prosecutor URL (mp.rj.gov.br, tjrj.jus.br, jusbrasil.com.br *only* if it shows the docket page, etc.) or an FBI/DOJ filing referencing the foreign matter | NYT / Folha / O Globo / AP coverage citing the underlying docket |
| (c) | *North Shore Leader* September/October 2022 reporting that flagged Santos's resume holes pre-election | A *northshoreleader.com* article URL dated 2022-09 or 2022-10 (the Maureen Daly / Grant Lally pieces) | Other outlets citing the *Leader*'s scoop after the fact |

Pass criterion: **≥2 of 3 rows must be Surfaced=Y AND Citation type=`primary`** (per AC6).

Surfaced=Y, Citation type=`secondary` rows count toward the report's
quality score but do *not* satisfy the pass criterion. A row with
Surfaced=N or Citation type=`none` is a clear gap and must produce a
follow-up issue (see §6).

## 6. Follow-up filing template

Per AC5, every gap surfaced by §5 scoring drives a follow-up issue —
not edits in this PR. File one issue per category that fired:

| Gap category | What "this category fired" looks like | Where to file |
|---|---|---|
| Prompt tuning — planner | Conservative planner converged on biographical fluff and never queried financial disclosures | New issue, label `prompt`, milestone 009 |
| Prompt tuning — synth | Findings included the wealth jump but the synth tier dropped it from the report | New issue, label `prompt`, label `synth`, milestone 009 |
| `models.yaml` tier swap | A signal was *found* but the synth tier hallucinated dates/amounts → frontier_speed too weak for political-records reasoning | New issue, label `config`, link `config/models.yaml` row |
| Connector gaps — FEC | No FEC.gov hits in `job_sources` for a candidate with three federal filings | New issue, label `connector`, propose FEC bulk-data + Playwright recipe |
| Connector gaps — court records | No mp.rj.gov.br / tjrj.jus.br hits despite the Brazilian fraud case being on the open docket | New issue, label `connector`, propose Brazilian court Playwright recipe |
| Connector gaps — small-outlet news | No *northshoreleader.com* hits despite the scoop being indexed | New issue, label `connector`, label `news`, propose small-outlet bias in the news connector |
| Citation fidelity | Claim text matches a primary source but the cited URL is secondary or a Wikipedia mirror | New issue, label `synth`, label `citations` |
| Other | Anything the rubric didn't predict | New issue with a clear repro |

For each filed issue, drop the link in the results file's
"Follow-up issues filed" section. Do **not** edit prompts or
`config/models.yaml` in the same PR as #45.

```bash
# Suggested gh CLI invocation per filed issue:
gh issue create \
    --title "<concise title>" \
    --milestone "009 - <next milestone name>" \
    --label "<label>" \
    --body "Follow-up from Santos backtest run $JOB_ID.

Gap: <one sentence>

Evidence: <line ref in santos_backtest_results.md, plus event/log evidence>

Proposed fix: <one paragraph>"
```

## 7. v1-shippable gate

Only execute this section if **AC6 = PASS**.

```bash
# Confirm: the results file's final verdict says v1-shippable.
grep -E '^v1-shippable verdict:.*PASS' tests/integration/santos_backtest_results.md \
    && echo "PASS: results file declares v1 shippable" \
    || echo "STOP: results file does not declare v1 shippable yet"

# Open the v1 ship issue and mark milestone 008 done.
gh issue create \
    --title "Ship v1" \
    --milestone "008 - Polish" \
    --body "Santos backtest passed (job $JOB_ID, $(grep -E '^primary_hits' tests/integration/santos_backtest_results.md)).

See tests/integration/santos_backtest_results.md for the full scorecard.
Follow-up tuning issues are tracked separately under milestone 009."

# Close milestone 008 once all its issues are closed (gh has no
# 'mark milestone done' verb; closing the last issue resolves it).
gh issue list --milestone "008 - Polish" --state open
```

If AC6 is FAIL, do **not** open the ship issue. File the follow-up
tuning issues from §6, give them a milestone, and revisit Santos
after the tuning lands.

## 8. Tear-down + clean re-run

Same shape as Phase 6. The job folder + DB row are kept by default
for inspection (the results file references them).

```bash
rm -rf "jobs/$JOB_ID"
sqlite3 data/index.sqlite "DELETE FROM jobs WHERE id='$JOB_ID';"
research config cache-clear
```

Do **not** delete `data/index.sqlite` outright — other jobs and the
cross-job FTS index live there.

If the daemon is somehow still alive when you run tear-down:

```bash
research stop "$JOB_ID" --kill   # SIGTERM → SIGKILL
ps -p "$DAEMON_PID" 2>/dev/null && echo "STILL ALIVE — investigate"
ps -p "$CAFFEINATE_PID" 2>/dev/null && kill "$CAFFEINATE_PID"
```

## 9. Triage table

Phase-6-shape table extended with Santos-specific symptoms.

| Symptom | Diagnostic command | Most likely cause | File-under label |
|---|---|---|---|
| Goal text in `intake.json` differs from the §1 fixture | `diff <(jq -r .goal "$JOB_ROOT/intake.json") <(echo "Investigate ...")` | Operator paraphrased at the intake prompt | (operator error; abort and re-run) |
| `aggressiveness` is `balanced` instead of `conservative` | `sqlite3 data/index.sqlite "SELECT aggressiveness FROM jobs WHERE id='$JOB_ID';"` | Operator pressed Enter on the picker default | (operator error; abort and re-run, or hand-edit per Phase 6 §3.1 override) |
| Report has no mention of "$11M" or "wealth jump" or "disclosure" | `grep -iE '\$11|wealth|disclosure' "$REPORT"` | Planner converged on biographical surface area; conservative throttled financial-disclosure follow-ups | `prompt` follow-up per §6 |
| Report mentions Santos lying but cites only Wikipedia | `awk '/citation/ || /^https/' "$REPORT" | grep -i wikipedia | head` | Synth tier accepted secondary-only citations; the conservative planner didn't surface primaries | `synth/citations` follow-up per §6 |
| Brazilian fraud surfaced but with wrong date (e.g. "2023" without the 2008 origin) | `grep -in -E 'Brazil|estelionato|2008|2023' "$REPORT"` | Synth tier conflated the 2023 charges with the 2008 underlying case; frontier_speed reasoning gap | `models.yaml` tier-swap follow-up per §6 |
| `job_sources` has zero rows for `clerk.house.gov` / `fd.house.gov` | `sqlite3 data/index.sqlite "SELECT COUNT(*) FROM job_sources WHERE job_id='$JOB_ID' AND host LIKE '%.house.gov';"` | Connector / planner never queried the official disclosure portals | `connector/fec-house` follow-up per §6 |
| `job_sources` has zero rows for `northshoreleader.com` | `sqlite3 data/index.sqlite "SELECT COUNT(*) FROM job_sources WHERE job_id='$JOB_ID' AND host LIKE '%northshoreleader%';"` | News connector biased toward national outlets; small Long Island weekly never indexed | `connector/news` follow-up per §6 |
| `completion_reason` is `task_cap` (10k tasks) before any synth pass | `sqlite3 data/index.sqlite "SELECT completion_reason FROM jobs WHERE id='$JOB_ID';"` | Conservative planner stuck in a fan-out loop; anti-runaway guard fired without producing useful work | `planner/coverage` follow-up |
| Report starts with `# Report (truncated)` | `head -1 "$REPORT"` | Budget cap fired and final-pass synth ran on a near-empty budget; #39 stub path | (informational; may still pass §5 if the stub captured the signals) |
| Same as a Phase 6 symptom | (Phase 6 row) | (Phase 6 cause) | (Phase 6 label) |

If the triage table doesn't cover the symptom: file a new follow-up
issue per §6, link it from the results file's "Surprises" section,
and let the next Santos run pick it up as a regression test.
