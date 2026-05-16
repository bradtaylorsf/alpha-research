---
name: scope-discipline
description: Keep PR diffs strictly scoped to the linked issue. Reject unrelated env vars, deps, and feature bundling.
when-to-use: When implementing or reviewing any issue — check before commit and during review.
---

# Scope Discipline

This loop's #1 recurring quality issue is PRs that bundle unrelated changes. Across issues #100, #101, #102, #110, #112, and #96, connector PRs swept in PDF VLM escalation env vars, OCR VLM env vars, YouTube API keys, CourtListener tokens, FEC/LDA/OpenCorporates keys, new dependencies, planner changes, and critique model edits — none of which the issue asked for.

## Rule

One issue → one PR → one logical change. Every file in the diff must trace to a line in the issue body or acceptance criteria.

## Before committing

Run `git diff --stat origin/main...HEAD` and for each file ask:

1. Is this file mentioned in the issue body or acceptance criteria?
2. Is this change *required* to make the in-scope code work?
3. Would removing this change still satisfy the acceptance criteria?

If #1 and #2 are no and #3 is yes — the change is out of scope.

## Common scope-creep patterns in this repo

| Symptom | What to do |
|---|---|
| New `RESEARCH_*_VLM_ESCALATION` env var on a non-VLM connector PR | Remove. File a follow-up issue. |
| `pyproject.toml` adding `pdfplumber`/`tesseract`/`whisper` on a non-PDF/OCR/audio PR | Remove. |
| `.env.example` and `docs/API_KEYS.md` getting unrelated key entries | Remove. |
| Orchestrator `task_kind`/dispatch handler edits on a connector PR | Remove unless the issue asks for orchestrator wiring. |
| Critique-model field additions on a non-critique PR | Remove. |
| Plan/synth prompt rewrites on a non-prompt PR | Remove. |
| Broad contract docs, public API wrappers, or service-surface changes on a narrow MCP/HTTP issue | Split unless the issue explicitly names that contract surface. |

## When in-scope work reveals a needed change elsewhere

Do not fold it in. Instead:

1. Finish the in-scope work.
2. File a follow-up issue describing the discovered need (`alpha-loop add "<description>"`).
3. Leave a brief comment in the current PR pointing to the follow-up issue.

## Reviewer action

Flag scope creep as WARNING in the review summary. Do not block merge if the in-scope code is correct, but record the violation so the pattern is visible. Recommend split-out into a follow-up.
