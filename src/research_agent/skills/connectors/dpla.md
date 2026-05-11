---
name: dpla
description: "DPLA item metadata across US libraries, archives, museums, and cultural-history hubs; key-gated, metadata-first."
when_to_use: "US cultural institution metadata, digitized objects, photographs, manuscripts, books, maps, oral histories, collection discovery across many repositories"
when_not_to_use: "historical newspaper full-text discovery; use loc_search with collection=chronicling-america instead. Also skip for non-US Europeana-style aggregation."
---

# DPLA connector

Digital Public Library of America aggregates item metadata from hundreds of
US cultural institutions and hubs. It is useful when a question needs broad
cross-institution discovery before narrowing to the originating archive,
library, museum, or collection.

`provider` and `dataProvider` are different. `provider` is usually the DPLA
hub or service/content aggregator. `dataProvider` is the originating
institution that holds or supplied the item. Use `dataProvider` for citation
context and institutional provenance when both are present.

## Time-period / era filtering

DPLA is metadata-first, not full-text-first. Era terms must usually be in the
query (`1930`, `New Deal`, `civil rights`, `World War II`) or in the upstream
metadata. Dates live in `sourceResource.date` and may be display strings,
single years, or ranges, so treat date filtering as approximate unless the
source record has explicit begin/end values.

## Query construction

- Start with compact subject phrases plus a place, institution, or era:
  `Maya land claims`, `Japanese American incarceration California`, `Harlem
  Renaissance photographs`.
- Add the originating institution when known, or pass `provider` to scope the
  API (`?provider=New York Public Library`) when you want a specific DPLA hub
  or institution-like provider.
- Prefer object/collection nouns (`map`, `photograph`, `oral history`,
  `manuscript`, `poster`) over broad historical questions.
- When the result points to a provider item URL, fetch the DPLA item page/API
  record first and then follow the provider URL only if the claim requires the
  holding institution's fuller metadata or digital object.

## Knobs available

- `max_results` - capped client-side and sent as DPLA `page_size`.
- `provider` - optional DPLA `?provider=` filter. Use for scoping to a known
  provider/hub such as `New York Public Library`; remember that provider is
  not always the same as the originating `dataProvider`.

## Anti-patterns

- Do not use DPLA as the primary search surface for historical newspapers.
  Coverage is much thinner than Chronicling America. Use `loc_search` with
  `collection=chronicling-america` for US historical newspaper discovery.
- Do not treat a DPLA snippet as evidence of full item text. DPLA usually
  stores metadata and preview URLs, not OCR/full-text transcripts.
- Do not collapse `provider` and `dataProvider`; the former can be a hub, while
  the latter is the institution that supplied the item.
- Do not assume rights are uniform across DPLA. Check `metadata.license` and
  the provider page before using images or transcripts.

## When to fan out

Fan out from DPLA when the broad aggregation finds plausible collections or
institutions. Use the DPLA result to identify `dataProvider`, collection,
rights, dates, and object URL; then fetch the provider item URL for richer
local metadata, OCR, image derivatives, or collection context when needed.

For newspaper-heavy leads, fan out away from DPLA into `loc_search` /
Chronicling America instead of paging deeper in DPLA.

## Output shape

Each `SearchResult` has `source_kind="dpla_search"`, a `https://dp.la/item/...`
URL, title, snippet, date when parseable, and `extras` containing:
`dpla_id`, `provider`, `data_provider`, `license`, `object_url`, and
`is_shown_at`.

Each fetched `Source` has the same `source_kind` and required metadata keys:
`metadata.dpla_id`, `metadata.provider`, `metadata.data_provider`,
`metadata.license`, and `metadata.object_url`. The markdown body summarizes
the DPLA item metadata and includes provider/object URLs when present.

## Auth

Requires `DPLA_API_KEY`. Request a free key with:

```bash
curl -X POST https://api.dp.la/v2/api_key/<your-email>
```

Registration is an HTTP POST, not a web form. Key delivery is typically
instant, but still arrives by email; the 32-character key rides on API
requests as `?api_key=<key>`. Without the key, `dpla_search` and
`_smoke-tool dpla_search` skip cleanly.
