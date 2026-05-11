---
name: cspan
description: "C-SPAN Video Library for post-1979 US political broadcast video, scraped through the public search UI with transcript text returned in cleaned_text."
when_to_use: "US political broadcast video, congressional floor speeches, committee hearings, presidential events, White House briefings, public affairs programs, transcript-backed speech evidence"
when_not_to_use: "statutory bill text -> congress; campaign finance -> fec; lobbying disclosures -> lda; broad web facts or non-video discovery -> web_search"
---

# C-SPAN connector

The C-SPAN connector searches the C-SPAN Video Library for US political
broadcast video, including congressional floor speeches, committee hearings,
presidential events, White House briefings, campaign events, and public-affairs
programming. Coverage is strongest after the modern Video Library era and is
useful for post-1979 US political broadcast research.

This connector uses a Playwright scrape of `https://www.c-span.org/search/`.
There is no public API for this workflow. Transcripts MUST land in
`Source.cleaned_text`, not `Source.metadata["transcript"]`, because downstream
retrieval, FTS5, and embeddings only see searchable source text there.

## Time-period / era filtering

Use years and administration names in the query when the topic spans decades:
`Project 2025 2024`, `unitary executive 2025`, `Senate hearing AI 2025`, or
`immigration policy briefing 2018`. For chamber-scoped floor proceedings, add
the C-SPAN URL filter `&type=Senate` or `&type=House` to narrow the search to
one chamber.

## Query construction

- Prefer compact phrases that would appear in a transcript or program title.
- Combine the person, issue, and year when the phrase is common.
- Use hearing names, committee topics, bill nicknames, and administration
  labels as separate fan-out queries.
- Use `type=Senate` or `type=House` when the research question is specifically
  about floor speeches or chamber proceedings.

## Knobs available

- `max_results` - default 20. Limits parsed C-SPAN video search cards.
- `type` - optional C-SPAN URL filter, commonly `House` or `Senate`.

## Anti-patterns

- Do not expect transcripts for all events. Some clips have no transcript text
  yet; surface that gap clearly instead of returning empty `cleaned_text`.
- Do not stash transcript text in metadata. `metadata["transcript"]` is
  intentionally not part of this connector because it would be invisible to
  `research search`.
- Do not cite the search hit alone when the claim depends on exact remarks.
  Fetch the program or clip URL and cite the transcript-bearing `Source`.
- Do not use C-SPAN for statutory text or roll-call status when Congress.gov is
  the source of record.

## When to fan out

Fan out by speaker, hearing title, chamber, date, and policy phrase. For a
hearing, search the hearing title plus the witness name, then use
`metadata["speakers"]` to filter or prioritize clips that include one witness's testimony.
Pair with `congress_search` for the bill/hearing record,
`fec_search` for campaign finance context, and `fedregister_search` for agency
policy documents.

## Output shape

Each `SearchResult` has `source_kind="cspan_search"`, a C-SPAN program or clip
URL, title, snippet, optional `published_at`, and extras:

- `program_id`
- `air_date`
- `duration_seconds`
- `video_url`

`fetch(url)` returns a `Source` with `metadata["program_id"]`,
`metadata["air_date"]`, `metadata["duration_seconds"]`,
`metadata["video_url"]`, and `metadata["speakers"]`. Transcript segments are
rendered into `Source.cleaned_text` under `## Transcript`; there is no
`metadata["transcript"]` key.

## Auth

No auth or API key is required. The connector uses the shared Playwright
session and rate-limits C-SPAN hosts to 0.5 RPS.
