---
name: wikidata
description: "Wikidata raw SPARQL for entity IDs, biography facts, family ties, occupations, places, and other structured relationships."
when_to_use: "historical figures, public figures, organizations, places, occupations, family or employment ties, party/position history, and QID/PID resolution"
when_not_to_use: "unstructured articles, narrative interpretation, current news, or natural-language questions that have not yet been translated into SPARQL"
---

# Wikidata connector

Wikidata Query Service returns structured graph data from Wikidata. Use it as a
power-mapping primitive when the investigation needs stable entity IDs, dates,
family relationships, occupations, offices, employers, affiliations, locations,
or cross-wiki sitelinks.

v1 takes raw SPARQL queries only. Natural-language to SPARQL translation is a
follow-on. Build queries using the Wikidata Query Service documentation:
https://www.wikidata.org/wiki/Wikidata:SPARQL_query_service

## Time-period / era filtering

Wikidata statements can have qualifiers, ranks, and incomplete dates. For
historical research, bind date properties directly when rough chronology is
enough, and use statement paths (`p:`, `ps:`, `pq:`) when a start/end date or
source qualifier matters.

For broad historical classes, constrain by date or place early and always add a
small `LIMIT`. Large unconstrained human/entity queries are expensive.

## Query construction

- Use `wikidata_search` only with raw SPARQL in `payload.query`.
- Add `SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }`
  when results need readable labels.
- Common power-mapping patterns:
  - `P31` instance of, with `Q5` human.
  - `P19` place of birth.
  - `P39` position held.
  - `P102` member of political party.
  - `P108` employer.
- For entity pages, follow with `wikidata_fetch` on a `https://www.wikidata.org/wiki/Q...`
  URL to retrieve labels, descriptions, P-claims, and sitelinks.

Example:

```sparql
SELECT ?item ?itemLabel ?born WHERE {
  ?item wdt:P31 wd:Q5;
        wdt:P19 wd:Q90;
        wdt:P569 ?born.
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT 25
```

## Knobs available

- `max_results` - client-side truncation of returned SPARQL bindings. This does
  not reduce server work; include `LIMIT` in the SPARQL query.
- `timeout` - request timeout in seconds.

## Anti-patterns

- Do not pass natural-language questions such as "Who was born in Paris?" v1
  does not translate them.
- Do not emit queries that can return more than 10,000 rows. WDQS kills or
  throttles expensive queries; use `LIMIT` aggressively.
- Do not use a large `max_results` as a substitute for SPARQL constraints.
  Server-side `LIMIT`, date filters, place filters, and property constraints
  are the controls that matter.
- Do not treat every claim as fully sourced evidence. Wikidata is a structured
  index; fetch linked source material before making high-stakes claims.

## When to fan out

Fan out from Wikidata when a query resolves QIDs or relationship leads that
need evidentiary support. Typical follow-ups are:

- `wikidata_fetch` for entity metadata and sitelinks.
- `web_fetch` on linked references or sitelinks when the synthesis needs a
  citable narrative source.
- Domain connectors for evidence: `congress_search`, `fec_search`,
  `courtlistener_search`, `edgar_search`, or archival connectors when the QID
  identifies a target but not the underlying record.

## Output shape

`search()` returns one `SearchResult` per binding row that contains a Wikidata
entity. `extras.entity_id` contains the QID, and `extras.bindings` contains the
normalized SPARQL binding values.

`fetch()` returns a `Source` whose metadata includes:

- `entity_id` - the QID.
- `label` and `description` - English when available, otherwise the first
  localized value.
- `claims` - dict of `P...` property IDs to value lists.
- `sitelinks` - dict of wiki/site IDs to titles and URLs when present.

## Auth

No auth or API key is required.

The rate-limit shape is unusual: WDQS allows about 60 seconds of query CPU time
per IP plus User-Agent per minute, not a fixed requests-per-second cap. One slow
query can consume the whole minute. The connector tracks cumulative query wall
time over a rolling 60-second window as a CPU-time proxy and backs off before it
submits more SPARQL.

Wikimedia requires a descriptive project-identifying User-Agent. This connector
sends `research-agent/0.1 (+https://github.com/bradtaylorsf/muckwire; contact: ...)`
and honors HTTP 429 `Retry-After`. Policy reference:
https://meta.wikimedia.org/wiki/User-Agent_policy
