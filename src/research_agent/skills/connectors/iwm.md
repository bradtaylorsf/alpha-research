---
name: iwm
description: "Imperial War Museums public collections — photographs, oral histories/sound, documents, film, objects, and ephemera for twentieth- and twenty-first-century conflict. Browser connector, no public API; diagnostics land in data/diagnostics/iwm/."
when_to_use: "IWM collection records, Battle of Britain, First/Second World War photographs, oral histories, private papers, ephemera, Falklands, post-1945 colonial conflicts including Algeria via UK-held archives"
when_not_to_use: "full-text newspapers -> bne_search/gallica_search; US federal records -> nara_search; broad web context -> web_search; high-volume harvesting"
---

# IWM connector

Imperial War Museums Collections Search across public object records. Use it for
primary-source discovery in IWM-held collections: photographs, sound/oral
history records, documents/private papers, film, art, objects, posters,
souvenirs and ephemera.

This is a read-only public browser connector, not an API. Selectors may drift.
When parsing fails, diagnostic HTML and PNG dumps are written under
`data/diagnostics/iwm/`.

## Time-period / era filtering

IWM coverage is strongest for the First World War, Second World War, post-1945
colonial conflicts including Algeria via UK-held archives, and the Falklands.
Use the public Related Period filter through `related_period` when a period
facet is known:

| Period knob | Use for |
|---|---|
| `First World War` | WWI photographs, diaries, private papers, sound records |
| `Second World War` | Battle of Britain, Home Front, official collections |
| `1945-1989` | Cold War, decolonisation, Algeria-related UK-held archives, Falklands |
| `1990 to the present day` | Gulf War onward and contemporary conflict |

## Query construction

- Use concrete event/place/unit terms: `Battle of Britain`, `Falklands oral history`,
  `Algerian war interview`, `Suez private papers`.
- Filter by Object Category through `object_category` when the public URL
  exposes that facet.
- For oral histories, set `object_category="Sound"` or use the alias
  `object_category="oral histories"`.
- For photographs, set `object_category="Photographs"`.
- For documents, try `object_category="Private papers"` first; also fan out to
  `Books` or `Souvenirs and ephemera` when looking for printed material.
- Add `records_with_media=True` only when the plan needs digitised media; many
  useful catalogue records have no media online.

## Knobs available

- `max_results` — default 20.
- `object_category` — public URL facet
  `filters[webCategory][<category>]=on`; examples: `Photographs`, `Sound`,
  `Private papers`, `Souvenirs and ephemera`, `Film`.
- `related_period` — public URL facet
  `filters[periodString][<period>]=on`; examples: `First World War`,
  `Second World War`, `1945-1989`, `1990 to the present day`.
- `records_with_media` — when true, adds `media-records=records-with-media`.
- `style` — public view style such as `list` or `image`.
- `page_size` — public `pageSize` parameter.

## Anti-patterns

- High-volume queries. Keep public browser traffic to the 0.5 RPS polite cap.
- Treating catalogue metadata as the full archival item. Fetch the item page and
  cite the IWM object record, then fan out to OCR/PDF/audio tools only if a
  public media URL is surfaced.
- Using the top-level site search. Robots disallow `/search/`; this connector
  uses the allowed `/collections/search` path only.
- Assuming every record is digitised. IWM explicitly includes many records with
  no media available online.

## When to fan out

Pair `iwm_search` with `nara_search`, `ukna_search`, `gallica_search`,
`bne_search`, `persee_search`, and `commons_search` for cross-archive conflict
research. For a Battle of Algiers / Algerian War plan, combine `iwm_search`
with French-language connectors and use the optional translation pass for
non-English sources.

## Output shape

Each `SearchResult` has `source_kind="iwm_search"`, an IWM item URL, a title,
snippet, and `extras` where available:

- `object_type`
- `period`
- `collection`
- `catalogue_id`
- `production_date`
- `creator`

`fetch()` returns a `Source` with visible page text and the same metadata keys.
The AC-required keys are `metadata["object_type"]`, `metadata["period"]`,
`metadata["collection"]`, and `metadata["catalogue_id"]` when the item page
surfaces them.

## Auth

No auth. No login, form submission, bypass, or protected content access. Public
Playwright scrape only.
