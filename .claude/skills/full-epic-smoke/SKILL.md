---
name: full-epic-smoke
description: After an alpha-loop epic completes (multiple sub-PRs merged into a session branch), design and run a per-sub-issue smoke matrix that exercises each fix at its real runtime entry point — pytest passing is not enough.
when-to-use: When a session PR contains 3+ merged sub-PRs that close distinct issues and the user asks "smoke test the epic" / "verify each issue is complete" / "is this PR ready to merge".
---

# Full epic smoke matrix

A passing `pytest` proves "no regressions and the new functions return the
expected shape on synthetic input." It does **not** prove "the new function is
wired into the right runtime entry point so the next overnight run will
actually use it." This skill closes that gap.

Complements [smoke-verification](../smoke-verification/SKILL.md) (per-connector
smoke command quality); this one operates one level up — across an epic.

## When to fire

User says one of:
- "I just finished epic #N — run a smoke test"
- "verify each issue is complete and the PR is ready to merge"
- "smoke test the epic"

…and the session PR for that epic has ≥3 merged sub-PRs.

## Procedure

### 1. Inventory what shipped

```bash
gh pr list --state open --base main --json number,title,headRefName  # find session PR
SESSION_BRANCH=$(git rev-parse --abbrev-ref HEAD)
gh pr list --state merged --base "$SESSION_BRANCH" --limit 30 \
  --json number,title,files,body,mergedAt
```

For each merged sub-PR, capture:
- The issue number it closes (`Closes #N` from body)
- Files touched (these are the surfaces to smoke)
- Acceptance criteria from the issue body — `gh issue view <N>`

### 2. Foundation sweep (must be green before continuing)

```bash
uv run pytest -q                        # baseline regression check
uv run research doctor 2>&1 | tail      # env state, LM Studio, keys
```

Foundation failures stop the smoke. Don't smoke individual fixes if the
baseline is broken — fix the regression first.

### 3. Design per-issue smokes that hit the runtime path

For each sub-issue, pick checks that prove the fix is **wired up**, not just
that the new function returns. Anti-pattern: re-running the unit tests that
already shipped with the PR — they don't tell you whether the fix is reachable
from the orchestrator.

Targets by fix type:

| Fix type | Smoke target |
|---|---|
| New module / function | Import + call with synthetic input → assert structured return |
| New CLI subcommand | `uv run <cmd> --help` exits 0; help text contains expected flags |
| New file (skill, prompt, fixture) | File exists; required sections / frontmatter parse |
| Constant / mapping change | Read the dict; assert each key→value pair |
| Wiring into orchestrator | `default_handlers(...)` includes new kind; planner/synth prompt references new var |
| Synthesizer / prompt change | Prompt `.md` references new context var; rendered prompt parses |

### 4. One consolidated script, per-issue rollup

Write `/tmp/epic<N>_smoke.py` — single script that runs all checks, records
`(issue, label, ok, note)`, prints rollup, exits non-zero if any check fails.
Makes the result copy-pasteable into the PR comment and re-runnable without
prompting.

### 5. Comment on the session PR

Use `gh pr comment <pr> --body "$(cat <<'EOF' ... EOF)"` with the per-issue
table, the foundation summary, and a go/no-go recommendation. This documents
the validation evidence on the PR so the merge decision is auditable.

### 6. Merge (if green and user confirms)

```bash
gh pr merge <session-pr> --squash --delete-branch
```

Squash preserves the sub-PR commit history (alpha-loop sessions use squash by
convention). The session PR's `Closes #...` references close all the
sub-issues in a single sweep.

## Common gotchas (smoke-script bugs, not implementation bugs)

These cost iterations on epic #214's smoke and are worth memorizing:

- **`emit(job, ...)` needs a real `Job`**, not a stub. Pattern:
  ```python
  from research_agent.storage import db
  from research_agent.storage.jobs import Job
  tmp = Path(tempfile.mkdtemp()); db_path = tmp / "index.sqlite"
  db.migrate(path=db_path).close()
  job = Job.create({"goal": "smoke"}, jobs_root=tmp / "jobs", db_path=db_path)
  ```
- **Prompts have `{{var}}` placeholders** — `load_prompt("planner")` raises
  if you don't pass values. To check raw content, read the `.md` file directly.
- **`_format_source_line.fetched_at` is integer epoch**, not ISO string.
- **`_chunk_text` paragraph spillover** can produce chunks larger than the
  target — assert ≤2× target, not exact target.
- **`default_handlers(router)`** needs a router-shaped object — a stub class
  with `model_for(tier)` returning `None` is enough for handler-registration
  smokes.
- **Acceptance criteria that say "after re-running the overnight"** are
  end-to-end runtime criteria, not smoke criteria. Note them as PARTIAL — the
  smoke proves the wiring; only a real overnight proves the metric (e.g.
  ">=80% of congress.gov sources are 119th Congress").

## Why this exists

Epic #214 shipped 8 sub-issues touching loader, prompts, CLI, storage, and the
orchestrator loop. Each PR's unit tests proved its function works; the
session-level smoke proved each function was *reachable* from the runtime. The
two cost together (≈30 min smoke + ≈30 sec pytest) is small insurance against
shipping a feature that's wired wrong and only surfaces 8 hours into the next
overnight run.
