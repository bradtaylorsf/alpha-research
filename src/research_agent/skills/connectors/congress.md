---
name: congress
description: "Congress.gov v3 — bills, members, committees, hearings, congressional record. Searching without an era keyword in the query returns 110th-Congress (2007) hits ranked above modern bills."
when_to_use: "federal legislation, bill text and status, member voting record, committee membership, House/Senate hearings, Congressional Record floor speeches"
when_not_to_use: "executive branch policy → fedregister; lobbying disclosures → lda; campaign finance → fec; SEC filings → edgar"
---

# Congress connector

Congress.gov full-text search across five resource kinds. Default `kind=bill`.

## Time-period / era filtering (CRITICAL — relevance trap)

The connector does **not** currently filter by congress number. You must include the era in the query string, otherwise the API ranks oldest-first and modern bills sink.

| Congress | Period | Era |
|---|---|---|
| 117 | Jan 2021 – Jan 2023 | Biden, Inflation Reduction Act, IIJA |
| 118 | Jan 2023 – Jan 2025 | Biden 2nd half |
| 119 | Jan 2025 – Jan 2027 | **Trump 2nd term — current** |
| 120 | Jan 2027 onward | future |

**For Project 2025 / Trump 2nd-term policy, include "119th Congress" or "2025" in the query.** For Biden-era IRA / IIJA work, include "117th Congress" or "2022".

## Query construction

- Prefer keyword phrases over full bill titles. "Inflation Reduction Act 117" beats "An Act to provide for reconciliation pursuant to title II of S. Con. Res. 14".
- Add the era keyword (`117`, `118`, `119`, or a year like `2022`) when the topic spans decades. Without it, `Inflation Reduction Act` returns 110th-Congress (2007) procedural resolutions ranked above the 2022 Biden law.
- For member lookups, full name + state works best (`Elizabeth Warren MA`); for committees, the formal name (`Senate Banking`).

## Knobs available

- `kind` — one of `bill`, `member`, `committee`, `hearing`, `congressional-record`. Default `bill`.
- `max_results` — capped at the API page size (250). Default 20.
- `timeout` — seconds; default 15.

## Anti-patterns

- ❌ `search("Inflation Reduction Act")` — returns 110th-Congress (2007) procedural resolutions, not the 2022 Biden law. Add `117` or `2022` to the query.
- ❌ Searching the full long-form title — Congress.gov FTS scores keyword overlap; verbose titles dilute the signal.
- ❌ Using `kind=bill` to find a member's voting record. Use `kind=member` to resolve the bioguide ID, then fan out to vote-history sources.
- ❌ Treating a `kind=bill` SearchResult as authoritative bill text — see fan-out below.

## When to fan out

`kind=bill` results carry a permalink to congress.gov but **bill text is not auto-fetched** (see #193). After search, emit a follow-up `fetch()` task per bill ID to pull the full text / summary before any synthesis claim cites the bill's contents. Hearings have synthesized permalinks (`/congressional-hearings/{c}th-congress/...`) that only resolve through this connector's classifier — don't paste them into a browser expecting a public HTML page.

## Output shape

Each `SearchResult` carries `url`, `title`, `snippet`, `published_at` (introduced/action date), and `extras` with the resource-specific identifiers (`bill_id`, `bioguide_id`, `committee_code`, `jacket_number`, etc.). The body / vote text / committee report lives behind a separate fetch.

## Auth

Shared `DATA_GOV_API_KEY` (also used by `fec`). Loud failure if unset.
