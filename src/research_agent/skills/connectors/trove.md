---
name: trove
description: "Trove/National Library of Australia metadata search for Australian newspapers, books, photos, magazines, and oral histories; API key required; metadata-only by default."
when_to_use: "Australian historical research, colonial-era newspapers, World War records, photos/images, books, magazines/newsletters, oral histories, and cultural collection metadata from 1803-present"
when_not_to_use: "non-Australian archives; workflows that require automatic full-text harvesting; bulk article-text downloads; US/EU government records better covered by NARA, LOC, Gallica, Congress, or other dedicated connectors"
---

# Trove connector

Trove is the National Library of Australia discovery layer for Australian
newspapers, gazettes, books, pictures, magazines, maps, oral histories, and
other cultural collection metadata. Use `trove_search` when the research target
has an Australian historical, cultural, press, local-history, or World War angle
and structured Trove metadata is a better first pass than generic web search.

This connector is deliberately metadata-only. It returns titles, dates, Trove
URLs, IDs, zones/categories, holding-library metadata when surfaced, and a
`fulltext_url` pointer for a deliberate operator-controlled follow-up. It does
not auto-fetch article text or use `include=articletext`.

## Time-period / era filtering

Trove covers Australian material from 1803-present and is especially strong for
colonial-era newspapers, Federation debates, state and local history, and World
War records. Put years or date ranges directly in the query when the subject is
ambiguous across eras, for example `White Australia Policy 1901`, `ANZAC 1915`,
or `date:[1939 TO 1945] Darwin bombing`.

## Query construction

- Start with compact phrases plus an Australian place, person, institution, or
  era marker. `White Australia Policy 1901` beats a paragraph-length prompt.
- Trove supports index-style query syntax; use title/creator/date terms when
  the target is precise.
- Prefer direct `trove_search` before a `site:trove.nla.gov.au` web query when
  a `TROVE_API_KEY` is configured; the API returns structured metadata and IDs.
- For newspapers, combine the topic with state, newspaper title, or decade
  terms when common words swamp relevance.

## Knobs available

- `max_results` - total results to return after connector-side trimming. Default 20.
- `category` - v3 category list, comma string or list. Default is
  `book,newspaper,image,magazine`.
- `zone` - legacy-friendly category alias. `zone=newspaper|picture|book|sound`
  maps to v3 `category=newspaper|image|book|music`; use `sound` for oral
  histories/audio.
- `sortby` - Trove sort mode such as relevance or date-descending values
  supported by the API.
- `timeout` - HTTP timeout in seconds. Default 30.

## Anti-patterns

- Enabling full-text retrieval as a workflow default is the fastest path to
  losing your key.
- Do not request `include=articletext`, `include=all`, or bulk article bodies
  unless the operator has explicit NLA permission for that run.
- Do not send the key as a URL parameter in this project. Auth is the
  `X-API-KEY: <key>` header, NOT URL parameter auth.
- Do not treat OCR article text as if this connector fetched it. Results are
  metadata cards unless a separate, deliberate fetch path is chosen.

## When to fan out

Fan out by category/zone when the subject may appear in multiple collection
types: `newspaper` for press coverage, `picture` for photographs and images,
`book` for catalogued books/reports, `magazine` for periodicals, and `sound`
for oral histories/audio. Use follow-up `trove_fetch` or `web_fetch` only on
selected records whose metadata is relevant. Keep article-body retrieval out of
default plans.

## Output shape

Each `SearchResult` has `source_kind="trove_search"`, a public Trove URL, title,
snippet, optional `published_at`, and extras:

- `trove_id`
- `zone`
- `category`
- `pub_date`
- `holding_libraries`
- `fulltext_url`
- `metadata_only`

`fetch(url)` returns a `Source` with a short metadata markdown card. It does not
inline newspaper article text even if an upstream fixture or response contains
an article-text field.

## Auth

API key required: set `TROVE_API_KEY` after applying through the Trove API form
in a Trove account. This connector sends it as `X-API-KEY: <key>` and never as a
query parameter.

Critical ToS warning: keys expire after 12 months and require the renewal email
loop. Since February 2025, NLA has been reported to cancel keys WITHOUT WARNING
for users who download full text rather than metadata. This connector defaults
to metadata-only. DO NOT auto-fetch full-text bodies or make full-text retrieval
the default behavior.
