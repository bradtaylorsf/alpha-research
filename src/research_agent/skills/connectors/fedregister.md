---
name: fedregister
description: "Federal Register — executive orders, agency rules, notices, presidential documents. Without `since`, results skew historical; for Trump 2nd-term EOs use since=2025-01-20."
when_to_use: "executive orders, proposed rules, final rules, agency notices, presidential proclamations, Federal Register-published policy"
when_not_to_use: "legislation → congress; SEC rules in plain text → edgar; lobbying around a rule → lda; court challenges to a rule → courtlistener"
---

# Federal Register connector

Searches federalregister.gov across all document types. Default endpoint paginates at 2,000 max — narrow by `since` + `agencies` rather than asking for huge `max_results`.

## Time-period filtering (CRITICAL)

Without `since`, the API returns matches across the full archive (1990s onward) and modern policy gets buried.

| Investigation era | `since` value |
|---|---|
| Trump 2nd term (current) | `2025-01-20` |
| Biden admin | `2021-01-20` to `2025-01-19` |
| Trump 1st term | `2017-01-20` to `2021-01-19` |
| Obama 2nd term | `2013-01-20` to `2017-01-19` |

For "current administration" queries, **always** pass `since="2025-01-20"`. The 2026-05-08 Project 2025 overnight surfaced only 4/24 citations from federalregister.gov post-inauguration because date filtering was missing.

## Where executive orders live

Executive orders are NOT a top-level filter — they appear in the Federal Register under `document_type="presidential_document"` (subtype "Executive Order"). The connector does not currently expose a `document_type` knob; filter post-hoc on `extras.document_type` from the SearchResult, or include "executive order" in the query string.

## Query construction

- Pair the topic with a verb the agency would use ("rescind", "amend", "implement", "withdraw") — Federal Register prose is formulaic.
- Use the agency's common acronym in the query when known (`EPA`, `OSHA`) — the FTS hits agency_names.
- For rules-vs-notices triage, include the type word ("proposed rule", "final rule", "notice").

## Knobs available

- `since` — `date`, `datetime`, or ISO string `YYYY-MM-DD`. Maps to `conditions[publication_date][gte]`.
- `agencies` — list of agency slugs (e.g. `["environmental-protection-agency", "department-of-labor"]`). Repeated as `conditions[agencies][]`.
- `max_results` — capped at 200 per page; default 20.
- `timeout` — seconds; default 15.

## Anti-patterns

- ❌ `search("executive order on AI")` without `since="2025-01-20"` — returns Obama and Trump-1 EOs ranked above current ones.
- ❌ Passing `agencies=["EPA"]` (acronym) — slugs are kebab-case full names: `["environmental-protection-agency"]`. The connector silently drops unknown slugs.
- ❌ Asking for `max_results=1000` — the page cap is 200; narrow by date instead.
- ❌ Treating the `snippet` as the rule's binding text. Use `fetch()` to pull the full document HTML.

## When to fan out

Search returns metadata + a federalregister.gov URL. For the binding rule text, executive-order language, or comment-period analysis, emit a `fetch()` per document. `extras.document_number` is the stable identifier for citation.

## Output shape

Each `SearchResult` carries `url` (federalregister.gov), `title`, `snippet` (abstract), `published_at` (publication date), `source_kind="fedregister"`, and `extras` with `agencies` (list of names), `document_type`, `document_number`, and `significant` (bool, OIRA-flagged).
