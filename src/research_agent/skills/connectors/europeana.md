---
name: europeana
description: "Europeana multilingual European cultural-heritage metadata across museums, libraries, and archives; API key required."
when_to_use: "European archival, museum, library, visual-art, manuscript, book, newspaper, photograph, and cultural-heritage discovery across many countries and languages"
when_not_to_use: "US-only archival discovery better covered by loc_search, dpla_search, or nara_search; French national-library deep dives better started with gallica_search; workflows requiring guaranteed full text rather than metadata"
---

# Europeana connector

Europeana aggregates cultural-heritage metadata from European museums,
libraries, galleries, and archives. Use `europeana_search` when a question needs
cross-country European discovery or when the likely holding institution is not
known yet. It is especially useful for multilingual collection discovery before
fan-out to the original institution shown in `metadata["edmIsShownAt"]`.

Europeana is metadata-first. It can point to original provider pages, previews,
rights statements, and language/country facets, but it should not be treated as
the full text or OCR surface for every item.

## Time-period / era filtering

Europeana metadata varies by provider. Date fields can be years, ranges, or
localized display strings, so put era words and years directly into the query:
`Algerian war 1954`, `guerre d'Algerie 1958`, `Solidarnosc 1980`, or
`Spanish civil war 1936`.

For national or language-specific research, pair the era with native-language
terms. English keywords against a French, German, Spanish, Dutch, or Italian
subject often miss records whose metadata was supplied in the local language.

## Query construction

- Start with compact native-language subject phrases plus a year, person,
  place, institution, or object type.
- Use `lang` when the item language matters. The connector sends it as
  `qf=LANGUAGE:fr`, so `lang="fr"` filters to French-language items.
- `qf=COUNTRY:France` filters to French institutions or providers, not
  necessarily French-language item metadata. Use country and language as
  different concepts.
- Pair with the `multilingual-source-handling` strategy when results will need
  translation downstream. The connector can surface multilingual metadata, but
  synthesis should preserve original-language titles and translate carefully.
- For France-heavy national-library material, try `gallica_search` in parallel
  or as a follow-up because Gallica can be deeper than the Europeana aggregate.

## Knobs available

- `max_results` - sent as Europeana `rows`, capped at 100 per request.
- `lang` - optional language filter sent as `qf=LANGUAGE:<lang>`, for example
  `fr`, `de`, `es`, `it`, or `nl`.

## Anti-patterns

- Do not query only in English for a French/German/Spanish subject. Switch to
  native-language keywords for better recall.
- Do not treat `COUNTRY` and `LANGUAGE` as interchangeable. `COUNTRY:France`
  scopes institutions/providers; `LANGUAGE:fr` scopes item language metadata.
- Do not assume Europeana records contain full OCR or full object content. Use
  `edmIsShownAt` to follow the holding institution page when evidence requires
  local context.
- Do not assume licenses are uniform across institutions. The license field is
  in `Source.metadata["rights"]` and varies widely across European providers.

## When to fan out

Fan out when Europeana identifies likely holding institutions, collections, or
language/country clusters. Use the Europeana result to collect `dataProvider`,
`country`, `language`, `rights`, and `edmIsShownAt`, then fetch the provider
page or a national connector for richer metadata, OCR, or images.

Fan out by language for multilingual topics: query English plus native-language
variants (`Algerian war`, `guerre d'Algerie`, `Algerienkrieg`) and keep results
separate enough that downstream translation can preserve provenance.

## Output shape

Each `SearchResult` has `source_kind="europeana_search"`, a public
`https://www.europeana.eu/en/item/...` URL, title, snippet, optional
`published_at`, and `extras` containing:

- `europeana_id`
- `dataProvider`
- `country`
- `language`
- `rights`
- `edmIsShownAt`
- `provider`
- `type`
- `year`
- `edmPreview`

`fetch(url)` returns a `Source` with the same source kind and required metadata:
`metadata["europeana_id"]`, `metadata["dataProvider"]`, `metadata["country"]`,
`metadata["language"]`, `metadata["rights"]`, and
`metadata["edmIsShownAt"]`. The markdown body summarizes the Europeana record
and includes the provider item URL when present.

## Auth

Requires `EUROPEANA_API_KEY`. Since 2025-05-28, API key registration lives in
the Europeana account area under **Manage API keys**. The connector sends the
key as `?wskey=<key>` to `https://api.europeana.eu/api/v2/search.json`; do not
use `api_key=`.

Without `EUROPEANA_API_KEY`, `europeana_search` and
`_smoke-tool europeana_search` skip cleanly. The connector enforces 1 RPS.
