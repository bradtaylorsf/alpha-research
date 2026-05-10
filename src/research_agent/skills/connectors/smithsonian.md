---
name: smithsonian
description: "Smithsonian Open Access object metadata and media across Smithsonian museums, archives, libraries, research centers, and the National Zoo via api.data.gov."
when_to_use: "digitized Smithsonian collection objects, museum artifacts, historic images, 3D assets, object provenance, unit-specific collection metadata"
when_not_to_use: "research papers, exhibition text, blog posts, education pages, or non-object Smithsonian web content; use web_search/web_fetch or openalex_search instead"
---

# Smithsonian Open Access connector

Smithsonian Open Access searches digitized collection objects and associated
metadata across Smithsonian units. It is useful for primary-source object
records, media availability, object provenance, object type, and licensing
status. It is not a full-text search over Smithsonian articles, exhibitions, or
research publications.

## Time-period / era filtering

The API does not expose a dedicated era knob in this connector. Put the period
directly in the query when relevance depends on it: `Apollo 11 1969`,
`Civil War 1863`, `New Deal poster 1930s`, or `World War II aircraft`.

For object-heavy topics, pair the era with the object class or collection unit
when known. `Apollo 11 command module NASM` is usually stronger than just
`Apollo 11`; `Japanese screen FSG` narrows toward the Freer/Sackler collections.

## Query construction

- Use short object-oriented phrases: object name, mission/event/person, date,
  and material/type.
- Add a Smithsonian unit acronym when the target museum is known. Common unit
  codes include `NMAH` for National Museum of American History, `NASM` for
  National Air and Space Museum, `FSG` for Freer Gallery of Art and Arthur M.
  Sackler Gallery, `NPG` for National Portrait Gallery, `SAAM` for Smithsonian
  American Art Museum, `NMNH` for National Museum of Natural History, and
  `NMAAHC` for National Museum of African American History and Culture.
- Treat `unit_code` as a routing clue, not a complete museum name. Surface both
  the code and `data_source` in synthesis when possible.
- For derivative work or publication, inspect `metadata.license` before using
  images. `CC0` is broadly reusable; `CC-BY-NC`, `Usage conditions apply`, or
  `Restricted` require more care.

## Knobs available

- `max_results` — result count, default 20, capped by the connector before
  calling the API.

## Anti-patterns

- Do not use this for non-object content: Smithsonian research papers,
  exhibition essays, news pages, learning-lab text, or blog posts are outside
  Open Access object search.
- Do not assume every result has downloadable media. The API may return object
  metadata without an image URL.
- Do not treat all Smithsonian metadata as permission for reuse. License and
  rights conditions vary by object and media; carry `metadata.license` forward.
- Do not strip the unit code from citations. Smithsonian collection context is
  distributed across many museums and archives.

## When to fan out

After search, fetch the strongest object URLs before citing details. `fetch()`
returns the record metadata, summary, object details, `unit_code`,
`object_type`, `image_url`, and `license`.

Fan out by unit when a topic spans museums: for example, pair `NMAH` for
history artifacts with `NASM` for aerospace objects, or `FSG`/`SAAM`/`NPG` for
art and portrait materials.

Use complementary connectors when the research question needs non-object
evidence: `openalex_search` for papers, `loc_search`/`iarchive_search` for
books and archival texts, `commons_search` for reusable media on Wikimedia, and
`web_fetch` for Smithsonian pages outside Open Access records.

## Output shape

Each `SearchResult` has `source_kind="si_search"`, a public `si.edu/object/...`
URL, title, snippet, and `extras` with `unit_code`, `object_type`, `image_url`,
`license`, `record_id`, `smithsonian_id`, `data_source`, and media count when
available.

Each fetched `Source` has `source_kind="si_search"` and the required
`metadata.unit_code`, `metadata.object_type`, `metadata.image_url`, and
`metadata.license` fields. The cleaned text is a compact markdown object record
with summary and selected object-detail fields.

## Auth

Uses shared `DATA_GOV_API_KEY`, the same api.data.gov key used by `fec` and
`congress`. When unset, the connector falls back to `DEMO_KEY`, which is only
safe for low-volume smoke checks and is throttled to roughly 40 requests per
hour per IP. The connector enforces a 1 RPS process-local gate.
