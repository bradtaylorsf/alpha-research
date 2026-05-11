---
name: nara
description: "US National Archives Catalog OPA v2 - declassified federal records, military records, photos, and digitized archival descriptions. Requires NARA_API_KEY."
when_to_use: "declassified federal records; NARA holdings by Record Group; military records; photographs and textual archival descriptions; older primary-source federal records"
when_not_to_use: "current or in-process federal records; post-2010 diplomatic cables; Federal Register rules -> fedregister_search; Congress materials -> congress_search; general web pages -> web_search"
---

# NARA Catalog connector

Searches the US National Archives Catalog OPA v2 API at
`catalog.archives.gov/api/v2/records/search`. It covers archival
descriptions and digital objects: declassified federal records, military
records, photographs, audiovisual material, and item/file-unit/series/record
group metadata.

## Time-period / era filtering

NARA is strongest for archival holdings that have been accessioned,
processed, and cataloged. Add a year, date range, administration, war, or
declassification term to the query when the subject spans decades:

- `Vietnam War declassified 1968`
- `"Project Blue Book" available online`
- `RG 59 Vietnam 1972`

Do not use this connector for in-process records. post-2010 diplomatic cables
and other recent agency records usually are not digitized or publicly
cataloged yet.

## Query construction

`q` accepts normal keyword search and phrases. Prefer a short topic plus era
or agency clue over long natural-language questions. For agency holdings,
include the Record Group when known:

| Record Group | Meaning |
|---|---|
| `RG 59` | General Records of the Department of State |
| `RG 263` | Records of the Central Intelligence Agency |

Record Group numbers are NARA's top-level holding identifiers. They are not
evidence by themselves; they tell the planner which agency collection a hit
belongs to and whether a fan-out should stay inside that agency context.

## Knobs available

- `max_results` - number of hits to request, capped by the connector.
- `page` - 1-indexed page converted to OPA's `offset`.
- `available_online` - sends `availableOnline=true|false`; use true when the
  operator needs digitized objects now.
- `type_of_materials` - maps to `typeOfMaterials`, such as `Textual Records`,
  `Photographs and other Graphic Materials`, `Sound Recordings`, or `Moving Images`.
- `result_types` - maps to `resultTypes`, such as `item`, `fileUnit`, `series`,
  `recordGroup`, or `description`.
- `record_group` - maps to `recordGroupNumber`.
- `sort` - passes OPA's `sort` field when the API supports the field.

## Anti-patterns

- Do not query for in-process or very recent records. Post-2010 cables and
  many recent agency files are typically not digitized and may not be
  transferred to NARA yet.
- Do not treat a restricted record as available evidence. When
  `general_records_information.access_restriction` or `accessRestriction`
  says `Restricted`, the body may be FOIA-only or reading-room-only.
- Do not use broad queries like `Vietnam` without an agency, era, or material
  filter; Catalog ranking can bury the useful archival series.
- Do not assume every hit has a digital object. Many records are metadata-only.

## When to fan out

Fan out after a promising hit when:

- `digital_objects` includes a PDF, image, audio, or video URL that should be
  fetched by the relevant downstream connector.
- The hit is a series or file unit and the goal needs item-level evidence;
  search again with the series title, NAID, Record Group, and narrower topic.
- The access restriction is `Restricted`; surface that status rather than
  claiming the underlying body is available. FOIA or reading-room follow-up is
  a human/outbound workflow, not automatic connector work.

## Output shape

Each `SearchResult` has `source_kind="nara_search"`, a public Catalog detail
URL, title, snippet, `published_at` when production dates are parseable, and
`extras` with:

- `nara_record_id`
- `record_group`
- `series_title`
- `scope_and_content`
- `access_restriction`
- `use_restriction`
- `digital_objects`

`fetch()` re-queries the API by NAID and returns a `Source` whose metadata
populates `nara_record_id`, `record_group`, `series_title`, and
`scope_and_content` when NARA surfaces those fields. Detail-page fetches also
kick off a best-effort Wayback save.

## Auth

API key required. Set `NARA_API_KEY`; the connector sends it as the
`x-api-key` header. Request a read-only key by emailing
`Catalog_API@nara.gov` with your name and the email address for the key.
Registration takes about 24h. The default limit is 10,000 queries/month per
key, and this connector enforces 0.5 RPS to stay well under that limit.

When `NARA_API_KEY` is unset, `search()` logs
`would need NARA_API_KEY; skipping` and returns `[]`. The smoke command prints
`would need NARA_API_KEY; live test skipped` and exits 0.
