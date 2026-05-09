---
name: loc
description: "Library of Congress digital collections via the unified loc.gov JSON API. The `collection` knob routes to chronicling-america (US newspapers 1690–1963 with OCR text), prints, manuscripts, recordings, or maps. Default: search across all surfaces. No auth."
when_to_use: "historical US newspapers (Chronicling America via collection knob), prints/photographs, manuscripts, sound recordings, maps, or any LoC digital collection item; primary-source citations for pre-1964 American history; IIIF image references"
when_not_to_use: "post-1963 US news → news_search or web_search; older European press → gallica or europeana; bibliographic-only catalog records (use the worldcat-style sources instead); modern federal documents → fedregister, congress, edgar"
---

# LoC connector

Unified search across the Library of Congress digital collections via the loc.gov JSON API. Single connector, multiple surfaces selected via the `collection` knob.

## Time-period / era filtering (CRITICAL)

Chronicling America covers **1690–1963 only**. The collection's date range is hard — running a date-restricted query past 1963 against `collection: chronicling-america` returns zero results, with no useful error.

| Era / target | Right tool |
|---|---|
| US newspapers, 1690–1963 | `loc_search` with `collection: chronicling-america` |
| US press, 1964–present | `news_search` (recent week) or `web_search` |
| European press, any era | gallica (BnF) or europeana via `web_search` site-scoping until those connectors ship |
| Pre-1964 photographs / prints | `loc_search` with `collection: prints` |
| Pre-1964 manuscripts / personal papers | `loc_search` with `collection: manuscripts` |
| Sound recordings (LoC holdings) | `loc_search` with `collection: recordings` |
| Maps (LoC Geography & Map Division) | `loc_search` with `collection: maps` |

## Query construction

- Keep queries short and keyword-led — the loc.gov FTS scores keyword overlap, not narrative phrasing.
- For chronam, place + event + decade is a reliable pattern: `"Pullman strike Chicago 1894"`, `"yellow fever New Orleans 1853"`.
- For prints/manuscripts, name + role works: `"Frederick Douglass portrait"`, `"Lincoln cabinet meeting"`.
- Don't quote multi-word phrases unless you genuinely need exact-match — quoting reduces recall sharply against OCR-derived text.

## Knobs available

- `collection` — one of `chronicling-america`, `prints`, `manuscripts`, `recordings`, `maps`. Omit to search all surfaces. Unknown values are treated as raw collection slugs under `/collections/<slug>/`.
- `max_results` — default 20; the loc.gov API caps a single response at 100.
- `page` — 1-indexed; the connector turns this into the `sp=<page>` query param.
- `timeout` — seconds; default 15.

## Anti-patterns

- ❌ Date filter past 1963 against `collection: chronicling-america` — returns nothing because the collection ends in 1963.
- ❌ Pointing fetch URLs at `chroniclingamerica.loc.gov` — that standalone API was retired on 2025-08-04. The connector's classifier intentionally rejects this host so a stale link doesn't silently 404.
- ❌ Stashing per-page chronam OCR text in `Source.metadata` — it must land in `cleaned_text` so FTS5 / embeddings can retrieve it.
- ❌ Treating low-confidence pre-1900 OCR as authoritative — flag findings derived from blurry/skew-rotated newspaper scans for manual confirmation.
- ❌ Quoting the full headline of a 19th-century newspaper article — OCR errors guarantee zero hits. Search 2–3 distinctive content words.

## When to fan out

- A `loc_search` hit's `url` points at the canonical loc.gov page (`/item/<id>/` or `/resource/<lccn>/<date>/...`). The loop turns top hits into follow-up `web_fetch` calls; those are host-routed back into this connector's `fetch()` so chronam pages get their per-page OCR materialized into `cleaned_text` automatically.
- For an era-spanning question, run the same query twice with different `collection` values (e.g. `chronicling-america` for the press coverage and `prints` for the visual record) — the planner can compare the two retrieval surfaces side by side.

## Output shape

`SearchResult.url` / `.title` / `.snippet` / `.published_at` carry the obvious fields. `extras` adds:

- `collection` — the knob the search was scoped to (or `""`).
- `item_id` — the loc.gov item / resource ID (URL or short id).
- `image_url` — first thumbnail / preview URL when present.
- `mime_type`, `original_format`, `online_format`, `partof`, `site` — surfacing the LoC's own classification so downstream re-rankers can disambiguate.

`Source.cleaned_text` — for chronam pages, this is the concatenated per-page OCR text fetched from the page's `fulltext_service` URL. For all other surfaces, it's a markdown-ish render of title + dates + description + subjects. **OCR text is not in metadata — it's in `cleaned_text` so retrieval can see it.**

`Source.metadata` for image-bearing surfaces (prints, maps):

- `image_url` — direct URL to the highest-resolution thumbnail / service image LoC exposes.
- `image_iiif_manifest` — synthesized `https://www.loc.gov/item/<id>/manifest.json` URL for IIIF Presentation 2.x consumers (only set for `/item/...` pages, not `/resource/...`).

## Auth

None. The loc.gov JSON API is free and unauthenticated — set `RESEARCH_USER_AGENT` (any value) so the connector identifies itself politely, but no key is required and `research doctor` does not check anything for this connector.
