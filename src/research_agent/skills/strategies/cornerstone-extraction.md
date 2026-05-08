---
name: cornerstone-extraction
description: "When the goal names a specific primary document (Mandate for Leadership, a 10-K, a court opinion, a bill), exhaustively index it section-by-section before fanning out to breadth searches."
when_to_use: "goal names a specific document by title, URL, or unambiguous reference; investigations anchored on a single primary source the synthesis must trace back to"
when_not_to_use: "open-ended exploratory queries with no anchor document; broad survey of a topic across many sources"
---

# Cornerstone extraction

When the goal is anchored on a specific named document — **Mandate for Leadership**, a company's **10-K**, a **court opinion**, a **bill text**, an **executive order** — extract that document exhaustively *before* issuing breadth searches. The cornerstone is the spine of the report; everything else fans out from findings inside it.

## Why ordering matters

A breadth-first search ("AI policy 2025") returns surface-level commentary about the cornerstone before you've read the cornerstone itself. You end up citing other people's summaries instead of the source document. Worse, you miss claims that *only* appear in the cornerstone — buried in chapter 23, section 4, footnote 12 — because no breadth search will surface them by topic keyword. Read the primary first, then fan out into specifics.

## Procedure

1. **Identify the cornerstone URL or DOI.** If the goal names the document but not the URL, resolve it explicitly (one targeted search, not a topic dragnet). Save the canonical URL — every finding will cite back to it with a section breadcrumb.
2. **Fetch the full document.** Use the connector that owns this artifact: `edgar` for SEC filings, `congress` for bill text / committee reports, `courtlistener` for opinions, `fedregister` for executive orders / final rules, generic web fetch for advocacy reports / academic papers. Long PDFs (>~50 pages) get chunked per #206 — once that lands, lean on the chunker; until then, paginate manually.
3. **Walk the document in order.** Sections for short documents; chapters for books / multi-hundred-page reports; opinions for court decisions (majority → concurrences → dissents); items for an 8-K. Don't jump around — you'll miss connective tissue.
4. **Emit findings tagged with a section breadcrumb.** Format: `Mandate for Leadership > Ch. 3 (Central Personnel Agencies) > p. 87`. Every finding must carry the breadcrumb so synthesis can trace each claim back to its exact location and reviewers can verify in seconds.
5. **Only after the index is complete, fan out.** Now issue breadth searches that drill into specific findings: "Who is Russell Vought" (named in the cornerstone), "OPM Schedule F litigation" (a specific proposal in the cornerstone), "Heritage Foundation budget 2024" (publisher of the cornerstone). The cornerstone tells you *what to fan out on*; without reading it first, you're guessing.

## Output discipline

- **Every finding from the cornerstone gets the breadcrumb.** Anonymous "the report says..." claims are unreviewable.
- **Findings that *contradict* something in the cornerstone are flagged**, not silently merged. The cornerstone is one source — a contradicting source doesn't override it without `triangulation` evidence.
- **The synthesis section structure should mirror the cornerstone's structure** when the goal is "summarize / analyze the cornerstone." When the goal is broader ("policy landscape, with the cornerstone as one input"), the cornerstone becomes one section among many, but its findings still cite back with breadcrumbs.

## Pairing with other strategies

- **#206 (chunked PDF handling, when shipped):** Long cornerstones (Mandate for Leadership is ~900 pages) require chunked extraction. Until #206, document the chunking strategy in the run log so reviewers can see what was skipped.
- **`modern-policy-era-filtering`:** Apply era filters to the *fan-out searches*, not the cornerstone fetch — the cornerstone is whatever the goal names, even if it's old.
- **`triangulation`:** Single-cornerstone-sourced claims are by definition single-sourced. For claims the synthesis elevates to the executive summary, find a second independent source that confirms or contradicts the cornerstone's assertion.

## Anti-patterns

- Skimming the cornerstone for keywords matching the goal and citing only those passages — misses the document's argument structure and the proposals that don't share the goal's vocabulary.
- Issuing 20 breadth searches before reading the cornerstone — you'll spend the budget on commentary about the document rather than the document itself.
- Citing the cornerstone with no breadcrumb (`"per Mandate for Leadership"`) — reviewers can't verify; treat as an unsupported claim.
- Treating a Wikipedia summary or a news article *about* the cornerstone as a substitute for fetching the cornerstone itself — these are derivative sources, not primary.
