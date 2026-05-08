---
name: courtlistener
description: "CourtListener — federal/state opinions, RECAP PACER dockets, oral arguments. Default kind=opinions; without a court/date constraint in the query, modern circuit decisions get buried under 19th-century cases."
when_to_use: "case law, court opinions, federal/state appellate decisions, PACER docket filings, SCOTUS oral arguments, citation lookup"
when_not_to_use: "agency adjudications → fedregister; legislation → congress; campaign-finance complaints (FEC enforcement) → fec; private settlements → not on CourtListener"
---

# CourtListener connector

Free Law Project search across opinions, RECAP (PACER mirror) dockets, and oral arguments. Default `kind=opinions`.

## Index selection

| `kind` | What it is | Use for |
|---|---|---|
| `opinions` | Court opinions / case law | Holdings, precedent, judicial reasoning |
| `dockets` | RECAP filings (PACER mirror) | Briefs, motions, exhibits, lower-court trial record |
| `oral_arguments` | Audio transcripts | SCOTUS / circuit court oral argument text |

`opinions` and `dockets` are different worlds — an opinion is the appellate court's published ruling; a docket is the trial-court paperwork. If the question is "what did the judge decide" use `opinions`; if it's "what evidence was filed" use `dockets`.

## Query construction (citation parsing)

CourtListener understands several citation forms in the query string:

- **Bluebook reporter cites** — `"576 U.S. 644"` (Obergefell), `"410 F.3d 786"` — these route the query at the FTS layer.
- **Case name** — `"Obergefell v. Hodges"`. Use `v.` (with the period) — `vs` and `versus` reduce hit rate.
- **Date filtering** — append `decided:[2020-01-01 TO *]` to scope to the modern era. Without a date scope, 19th-century cases with overlapping keywords ("commerce", "due process") rank up.
- **Court filtering** — append `court:scotus`, `court:ca9` (9th Circuit), `court:nysd` (SDNY) to constrain jurisdiction. Use the connector's lower-case court slug, not the formal name.

## Knobs available

- `kind` — `opinions` (default), `dockets`, or `oral_arguments`. Raises `ValueError` on unknown values.
- `max_results` — default 20.
- `timeout` — seconds; default 15.

## Anti-patterns

- ❌ `search("Bostock", kind="opinions")` without a date scope — the FTS surfaces same-name 19th-century cases. Use `"Bostock decided:[2018-01-01 TO *]"`.
- ❌ Searching `kind="opinions"` for trial-court motions — those are RECAP filings; switch to `kind="dockets"`.
- ❌ Using full citation prose like "the Supreme Court of the United States" — use `court:scotus` instead.
- ❌ Asking for `kind="filings"` — not a valid kind. The connector raises `ValueError`.

## When to fan out

Opinion `SearchResult`s carry a CourtListener permalink, snippet, and case metadata. For full opinion text or the cited authorities, emit a `fetch()` per opinion. Docket results often only carry the case caption — to inspect a specific filing, fan out via the docket's child-entry URLs.

## Output shape

Each `SearchResult` carries `url` (courtlistener.com), `title` (case caption), `snippet` (excerpt with FTS-highlighted terms), `published_at` (date filed / decided), `source_kind="courtlistener"`, and `extras` keyed to the kind (court slug, citation list, docket number, judge names, status).

## Auth

Optional `COURTLISTENER_API_TOKEN` raises rate limits. Without it, the connector hits the public endpoint with stricter throttling.
