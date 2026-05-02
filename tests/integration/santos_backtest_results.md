# Santos backtest — results template

> **Fill-in-the-blanks template.** Run the playbook in
> `test_santos_backtest.md`, capture the values it tells you to
> capture, and replace every `<…>` placeholder below with the actual
> value. Don't reflow the structure; the next operator (or a future
> Claude session) diffs against this layout to see what changed
> between runs.
>
> Closes the gate from issue #45.

## Run metadata

| Field | Value |
|---|---|
| `JOB_ID` | `<JOB_ID>` |
| Goal | `Investigate George Santos's pre-2022-election public record for misrepresentations` |
| Domain | `Political / corruption` |
| `T0` (launch, UTC) | `<T0>` |
| `T1` (daemon exit, UTC) | `<T1>` |
| Wall-clock duration | `<WALL_CLOCK>` (HH:MM) |
| Time cap | 12h |
| Budget cap | $25.00 |
| Aggressiveness | `conservative` |
| `completion_reason` | `<goal_complete | budget_cap | task_cap | user_stopped>` |
| Total cost | `$<TOTAL_COST>` |
| Operator | `<name / handle>` |
| Machine / OS | `<e.g. MacBook Pro M3 Max, macOS 15.3>` |
| Sleep-prevention mechanism | `<caffeinate -i -w <DAEMON_PID> | systemd-inhibit | PowerToys Awake | other>` |

## Per-model spend

Captured from the AC7-shape query in the playbook
(`SELECT model, COUNT(*), SUM(cost_usd), …`). Sort by `total_cost_usd`
descending.

| Model | Calls | Total cost (USD) | Input tokens (k) | Output tokens (k) |
|---|---:|---:|---:|---:|
| `<model-id-1>` | `<N>` | `<$X.XX>` | `<K>` | `<K>` |
| `<model-id-2>` | `<N>` | `<$X.XX>` | `<K>` | `<K>` |
| ... | | | | |

**Total Opus / frontier spend:** `$<X.XX>` across `<N>` calls.

## Known-signal scoring (the §5 rubric, filled in)

The pass-criterion core of #45. PASS = ≥2 of 3 rows with Surfaced=Y
*and* Citation type=`primary`.

| # | Signal | Surfaced (Y/N) | Citation type | URL(s) cited | `report.md` line(s) | Notes |
|---|---|---|---|---|---|---|
| (a) | $0→$11M wealth jump (2020 vs 2022 House financial disclosures) | `<Y \| N>` | `<primary \| secondary \| none>` | `<url1>; <url2>` | `<line numbers from grep>` | `<one-line>` |
| (b) | Brazilian fraud / 2008 Niterói stolen-checkbook (*estelionato*) case | `<Y \| N>` | `<primary \| secondary \| none>` | `<url1>; <url2>` | `<line numbers>` | `<one-line>` |
| (c) | *North Shore Leader* September/October 2022 reporting | `<Y \| N>` | `<primary \| secondary \| none>` | `<url1>` | `<line numbers>` | `<one-line>` |

`primary_hits` = `<count of rows with Surfaced=Y AND Citation type=primary>`

**Pass criterion (≥2 of 3 primary):** `<PASS | FAIL>`

## Precision / recall vs gold-standard timeline

A short narrative judgement against the well-known Santos timeline.
The §5 rubric is binary by design; this section captures shading the
binary check loses.

**Precision (of what the report claimed, how much was right?):**
`<PASS | WARN | FAIL — one paragraph. Were any claims hallucinated?
Did dates / dollar amounts / outlet names match reality? Were there
"truthy-sounding" claims with no supporting source?>`

**Recall (of what the gold-standard timeline contains, how much did
the report surface?):** `<PASS | WARN | FAIL — one paragraph. Beyond
the 3 load-bearing signals, did the report surface the FEC campaign-
finance issues, the volleyball / Baruch / Citi / Goldman fabrications,
the Harbor City Capital connection, the GoFundMe / pet-charity
allegations, etc.? List the top 3 misses if any.>`

## Acceptance criteria checklist

| AC | Criterion | Status | Evidence |
|---|---|---|---|
| AC1 | Intake matches fixture (goal / Political / 12h / $25 / conservative) | `<PASS \| FAIL>` | `<diff result + sqlite row>` |
| AC2 | `research logs <id> -f` ran in another terminal during the run | `<PASS \| FAIL>` | `<operator attestation: started <ts>, last seen tailing <ts>; optional screenshot path>` |
| AC3 | Post-run scoring documented (the §5 rubric above) | `<PASS \| FAIL>` | `<see "Known-signal scoring" table>` |
| AC4 | Results documented in `tests/integration/santos_backtest_results.md` | PASS | this file |
| AC5 | Findings drove follow-up tuning issues (filed, not in this issue) | `<PASS \| FAIL \| N/A>` | `<see "Follow-up issues filed">` |
| AC6 | Pass criterion: ≥2 of 3 primary-source citations | `<PASS \| FAIL>` | `primary_hits = <N>` |
| AC7 | v1 marked shippable iff AC6 PASS | `<PASS \| FAIL \| N/A>` | `<verdict in §7 below>` |

If any row is FAIL (other than AC5 N/A and AC7 N/A), the run is not
green. Either re-run after a fix or open follow-ups under §6 of the
playbook and link them below.

## Event log analysis

### ERROR / WARN clusters by stage

```
<paste output of:
 jq -r 'select(.level=="ERROR" or .level=="WARN") | .payload.stage // .payload.kind // "<unknown>"' "$EVENTS" \
   | sort | uniq -c | sort -rn>
```

| Stage | Count | Disposition | Follow-up |
|---|---:|---|---|
| `<stage-1>` | `<N>` | `<fixed-here \| filed-as-#NNN \| acceptable-noise>` | `<link>` |
| `<stage-2>` | `<N>` | ... | ... |

### Notable individual events

> Single events worth a future ticket on their own — first
> `BudgetExceeded`, a checkpoint that came back from a tier fallback,
> a connector retry pattern, a hallucinated source URL caught by
> citation validation, etc.

- `<event JSON>` — `<why it's notable>`
- ...

### Source-coverage spot check

Did the connectors reach the primary-source domains the rubric expects?
A zero on any of these rows is a strong signal toward a connector
follow-up issue (§6 of the playbook).

```
<paste output of:
 sqlite3 data/index.sqlite \
   "SELECT host, COUNT(*) FROM job_sources WHERE job_id='$JOB_ID' GROUP BY host ORDER BY 2 DESC;">
```

| Host pattern | Hits | Pass-criterion relevance |
|---|---:|---|
| `*.house.gov` (clerk / fd / disclosures-clerk) | `<N>` | Signal (a) primary |
| `fec.gov` | `<N>` | Campaign-finance recall |
| `mp.rj.gov.br` / `tjrj.jus.br` | `<N>` | Signal (b) primary |
| `northshoreleader.com` | `<N>` | Signal (c) primary |
| Wikipedia / Wikidata | `<N>` | Should be near zero — secondary at best |

## Surprises

> Anything the playbook didn't predict. New connectors that needed
> retries, planner behavior that was out of character for
> conservative aggressiveness, model-routing edge cases, citation
> patterns we hadn't seen, etc. One bullet per surprise. Each
> surprise that's worth a fix should be linked to the filed issue
> below.

- `<surprise>` → filed as `#<NNN>`
- ...

If the answer is "nothing surprising, the playbook covered everything",
say so explicitly: future operators should know the playbook held up
under a real run.

## Follow-up issues filed

Per AC5, prompt and `models.yaml` tuning happens in **separate**
issues, not in this PR. List every issue filed as a result of this
run — even ones marked "won't fix yet" — so the audit trail is
complete.

| Category | Issue | Title |
|---|---|---|
| Prompt — planner | `#<NNN>` | `<title>` |
| Prompt — synth | `#<NNN>` | `<title>` |
| `models.yaml` tier swap | `#<NNN>` | `<title>` |
| Connector — FEC / House disclosures | `#<NNN>` | `<title>` |
| Connector — Brazilian court records | `#<NNN>` | `<title>` |
| Connector — small-outlet news | `#<NNN>` | `<title>` |
| Citation fidelity | `#<NNN>` | `<title>` |
| Other | `#<NNN>` | `<title>` |

If a category has no issue (because nothing in §5 fired for it), write
`(n/a)` — leaving cells blank is ambiguous between "no issue needed"
and "operator forgot to file".

## Lessons fed back via `alpha-loop review`

`alpha-loop review` propagates learnings into future runs. After the
backtest, capture the high-leverage takeaways here and confirm they
were fed through.

- [ ] `alpha-loop review` ran on this branch
- [ ] Learnings appended to `<list any agents/skills updated>`

Top 3 lessons (in priority order):

1. `<lesson>`
2. `<lesson>`
3. `<lesson>`

## v1-shippable verdict

> One sentence. Either:
>
> - `v1-shippable verdict: PASS — <one-sentence justification keyed
>   to the §5 scoring table>`
>
> ...or:
>
> - `v1-shippable verdict: FAIL — <one-sentence justification
>   identifying which signal(s) missed and which follow-up issues
>   block the next run>`

`v1-shippable verdict: <PASS | FAIL> — <justification>`

If PASS: open the v1 ship issue and proceed with §7 of the playbook.
If FAIL: do **not** open the ship issue; the follow-up issues above
gate the next attempt.

## Appendix — raw command output (optional)

> If anything in the AC checklist is non-obvious, paste the exact
> command + output here so a future reader doesn't have to re-derive
> the value from a snapshot of the job folder. Especially useful for
> the §5 scoring greps and the per-model spend query.
