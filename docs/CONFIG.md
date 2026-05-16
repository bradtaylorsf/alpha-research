# Configuration

## Per-Job Knobs

Per-job settings live in `jobs/<job-id>/intake.json` and are mirrored in the
`jobs.intake_json` SQLite column. `research start` writes them from CLI flags
and intake answers; plan YAML can override some behavior per task by setting
fields on `task_template[].payload`.

| Field | Default | Scope | Behavior |
|---|---:|---|---|
| `translate_non_english` | `false` | job or task payload | When true, extraction writes an English mirror for each non-English finding as `findings/NNNNNN.translation.md`. |
| `fragments` | `false` | job | Records that the operator requested section-fragment synthesis with `research start --fragments`. Runtime routing still uses `RESEARCH_FRAGMENT_SYNTH=1`, which the CLI sets for the spawned daemon. |

`translate_non_english` is intentionally opt-in. Use it for multilingual
archive runs where French, Spanish, or other non-English primary sources are
material to the answer. Do not enable it for English-only goals.

When enabled, each translated finding uses the `frontier_speed` tier and is
budget-tracked through `BudgetTracker`. If the estimated translation would push
the job past its cap, the original finding is still kept, no translation file is
written, and the loop emits an `INFO` event named `translation_skipped_budget`.

Task-level opt-in example:

```yaml
task_template:
  - kind: gallica_search
    payload:
      query: guerre d'Algerie 1956
      translate_non_english: true
```

Job-level CLI opt-in:

```bash
research start --skip-intake --goal "Algerian war archives" --translate-non-english
```

## Fragment Synthesis Rollout

Whole-report synthesis remains the default. To opt a job into section-fragment
synthesis, run:

```bash
research start --skip-intake --goal "Widget Co governance" --fragments
```

The flag sets `RESEARCH_FRAGMENT_SYNTH=1` for the daemon and stores
`"fragments": true` in `jobs/<job-id>/intake.json`. Fragment state is persisted
under `fragments/<section>/NNNN.{md,json}` and mirrored in SQLite, so resume and
final-synthesis paths reassemble `report.md` from the latest fragments after a
restart. Logs include a `synthesis_mode` event with `mode="fragments"` or
`mode="legacy"` for operator visibility.
