#!/usr/bin/env bash
# 1-hour end-to-end smoke for muckwire.
#
# Validates the full agent pipeline (every connector + skills + cornerstone PDF
# + drain-replan + dept tracker + source reconciliation + archive/compare)
# without waiting for an overnight run. Designed to fire after a multi-issue
# epic completes so each shipped feature is exercised in the live runtime path.
#
# Usage:
#   bash tools/smoke_1hour.sh                  # Run all phases (preflight, run, audit) ~75 min
#   bash tools/smoke_1hour.sh preflight        # Phase 1 only — connector sweep (~5 min)
#   bash tools/smoke_1hour.sh start            # Phase 2 only — launch the research job
#   bash tools/smoke_1hour.sh wait [JOB_ID]    # Block until job is no longer running
#   bash tools/smoke_1hour.sh audit [JOB_ID]   # Phase 3 — events + report inspection + compare
#
# Defaults:
#   - Goal: identical to the Project 2025 overnight (A/B-able via `research compare`)
#   - --max-tasks 100, --time-cap 1 (one hour wall-clock)
#   - --local mode (gemma, $0 cost)
#
# Env overrides:
#   SMOKE_BASELINE_JOB - job id to compare against (default: the 2026-05-08 overnight)
#   SMOKE_GOAL         - override the goal text
#   SMOKE_MAX_TASKS    - override --max-tasks (default 100)
#   SMOKE_TIME_CAP     - override --time-cap hours (default 1)

set -u  # treat unset vars as errors; do NOT set -e — preflight failures shouldn't abort the sweep

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[0;33m'; DIM='\033[2m'; RESET='\033[0m'
else
  GREEN=''; RED=''; YELLOW=''; DIM=''; RESET=''
fi

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

pass() { printf "  ${GREEN}[PASS]${RESET} %s\n" "$1"; PASS_COUNT=$((PASS_COUNT+1)); }
fail() { printf "  ${RED}[FAIL]${RESET} %s\n" "$1"; FAIL_COUNT=$((FAIL_COUNT+1)); }
skip() { printf "  ${YELLOW}[SKIP]${RESET} %s\n" "$1"; SKIP_COUNT=$((SKIP_COUNT+1)); }
info() { printf "${DIM}%s${RESET}\n" "$1"; }
hdr()  { printf "\n${YELLOW}=== %s ===${RESET}\n" "$1"; }

DEFAULT_GOAL='Project 2025 implementation tracker: identify which specific policy proposals from the Heritage Foundation'\''s Project 2025 document have been adopted, attempted, withdrawn, or remain pending under the current Trump administration. Organize by federal department (DOJ, DOI, EPA, DHS, State, etc.). For each tracked proposal, surface news coverage, public statements, and any pushback or legal challenges. Prioritize primary sources and date-stamp every finding.'

GOAL="${SMOKE_GOAL:-$DEFAULT_GOAL}"
MAX_TASKS="${SMOKE_MAX_TASKS:-100}"
TIME_CAP="${SMOKE_TIME_CAP:-1}"
BASELINE_JOB="${SMOKE_BASELINE_JOB:-2026-05-08-project-2025-implementation-tracker-identify-which-specific}"

# ---------------------------------------------------------------------------
# Phase 1: connector pre-flight (~5 min)
# ---------------------------------------------------------------------------
phase_preflight() {
  hdr "PHASE 1 — connector pre-flight"

  # No-key group (must return real content)
  local no_key=(
    'fedregister:AI executive order'
    'nonprofits:Heritage Foundation'
    'usaspending:Booz Allen Hamilton'
    'gdelt:Project 2025'
    'littlesis:Heritage Foundation'
    'sanctions:Yevgeny Prigozhin'
    'sos:SBI Builders'
    'licensing:SBI Builders'
    'bbb:SBI Builders'
    'calaccess:Gavin Newsom'
  )
  # Keyed group (DATA_GOV / COURTLISTENER / RESEARCH_USER_AGENT / YOUTUBE)
  local keyed=(
    'congress:Inflation Reduction Act 117'
    'edgar:Cisco 8-K cybersecurity'
    'fec:George Santos'
    'lda:Heritage Foundation'
    'opencorporates:Heritage Foundation'
    'courtlistener:first amendment retaliation'
    'youtube:Project 2025 explained'
  )
  local baseline=(
    'web_search:Project 2025 implementation'
    'reddit:Project 2025'
  )

  smoke_one() {
    local spec="$1" name q
    name="${spec%%:*}"; q="${spec#*:}"
    local out rc
    out=$(uv run research _smoke-tool "$name" "$q" 2>&1)
    rc=$?
    if [ $rc -ne 0 ]; then
      fail "$name — exit $rc"
      printf "${DIM}    %s${RESET}\n" "$(echo "$out" | tail -2 | head -1)"
      return 1
    fi
    # Credential-gated skips first (must precede the content checks). Patterns:
    #   - explicit "live test skipped" (the canonical phrase from #156-style guards)
    #   - "would need <ENV>_API_KEY" / "requires <ENV>_API_KEY"
    #   - HTTP 401/403 from a connector with no key = same shape (real bug example:
    #     courtlistener falls back to anonymous and the API rejects without the key)
    if echo "$out" | grep -qiE 'live test skipped|would need .*_KEY|requires .*_KEY|returned HTTP 40[13]' ; then
      local why
      why=$(echo "$out" | grep -iE 'live test skipped|would need|HTTP 40[13]' | head -1 | tr -d '\n' | head -c 80)
      skip "$name — $why"
      return 0
    fi
    # smoke-verification rule: exit 0 + non-empty content. Threshold 2 lines is
    # tolerant: short tools like the pdf extractor return 4 lines, broken
    # connectors return 0-1.
    local lines
    lines=$(echo "$out" | wc -l | tr -d ' ')
    if [ "$lines" -lt 2 ]; then
      fail "$name — output suspiciously short ($lines lines)"
      return 1
    fi
    pass "$name"
  }

  for spec in "${no_key[@]}";  do smoke_one "$spec" || true; done
  for spec in "${keyed[@]}";   do smoke_one "$spec" || true; done
  for spec in "${baseline[@]}"; do smoke_one "$spec" || true; done

  # PDF extractor — exit 0 + non-zero char_count is sufficient
  if [ -f tests/fixtures/arxiv_paper.pdf ]; then
    local out
    out=$(uv run research _smoke-tool pdf tests/fixtures/arxiv_paper.pdf 2>&1)
    if [ $? -eq 0 ] && echo "$out" | grep -qE 'char_count: [1-9]'; then
      pass "pdf extractor (fixture)"
    else
      fail "pdf extractor"
    fi
  else
    skip "pdf extractor — fixture not found"
  fi
}

# ---------------------------------------------------------------------------
# Phase 2: 1-hour focused research run
# ---------------------------------------------------------------------------
phase_start() {
  hdr "PHASE 2 — launch 1-hour research run"
  info "Goal: $(echo "$GOAL" | head -c 100)..."
  info "Knobs: --max-tasks $MAX_TASKS --time-cap $TIME_CAP --local"
  info ""

  uv run research start --skip-intake --local \
    --goal "$GOAL" \
    --max-tasks "$MAX_TASKS" \
    --time-cap "$TIME_CAP"
  local rc=$?

  if [ $rc -ne 0 ]; then
    fail "research start exited $rc"
    return 1
  fi
  pass "research start kicked off"

  # Capture the job id and start caffeinate
  local job
  job=$(uv run research list --json 2>/dev/null | python3 -c "import sys,json;print(sorted(json.load(sys.stdin),key=lambda j:j['id'])[-1]['id'])")
  if [ -z "$job" ]; then
    fail "could not resolve newly-started job id"
    return 1
  fi
  echo "$job" > /tmp/smoke_1hour_job_id
  info "Job id: $job  (saved to /tmp/smoke_1hour_job_id)"

  local pidfile="jobs/$job/daemon.pid"
  if [ -f "$pidfile" ]; then
    local daemon_pid
    daemon_pid=$(cat "$pidfile")
    info "Starting caffeinate -i -w $daemon_pid in background..."
    caffeinate -i -w "$daemon_pid" &
    echo $! > /tmp/smoke_1hour_caffeinate_pid
    pass "caffeinate attached"
  else
    skip "caffeinate — daemon pid file not found yet (job may have exited fast)"
  fi
}

# ---------------------------------------------------------------------------
# Wait for job completion (or time-cap)
# ---------------------------------------------------------------------------
phase_wait() {
  local job="${1:-$(cat /tmp/smoke_1hour_job_id 2>/dev/null)}"
  if [ -z "$job" ]; then
    fail "no job id provided and /tmp/smoke_1hour_job_id missing"
    return 1
  fi
  hdr "WAIT — polling $job until terminal state (max ~65 min)"

  local elapsed=0 interval=60 max_wait=3900  # 65 min
  while [ $elapsed -lt $max_wait ]; do
    local state
    state=$(uv run research list --json 2>/dev/null | python3 -c "
import sys,json
for j in json.load(sys.stdin):
    if j['id']=='$job':
        print(j.get('status') or j.get('state') or 'unknown')
        break
")
    info "  [$(date +%H:%M:%S)] state=$state  elapsed=${elapsed}s"
    case "$state" in
      completed|failed|stopped|done|finished) pass "job reached terminal state: $state"; return 0 ;;
    esac
    sleep $interval
    elapsed=$((elapsed + interval))
  done
  fail "wait timed out at ${max_wait}s — job may still be running, proceeding to audit"
  return 1
}

# ---------------------------------------------------------------------------
# Phase 3: audit
# ---------------------------------------------------------------------------
phase_audit() {
  local job="${1:-$(cat /tmp/smoke_1hour_job_id 2>/dev/null)}"
  if [ -z "$job" ]; then
    fail "no job id provided and /tmp/smoke_1hour_job_id missing"
    return 1
  fi
  hdr "PHASE 3 — audit job $job"

  local events="jobs/$job/events.jsonl"
  if [ ! -f "$events" ]; then
    fail "events.jsonl missing at $events"
    return 1
  fi

  uv run research status "$job" || true
  echo ""

  hdr "Per-feature event counts (epic #214 fixes)"

  # `grep -c` always prints an integer, even on no-match (with exit 1). Don't use
  # `|| echo 0` — that double-prints "0\n0" which breaks the bash arithmetic
  # comparison below. Just suppress the exit-1 with `|| true`.
  local n_idx n_skill n_cs_extract n_cs_section n_cs_query n_recon n_drain n_cap_hit n_cap_diag n_uncaught
  n_idx=$(grep -c '"kind":"index_loaded"' "$events" || true)
  n_skill=$(grep -c '"kind":"skill_loaded"' "$events" || true)
  # Real event names emitted by orchestrator/loop.py:
  # - cornerstone_extract       — section-walk entry point (per source)
  # - cornerstone_section_extract — per-section finding extraction
  # - cornerstone_query_run     — vector-index query at replan time (only fires
  #                                when planner emits the cornerstone_query task kind)
  n_cs_extract=$(grep -c '"kind":"cornerstone_extract"' "$events" || true)
  n_cs_section=$(grep -c '"kind":"cornerstone_section_extract"' "$events" || true)
  n_cs_query=$(grep -c '"kind":"cornerstone_query_run"' "$events" || true)
  n_recon=$(grep -c '"kind":"source_list_reconciled"' "$events" || true)
  n_drain=$(grep -c '"kind":"drain_replan"' "$events" || true)
  n_cap_hit=$(grep -c '"cap_hit":true' "$events" || true)
  n_cap_diag=$(grep -c '"kind":"cap_diagnostic"' "$events" || true)
  n_uncaught=$(grep -c '"actor":"daemon","kind":"error"' "$events" || true)

  [ "$n_idx" -ge 2 ]      && pass "#211 skills index loaded ($n_idx events, expect ≥2)"      || fail "#211 skills index loaded ($n_idx, want ≥2)"
  [ "$n_skill" -ge 3 ]    && pass "#212 connector skills deep-loaded ($n_skill events)"      || fail "#212 connector skills deep-loaded ($n_skill, want ≥3)"
  # #206: section-walk + per-section extracts are the proof; cornerstone_query is
  # secondary (only fires when the planner emits the task kind, which it may not
  # do in a 60-min run that hits max_tasks first).
  [ "$n_cs_extract" -ge 1 ]  && pass "#206 cornerstone_extract fired ($n_cs_extract events)"   || fail "#206 cornerstone_extract ($n_cs_extract, want ≥1)"
  [ "$n_cs_section" -ge 1 ]  && pass "#206 cornerstone_section_extract fired ($n_cs_section events)"  || fail "#206 cornerstone_section_extract ($n_cs_section, want ≥1)"
  if [ "$n_cs_query" -ge 1 ]; then
    pass "#206 cornerstone_query_run fired ($n_cs_query events)"
  else
    info "  (#206 cornerstone_query_run = 0 — only fires if planner emits cornerstone_query task; ok if section-walk produced enough findings)"
  fi
  [ "$n_recon" -ge 1 ]    && pass "#207 source_list_reconciled fired ($n_recon events)"      || fail "#207 source_list_reconciled ($n_recon, want ≥1 — may be 0 if no synth pass ran)"
  # #209: drain_replan only fires when the queue empties. If max_tasks fires
  # first (cap_hit), drain_replan won't fire — that's expected, not a failure.
  if [ "$n_drain" -ge 1 ]; then
    pass "#209 drain_replan fired ($n_drain events)"
  elif [ "$n_cap_hit" -ge 1 ]; then
    info "  (#209 drain_replan = 0 because max_tasks cap fired first — different cap, expected)"
  else
    fail "#209 drain_replan ($n_drain, want ≥1) — neither drain_replan nor cap_hit fired; loop may not have entered the replan path"
  fi
  if [ "$n_cap_diag" -gt 0 ]; then
    info "  ($n_cap_diag cap_diagnostic events — expected if drain-replans hit the scope cap)"
  fi
  [ "$n_uncaught" -eq 0 ] && pass "no daemon errors"  || fail "$n_uncaught daemon errors — investigate"

  # #213 — first plan's active_strategies + scope_class
  hdr "#213 first plan_created (scope + strategies)"
  grep '"kind":"plan_created"' "$events" 2>/dev/null | head -1 | python3 -c "
import sys, json
try:
    e = json.loads(sys.stdin.read())
    p = e.get('payload', {})
    print(f\"  scope_class:       {p.get('scope_class')!r}\")
    print(f\"  active_strategies: {p.get('active_strategies', [])}\")
    print(f\"  initial tasks:     {p.get('tasks')}\")
    print(f\"  subgoals:          {p.get('subgoals')}\")
except Exception as exc:
    print(f'  (no plan_created event yet: {exc})')
"

  # #208 — dept tracker section in report.md. Synthesizer renders departments
  # as H3 nested under an H2 ``## Departmental Policy Tracker`` (or similar) —
  # not as bare H2 per-department. Match either shape.
  hdr "#208 department tracker sections in report.md"
  if [ -f "jobs/$job/report.md" ]; then
    local depts dept_header
    dept_header=$(grep -cE '^## (Departmental|Department|Agency|Department-by-Department|Federal Department)' "jobs/$job/report.md" || true)
    depts=$(grep -cE '^### (DOJ|DOI|EPA|DHS|State|Defense|DoD|HHS|Education|Treasury|HUD|USDA|Labor|Commerce|VA|Justice|Homeland|Agriculture|Veterans|Personnel|White House)\b|^## (DOJ|DOI|EPA|DHS|State|Defense|DoD|HHS|Education|Treasury|HUD|USDA|Labor|Commerce|VA|Justice|Homeland|Agriculture|Veterans|Personnel)\b' "jobs/$job/report.md" || true)
    if [ "$dept_header" -ge 1 ] && [ "$depts" -ge 3 ]; then
      pass "Department tracker H2 + $depts department subsections"
    elif [ "$depts" -ge 3 ]; then
      pass "$depts department sections (no canonical tracker H2 but content present)"
    else
      fail "$depts department sections (want ≥3); tracker_h2=$dept_header"
    fi
  else
    skip "report.md not yet written — synth may not have fired"
  fi

  # #210 — archive present?
  hdr "#210 prior-report archive"
  if [ -d "jobs/$job/archive" ] && [ -n "$(ls -A jobs/$job/archive 2>/dev/null)" ]; then
    pass "archive/ contains $(ls jobs/$job/archive | wc -l | tr -d ' ') prior report(s)"
    ls -la "jobs/$job/archive" | tail -n +2
  else
    info "  (no archive — fresh job, expected if no prior run for this goal)"
  fi

  # #210 — research compare against baseline
  hdr "#210 research compare vs. baseline ($BASELINE_JOB)"
  if [ -d "jobs/$BASELINE_JOB" ]; then
    uv run research compare "jobs/$BASELINE_JOB" "$job" 2>&1 | head -40 || fail "research compare exited non-zero"
  else
    skip "baseline job dir not found — set SMOKE_BASELINE_JOB env to compare"
  fi

  # Final job stats
  hdr "Final stats"
  uv run python -c "
from research_agent.storage import db
conn = db.connect()
print('  tasks by status:', dict(conn.execute('SELECT status, COUNT(*) FROM tasks WHERE job_id=? GROUP BY status', ('$job',)).fetchall()))
print('  findings:       ', conn.execute('SELECT COUNT(*) FROM findings WHERE job_id=?', ('$job',)).fetchone()[0])
print('  sources:        ', conn.execute('SELECT COUNT(*) FROM job_sources WHERE job_id=?', ('$job',)).fetchone()[0])
print('  plan versions:  ', conn.execute('SELECT COUNT(*) FROM plans WHERE job_id=?', ('$job',)).fetchone()[0])
print('  cost (USD):     ', conn.execute('SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE job_id=?', ('$job',)).fetchone()[0])
"
}

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
cleanup() {
  if [ -f /tmp/smoke_1hour_caffeinate_pid ]; then
    local cpid
    cpid=$(cat /tmp/smoke_1hour_caffeinate_pid)
    if [ -n "$cpid" ] && kill -0 "$cpid" 2>/dev/null; then
      kill "$cpid" 2>/dev/null || true
    fi
    rm -f /tmp/smoke_1hour_caffeinate_pid
  fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Final rollup
# ---------------------------------------------------------------------------
rollup() {
  hdr "ROLLUP"
  printf "  PASS: %s\n" "$PASS_COUNT"
  printf "  FAIL: %s\n" "$FAIL_COUNT"
  printf "  SKIP: %s\n" "$SKIP_COUNT"
  if [ "$FAIL_COUNT" -eq 0 ]; then
    printf "${GREEN}VERDICT: GREEN${RESET}\n"
    return 0
  fi
  printf "${RED}VERDICT: %s FAILURE(S)${RESET}\n" "$FAIL_COUNT"
  return 1
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
mode="${1:-all}"
case "$mode" in
  preflight)  phase_preflight; rollup ;;
  start)      phase_start; rollup ;;
  wait)       phase_wait "${2:-}"; rollup ;;
  audit)      phase_audit "${2:-}"; rollup ;;
  all)        phase_preflight && phase_start && phase_wait && phase_audit; rollup ;;
  -h|--help|help)
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *)
    echo "unknown mode: $mode" >&2
    echo "usage: $0 [preflight|start|wait|audit|all]" >&2
    exit 2
    ;;
esac
