---
name: openlibrary
description: "Open Library book metadata and Internet Archive scan IDs; use for books, editions, ISBN/OCLC/LCCN lookup, and HathiTrust enrichment seeds."
when_to_use: "digitized books, bibliographic metadata, Internet Archive scan identifiers, ISBN/OCLC/LCCN discovery, HathiTrust enrichment upstream"
when_not_to_use: "newspapers or articles → loc/trove/gallica/openalex; full-text transcription → iarchive/web_fetch after resolving an IA scan; bulk catalog harvesting → Open Library dumps"
---

# Open Library connector

Open Library exposes book/work metadata through `openlibrary.org/search.json`.
This connector is a cheap, no-auth bibliographic lookup path and routinely
backfills HathiTrust gaps by surfacing ISBN, OCLC, and LCCN identifiers.

Endpoint: `openlibrary.org/search.json?q=<query>&fields=<focused-list>`.
Always set `fields=`. Without it, `search.json` can return 500KB+ payloads
with large lists that are not useful for this agent's routing decision.

## Time-period / era filtering

Open Library is a bibliographic catalog, not a date-indexed primary-source
search engine. Put the relevant year or era in the query when the title/topic
is broad (`Pullman Strike 1894`, `Battle of Algiers 1957`, `reconstruction 1868`).
The returned `first_publish_year` is metadata for the work, not proof that the
scan or edition was published in that exact year.

## Query construction

- Use title/topic plus year for historical events: `Pullman Strike 1894`.
- Use author + title when the title is generic.
- Use known identifiers directly when available: ISBN, OCLC, or LCCN values are
  strong seeds for follow-up HathiTrust checks.
- Prefer `openlibrary_search` before `web_search site:openlibrary.org`; the
  connector already uses the JSON endpoint and focused `fields=`.

## Knobs available

- `max_results` — default 20, passed as the endpoint `limit`.
- `timeout` — request timeout in seconds; default 15.

## Anti-patterns

- Do not bulk-download metadata with `search.json`. Open Library provides
  separate bulk data dumps for catalog-scale work.
- Do not omit `fields=` or use `fields=*`; the default/full payload is too
  large and can trigger slow or unstable responses.
- Do not treat an Open Library hit as full text. It is a metadata record; fan
  out to Internet Archive or HathiTrust when full text or rights status matters.

## When to fan out

Fan out when a hit has useful identifiers:

- `ia_scan_id` values point to Internet Archive scan pages such as
  `https://archive.org/details/<scan-id>`.
- `oclc`, `isbn`, and `lccn` values feed A13 HathiTrust enrichment downstream
  for rights/full-text status.
- Multiple high-relevance works can each produce separate HathiTrust lookups,
  but keep the fan-out bounded to avoid catalog crawling.

## Output shape

Each `SearchResult` has an Open Library work/book URL, title, snippet, and
`extras` with `isbn`, `oclc`, `lccn`, `ia_scan_id`, `edition_count`, and
`author_keys`. `fetch()` returns a `Source` whose `metadata` repeats those keys
so HathiTrust enrichment can read identifiers from the fetched source.

## Auth

No auth or API key is required. Open Library gives identified User-Agents a
higher 3 RPS tier versus 1 RPS anonymous/default traffic; the connector sets
`User-Agent` from `RESEARCH_USER_AGENT` when configured.
