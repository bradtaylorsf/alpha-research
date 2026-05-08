---
name: modern-policy-era-filtering
description: "Apply recency filters across all connectors when investigating current-era policy. Government APIs default to relevance, not recency — without this strategy, queries surface decades-old precedent ranked above current actions."
when_to_use: "goal mentions current admin, this term, recent, 2025+, ongoing policy, Project 2025, Trump 2nd term, executive orders this year"
when_not_to_use: "historical comparison, precedent research, longitudinal trend analysis, anything explicitly requesting older eras"
---

# Modern-policy-era filtering

When the goal is about current/recent federal policy (default era for the 119th Congress / Trump 2nd term: **on or after 2025-01-20**), apply the directives below across every connector that fires. Government search APIs default to relevance ranking, not recency — without this filter the planner surfaces Clinton-era statutes and Obama EOs ranked above current actions.

## Per-connector recency directives

Knob shape varies. Some connectors take a real date parameter; the rest only accept query-string scoping. Read carefully — passing `since=2025-01-20` to a connector that doesn't accept it is a silent no-op.

| Connector | Mechanism | Modern-era value |
|---|---|---|
| `congress` | **No `congress=` knob.** Include era in `query` string. | Append `"119th Congress"` or `"2025"` |
| `edgar` | **No date knob.** Filter `SearchResult.published_at >= 2025-01-20` post-hoc. | `published_at >= 2025-01-20` |
| `fedregister` | Real `since=` knob (ISO `YYYY-MM-DD`). | `since="2025-01-20"` |
| `courtlistener` | **No `date_filed_after` knob.** Append CourtListener's FTS date scope to `query`. | Append `decided:[2025-01-20 TO *]` |
| `fec` | **No `cycle=` knob.** Include cycle year in `query`. | Append `"2024"` (presidential), `"2026"` (midterms) |
| `usaspending` (when shipped) | `fiscal_year_min` | `2025` |
| `nonprofits` (when shipped) | `tax_year` | most recent available |
| `sanctions` (when shipped) | `added_after` | `2025-01-20` |

## Executive Order numbering

Trump 2nd-term EOs start at roughly **EO 14148** (2025-01-20). Filter `extras.document_number` (Federal Register) or the EO number in the title `> 14150` to catch the current term while excluding the 2017–2021 EO range (≈13765–14013) and Biden's range (≈14014–14147).

## When to override

Override the recency filter only when the goal explicitly asks about historical comparison, original-intent / precedent research, or longitudinal trends. If the goal mentions a specific older administration, scope to *that* era's window instead — don't drop recency entirely.

## Anti-pattern

Searching `"AI regulation"` with no recency filter returns Clinton-era Telecommunications Act fragments and Obama-era NIST guidance ranked above Biden's 2023 EO 14110 and Trump's 2025 actions. The 2026-05-08 Project 2025 overnight surfaced 0/24 citations from the 119th Congress and 4/24 from federalregister.gov post-2025-01-20 because this strategy was not invoked. Always pass the era filter on current-policy investigations.

## Cross-stacking with other strategies

This strategy is purely additive — it constrains the search window without changing query semantics, so it composes cleanly with `cornerstone-extraction` (extract the cornerstone in full, then apply era filters when fanning out) and `triangulation` (require ≥2 *modern-era* sources, not 1 modern + 1 historical).
