---
name: gallica
description: "Gallica/BnF SRU XML search for French national-library newspapers, books, manuscripts, maps, and other digitized primary sources."
when_to_use: "French-language primary sources, BnF digitized newspapers, books, manuscripts, maps, images, press coverage, colonial history, and European archival research"
when_not_to_use: "non-French archives better covered by LOC, Trove, Europeana, DPLA, NARA, or Internet Archive; article full text that is not present in Gallica metadata; broad web discovery before structured archive search"
---

# Gallica connector

Gallica is the Bibliotheque nationale de France digital archive for newspapers,
books, manuscripts, maps, images, periodicals, and other digitized French
primary sources. Use `gallica_search` when the target has a French, colonial,
Francophone, European, bibliographic, or historical press angle and BnF metadata
is a better first pass than generic web search.

SRU returns XML, not JSON. This is the only XML-response connector in the tree
and it uses `xml.etree.ElementTree` to parse SRU/Dublin Core namespaces. Do not
copy JSON-first connector patterns such as `nonprofits.py`.

Endpoint: `gallica.bnf.fr/services/engine/search/sru?operation=searchRetrieve&version=1.2&query=<CQL>`.
This is not `gallica.bnf.fr/SRU?operation=searchRetrieve`; that older/common
handoff endpoint is wrong for this connector.

## Time-period / era filtering

Gallica is strong for nineteenth- and twentieth-century French publications,
including colonial-era newspapers and books. Put years directly in the CQL when
the subject spans eras. For the Algerian war, pair topic terms with dates such
as `1954`, `1956`, `1958`, or `1962`; for older newspapers, include a decade,
publication title, or place name.

Gallica is a French-language primary-source surface. Pair it with the
`multilingual-source-handling` strategy when the goal is in English or when the
research target has multilingual names.

## Query construction

- Plain keyword search uses CQL: `gallica all "<keywords>"`.
- Author search can use `dc.creator any "<author>"`.
- Date filters can use `dc.date >= "1956"` or combine date terms with keyword
  CQL when the planner needs narrower era control.
- Keep queries compact. French spellings and aliases matter: try both
  `guerre d'Algerie` and topic-specific French phrases where relevant.
- Prefer `gallica_search` before `web_search site:gallica.bnf.fr`; the SRU
  endpoint returns structured Dublin Core fields and ARK identifiers.

## Knobs available

- `max_results` - default 20. The SRU `maximumRecords` page size is capped at
  50 server-side, and larger values are silently overridden by Gallica.
- `timeout` - HTTP timeout in seconds; default 15.

## Anti-patterns

- Do not call `gallica.bnf.fr/SRU?...`; use
  `gallica.bnf.fr/services/engine/search/sru?...`.
- Do not expect JSON. The connector parses XML through
  `xml.etree.ElementTree`.
- Do not request `maximumRecords` above 50 and assume more results will arrive.
- Do not treat a metadata hit as full OCR text. Fetch returns a Dublin Core
  metadata card; full-page OCR/image follow-up is a separate task.
- Do not drop the ARK. `metadata["ark"]` is the canonical BnF citation handle.

## When to fan out

Fan out when a broad historical subject has multiple French terms, spellings,
people, or publication titles. For example, run separate searches for a topic
phrase, a person/organization, and a date-filtered CQL variant. Keep fan-out
bounded because SRU pages are capped at 50 records and Gallica relevance varies
by collection type.

Fan out to `web_fetch` or a future OCR/full-text path only after selecting
records with useful ARKs. For non-French comparative coverage, pair Gallica with
LOC, Trove, Europeana, Internet Archive, Wikisource, or OpenLibrary depending
on the country, language, and source type.

## Output shape

Each `SearchResult` has `source_kind="gallica_search"`, a public Gallica ARK
URL, title, snippet, optional `published_at`, and extras:

- `ark`
- `dc:type`
- `dc:date`
- `dc:language`
- `dc:source`
- `dc:identifier`
- `dc:creator`

`fetch(url)` accepts Gallica ARK permalinks and returns a `Source` whose
`metadata` repeats those Dublin Core fields. The ARK in `metadata["ark"]` is
the persistent identifier to preserve in citations and follow-up tasks.

## Auth

No auth or API key is required. The connector sets `User-Agent` from
`RESEARCH_USER_AGENT` when configured and rate-limits requests to 1 RPS.
