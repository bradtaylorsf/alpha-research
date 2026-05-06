# Phase 6 — 24-hour soak postmortem

> **Fill-in-the-blanks template.** Run the playbook in
> `test_phase6_soak_24h.md`, capture the values it tells you to
> capture, and replace every `<…>` placeholder below with the actual
> value. Don't reflow the structure; the next operator (or a future
> Claude session) diffs against this layout to see what changed
> between soaks.
>
> Closes the gate from issue #40.

## Run metadata

| Field | Value |
|---|---|
| `JOB_ID` | `<JOB_ID>` |
| Goal | `<goal text — should match canonical fixture from §1 of the playbook>` |
| `T0` (launch, UTC) | `<T0>` |
| `T1` (daemon exit, UTC) | `<T1>` |
| Wall-clock duration | `<WALL_CLOCK>` (HH:MM) |
| Time cap | 24h |
| Budget cap | $25.00 |
| Aggressiveness | `balanced` |
| `completion_reason` | `<goal_complete | budget_cap | task_cap | user_stopped>` |
| Total cost | `$<TOTAL_COST>` |
| Operator | `<name / handle>` |
| Machine / OS | `<e.g. MacBook Pro M3 Max, macOS 15.3>` |

## Per-model spend

Captured from the AC7 query in the playbook
(`SELECT model, COUNT(*), SUM(cost_usd), …`). Sort by `total_cost_usd`
descending.

| Model | Calls | Total cost (USD) | Input tokens (k) | Output tokens (k) |
|---|---:|---:|---:|---:|
| `<model-id-1>` | `<N>` | `<$X.XX>` | `<K>` | `<K>` |
| `<model-id-2>` | `<N>` | `<$X.XX>` | `<K>` | `<K>` |
| ... | | | | |

**Total Opus spend (per #40 AC7):** `$<OPUS_COST>` across `<N>` calls.

## Goal + final report quality assessment

**Coherence (one paragraph):** `<Did the report read end-to-end? Was
the structure visible — exec summary, framework-by-framework
breakdown, comparison matrix, recommendations? Did the synth tier
hallucinate frameworks or confuse versions?>`

**Citation density:** `<count of [N] / http(s) lines from AC6>` lines
of citation markers across `<N>` total report lines. `<one sentence
on whether the citations are real / load-bearing or filler>`.

**Framework coverage:**

| Framework | Mentions | Notes |
|---|---:|---|
| Pydantic AI | `<N>` | `<one-line on depth of treatment>` |
| LangGraph | `<N>` | `<one-line>` |
| CrewAI | `<N>` | `<one-line>` |

**Quality bar (per #40 AC6):** `<PASS | FAIL>` — does this match
"Phase 4 short run scaled up"? `<one sentence justification>`.

## Acceptance criteria checklist

| AC | Criterion | Status | Evidence |
|---|---|---|---|
| AC1 | Goal selected and documented | `<PASS | FAIL>` | `<diff result from playbook AC1>` |
| AC2 | Time cap 24h, budget $25, aggressiveness `balanced` | `<PASS | FAIL>` | `<sqlite SELECT row>` |
| AC3 | `caffeinate -i -w <pid>` documented + used | `<PASS | FAIL>` | `<operator attestation: started at <ts>, exited at <ts>; max task_done gap = <Ns>>` |
| AC4 | Run completed without manual intervention | `<PASS | FAIL>` | `<final status>; <completion_reason>; job_started checkpoints = <N>` |
| AC5 | events.jsonl ERROR/WARN cluster scan | `<PASS | FAIL>` | `<top clusters from playbook AC5; each marked fixed-here / filed-as-#NNN / acceptable noise>` |
| AC6 | Report coherent + cites sources | `<PASS | FAIL>` | `<citation count + framework coverage above>` |
| AC7 | Cost under cap; Opus spend recorded | `<PASS | FAIL>` | `<TOTAL_COST> ≤ $25.00; Opus spend $<OPUS_COST>` |
| AC8 | Postmortem written | PASS | this file |

If any row is FAIL, the soak is not green. Either re-run after a fix
or open a follow-up issue and link it under "Surprises".

## Event log analysis

### ERROR / WARN clusters by stage

| Stage | Count | Disposition | Follow-up |
|---|---:|---|---|
| `<stage-1>` | `<N>` | `<fixed-in-this-PR | filed-as-#NNN | acceptable-noise>` | `<link or PR>` |
| `<stage-2>` | `<N>` | ... | ... |

### Notable individual events

> Paste any single events that are interesting in their own right —
> e.g., the first `BudgetExceeded`, a checkpoint that came back from
> a tier fallback, a connector retry pattern that's worth a future
> ticket. One bullet each, with a short caption.

- `<event JSON>` — `<why it's notable>`
- ...

### Activity profile

Hourly event-count buckets (from the AC4-style query in
`test_phase5_lifecycle.md` §AC4 — same shape, scaled to 24 buckets).
Paste the output verbatim:

```
<hourly bucket counts from `jq -r '.ts' "$EVENTS" | python3 -c '...'`>
```

Max `task_done` gap: `<Ns>` (`<reason if > 15 min: connector hang /
synth pass / explained / unexplained>`).

## Surprises + follow-up issues filed

> Anything the playbook didn't predict. New connectors that needed
> retries, planner behavior that surprised you, model-routing edge
> cases, etc. One bullet per surprise. Link the filed issue.

- `<surprise>` → filed as `#<NNN>`
- ...

If the answer is "nothing surprising, the playbook covered everything",
say so explicitly: future operators reading this should know the
playbook held up under a real run.

## Lessons fed back via `alpha-loop review`

`alpha-loop review` propagates learnings into future runs. After the
soak, capture the high-leverage takeaways here and confirm they were
fed through:

- [ ] `alpha-loop review` ran on this branch
- [ ] Learnings appended to `<list any agents/skills updated>`

Top 3 lessons (in priority order):

1. `<lesson>`
2. `<lesson>`
3. `<lesson>`

## Appendix — raw command output (optional)

> If anything in the AC checklist is non-obvious, paste the exact
> command + output here so a future reader doesn't have to re-derive
> the value from a snapshot of the job folder.
