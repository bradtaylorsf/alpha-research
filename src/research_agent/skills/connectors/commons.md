---
name: commons
description: "Wikimedia Commons free media files; always inspect imageinfo license metadata before citing or reusing."
when_to_use: "public-domain or CC-licensed images, audio, video, diagrams, maps, portraits, and other reusable media"
when_not_to_use: "encyclopedia text, source texts, page prose, or non-media facts - use Wikipedia/Wikisource/web_search instead"
---

# Commons connector

Wikimedia Commons is the Wikimedia movement's free-media repository, not an
encyclopedia. Use `commons_search` when the research task needs reusable media
evidence or assets: photographs, illustrations, scans, audio, video, maps, and
other files.

The connector uses the MediaWiki Action API on `commons.wikimedia.org`: keyword
search goes through `action=query&list=search&srsearch=...`, and each File hit is
enriched with `prop=imageinfo&iiprop=url|mime|mediatype|extmetadata`. The
`imageinfo.extmetadata` block is where Commons exposes license and attribution
fields.

## Time-period / era filtering

Commons search does not have a first-class date-range knob in this connector.
Put era terms directly in the query: `Algerian war 1957 photograph`, `Pullman
Strike 1894`, `Paris Commune 1871 engraving`. For historical media, include the
event name plus the year, location, and media type.

## Query construction

- Search for media concepts, not article claims: `Battle of Algiers 1956
  photograph`, `Houari Boumediene portrait`, `Algerian War map`.
- Add a media-type word when useful: `photograph`, `poster`, `map`, `audio`,
  `video`, `scan`, `diagram`.
- Prefer English keywords first, then fan out with local-language terms when the
  subject is multilingual.
- Distinguish Commons from Wikipedia and Wikisource. The API family is the same,
  but Commons returns media files and file metadata; Wikipedia/Wikisource are for
  encyclopedia/source text workflows.

## Knobs available

- `max_results` - client-side result cap; default 20.

## Anti-patterns

- Do not search Commons for text content or prose evidence. Use Wikipedia,
  Wikisource, LOC, Internet Archive, or `web_search` for that.
- Do not cite or reuse a Commons result until the license fields are present.
  `Source.metadata["license"]` is critical for derivative work decisions: CC0,
  public domain, CC-BY, and CC-BY-SA have different obligations.
- Do not treat thumbnails as the original media. Use `metadata["original_url"]`
  for the full file and `metadata["thumb_url"]` only for preview/display.

## When to fan out

Fan out when a query has multiple possible media angles: event + year, person +
portrait, location + map, and local-language terms. For reuse workflows, fetch
candidate files and filter by `metadata["license"]` / `metadata["license_short"]`
before synthesis or export.

## Output shape

Each `SearchResult` has a Commons File page URL, title, snippet, and `extras`
containing `mime_type`, `original_url`, `thumb_url`, `author`, `license`, and
`license_short`. `fetch()` returns a metadata-card `Source` with the same fields
in `Source.metadata`; downstream consumers should read
`Source.metadata["license"]` before producing derivative work.

## Auth

No auth or API key. Requests use a project-identifying User-Agent and a shared
1 RPS Wikimedia host limiter.
