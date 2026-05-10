---
name: ukna
description: "UK National Archives Discovery catalogue metadata for Foreign Office, War Office, Colonial Office, and other UK archival records; no auth; descriptions only."
when_to_use: "UK government and imperial/colonial archival research, especially Foreign Office, War Office, Colonial Office, declassified, military, diplomatic, and colonial-era catalogue discovery"
when_not_to_use: "full-text document search, OCR over scanned records, contemporary news, non-UK archives, or workflows that need the body text of physical records without a separate download/fetch step"
---

# UK National Archives Discovery connector

The UKNA connector searches Discovery, The National Archives catalogue. It is a
metadata and description index for UK government records and records held by
other archives. Use `ukna_search` before generic web search when the target has
a British state, empire, military, diplomatic, or colonial-record angle.

The Discovery API is a Beta API; schema may drift. The connector logs a clear
warning on schema mismatch and keeps parsing useful fields instead of crashing.

## Time-period / era filtering

`covering_dates` is a free-text field, not ISO. Surface it raw in metadata and
let downstream parsing decide whether `1952-1959`, `c 1948-1963`, `20th
century`, or `n.d.` is usable for the task.

For colonial-era work, add places, administrative names, and years directly to
the query: `Mau Mau Kenya 1950s`, `Aden emergency 1967`, or `Cyprus Colonial
Office 1955`.

## Query construction

- Start with compact subject + place + era terms. `Mau Mau Kenya 1950s` beats a
  paragraph-length investigative prompt.
- Catalogue references follow `<DEPT>/<SERIES>/<PIECE>` in operator shorthand,
  commonly displayed with spaces such as `CO 537/...`.
- Useful department prefixes:
  - `CO` - Colonial Office records, often core for empire and colonial
    administration.
  - `WO` - War Office records, military operations and army administration.
  - `FO` - Foreign Office records, diplomacy and overseas political reporting.
- If a search is too broad, fan out by department prefix plus subject:
  `CO Mau Mau Kenya`, `WO Kenya security operations`, `FO Kenya emergency`.

## Knobs available

- `max_results` - API page size and connector-side cap. Default 20.
- `page` - Discovery API page number when walking deeper result pages.
- `timeout` - HTTP timeout in seconds. Default 20.

## Anti-patterns

- Do not expect full-text search across actual document bodies. UKNA Discovery
  indexes catalogue descriptions, NOT contents. The actual records are often
  physical files, born-digital accessions, or scanned-image PDFs.
- Do not treat `covering_dates` as machine-normalized dates.
- Do not assume an open catalogue description means a digital document is
  immediately downloadable.
- Do not use this connector for US federal records; use `nara_search`.

## When to fan out

Fan out when the topic could sit in more than one department or series. For a
colonial conflict, run separate focused searches across `CO`, `WO`, and `FO`
with the place and years. Use follow-up `ukna_fetch` on selected catalogue
records to capture the metadata card before citing a record in synthesis.

Pair with `gallica_search`, `trove_search`, `loc_search`, or `nara_search` when
the same colonial-era story has French, Australian, US, or international
archival traces.

## Output shape

Each `SearchResult` has `source_kind="ukna_search"`, a Discovery detail URL,
title, snippet, and extras:

- `catalogue_reference`
- `covering_dates`
- `held_by`
- `scope_content`
- `department`
- `catalogue_level`
- `record_id`
- `url_parameters`
- `score`

`fetch(url)` returns a `Source` metadata card with the required metadata keys
`catalogue_reference`, `covering_dates`, `held_by`, and `scope_content`. It
does not return full archival document text.

## Auth

No auth. No API key is required. The connector enforces a process-local 1 RPS
polite rate for Discovery API requests.
