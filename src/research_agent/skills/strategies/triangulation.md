---
name: triangulation
description: "For high-stakes claims (executive summary, contested facts, legal/financial assertions), require ≥2 independent sources before marking confirmed; distinguish single-sourced from confirmed in synthesis."
when_to_use: "investigative reporting, fact-tracking, legal analysis, financial verification, any claim destined for the executive summary or that contradicts another finding"
when_not_to_use: "uncontested background facts, well-known historical events, claims sourced directly from a primary cornerstone where attribution alone suffices"
---

# Triangulation

For any claim a synthesis elevates to the **executive summary**, marks as **contested**, or treats as a **legal / financial / criminal-conduct assertion**, require **≥2 independent sources** before flagging the claim as `confirmed`. Single-sourced high-stakes claims must be flagged `inconclusive` or `single-sourced` — never `confirmed`.

## What "independent" means

Two sources are **independent** when they have:

1. **Different publishers** — not two outlets republishing the same wire story (AP, Reuters, AFP). A Reuters story syndicated to 40 newspapers is *one* source, not 40.
2. **Different chains of provenance** — a press release quoted in two outlets is one source (the press release). An anonymous source quoted by two reporters at the same publication is one source.
3. **Different evidentiary basis** — a primary document plus a secondary report citing that document is *one and a half* sources for the document's contents; the secondary report only counts as a second source if it adds independent reporting (interviews, additional documents, on-the-record confirmation).

Examples of *non*-independent pairs that look independent:
- Two news outlets both citing "according to a White House statement" — the statement is one source.
- A Wikipedia article and a news article that Wikipedia cites — Wikipedia is downstream of the news article.
- Two academic papers by overlapping co-authors using the same dataset — the dataset is one source.
- A 10-K and an 8-K from the same issuer disclosing the same fact — the issuer is one voice; you have one corporate source, not two.

Examples of genuinely independent pairs:
- A Federal Register publication of an executive order *and* a court opinion in litigation challenging that EO that quotes the operative text.
- An FEC filing showing a contribution *and* an independent reporter's interview with the contributor confirming the donation.
- A 10-K disclosing an acquisition *and* the target company's own SEC filing or board minutes confirming.

## Procedure

1. **Classify the claim.** Tag every finding as `executive-summary` (lead claim), `contested` (contradicts another finding), `legal-or-financial` (court holding, regulatory action, dollar amount, criminal allegation), or `background` (uncontested context). Background claims do not need triangulation; the other three categories do.
2. **Count independent sources.** For a high-stakes claim, list the sources. If ≥2 are independent by the definition above, mark the claim `confirmed`.
3. **Single-sourced high-stakes claims:** mark `single-sourced` (or `inconclusive` if the source is weak — anonymous, paywalled-rumor, social media). Schedule a follow-up task to find a corroborating second source. Do not silently elevate to the executive summary.
4. **Conflicting sources:** **surface both with the conflict noted.** Do not drop either. Format: `Source A reports X (URL, date); Source B reports Y (URL, date); conflict unresolved.` Schedule a follow-up to find a third source that breaks the tie or to fetch the underlying primary document if both are secondary.
5. **In the synthesis output:** every executive-summary claim carries an explicit `[confirmed]`, `[single-sourced]`, or `[contested]` tag. Reviewers should be able to see the evidentiary status without re-reading the source list.

## Anti-patterns

- Counting two reposts of the same wire story as two sources — they are one (provenance test).
- Treating an organization's press release plus a news write-up of that press release as confirmation — the news write-up is downstream.
- Dropping the lower-quality source in a conflict to avoid having to discuss it — the conflict is itself a finding; surface it.
- Marking a single-sourced claim `confirmed` because the source is "reputable" — reputation is not independence; the executive-summary tag is reserved for ≥2 independent sources regardless of source prestige.
- Failing to schedule the follow-up task on `single-sourced` claims — they will quietly persist into the final report unless someone tries to upgrade them.

## Pairing with other strategies

- **`cornerstone-extraction`:** A claim sourced *only* to the cornerstone is single-sourced by definition. For executive-summary claims, find a second independent source confirming or contradicting.
- **`modern-policy-era-filtering`:** When triangulating modern-policy claims, both sources should be modern-era. A modern claim "confirmed" by a 2008 source is not actually confirmed for the current policy environment.
