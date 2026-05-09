---
name: iarchive
description: "Internet Archive content archive (books, audio, film, web collections) via advancedsearch.php. Distinct from Wayback Machine — that lives in tools/archive.py. Pick mediatype: texts (digitized books), audio (period radio / oral history), movies (archival film), web."
when_to_use: "digitized historical books and periodicals (especially pre-1923 public domain); period radio broadcasts and oral histories; archival film and television; topic-curated IA collections"
when_not_to_use: "live web pages → web_fetch; modern news → news_search; a Wayback snapshot of a known URL → tools/archive.py (not this connector); academic papers → arxiv_search or scholar_search"
---

# Internet Archive connector

Searches `archive.org/advancedsearch.php` (Lucene-style query syntax, JSON
output) across IA's content archive. Each result is an *item* in IA's
sense — a book, an audio recording, a film, or a web-collection snapshot
— addressable by an `archive.org/details/<identifier>` permalink.

**Internet Archive ≠ Wayback Machine.** The Wayback Machine is IA's *web*
archive (point-in-time snapshots of arbitrary URLs); this connector is
IA's *content* archive (curated digitized media). The Wayback path lives
in `tools/archive.py` and is exposed via `archive_today` smoke + the
fire-and-forget save inside `web_fetch`. Don't conflate them.

## Time-period / era filtering

IA's `date` field is Lucene-indexed and supports range queries. To narrow
to a specific century, decade, or year:

- `date:[1850 TO 1900]` — 19th-century items only
- `date:[1942-01-01 TO 1945-12-31]` — WWII-era items
- `date:1968` — exactly 1968

`year:` is also indexed for items with a single canonical year. Combine
with a free-text query for topic + era: `Pullman Strike date:[1894 TO
1900]`. For audio / radio specifically, the IA collections are organized
by decade (e.g. `collection:radio-1940s`) — see *Query construction*.

## Query construction

`advancedsearch.php` accepts Lucene-style field operators. The most
useful for this agent:

| Operator | Use |
|---|---|
| `creator:"<name>"` | Author / performer / publisher |
| `date:<value or range>` | Item date (single or range) |
| `subject:<term>` | LoC-like subject heading |
| `collection:<id>` | A curated IA collection slug (`librivoxaudio`, `oralhistories`, `radio-1940s`, `feature_films`, `prelinger`, `gutenberg`, …) |
| `language:<iso>` | `eng`, `fra`, … |

Combine with the free-text `q` and the connector's `mediatype` knob:

```
search("Pullman Strike", mediatype="texts")
# → q=Pullman Strike AND mediatype:texts
```

Direct field operators inside the query string also work — useful for
`creator:` / `collection:` filters that the connector doesn't expose as
a knob:

```
search('"Studs Terkel" collection:oralhistories', mediatype="audio")
```

For pre-1923 public-domain text, prefer `mediatype=texts` plus a date
range. For period radio / oral histories, `mediatype=audio` paired with
a decade collection (`collection:radio-1940s`,
`collection:oralhistories`) gives the cleanest hit set.

## Knobs available

- `mediatype` — one of `texts`, `audio`, `movies`, `web`. Default
  unset (no `mediatype:` filter, returns everything). Unknown values
  are logged at WARN and dropped — they don't crash the search.
- `max_results` — default `20`. The IA API page size is generous;
  values up to a few hundred are fine, but pagination via `page` is
  cleaner for >100 hits.
- `page` — 1-indexed page number, default `1`. Use with `max_results`
  for sweep searches over a large topic.
- `timeout` — seconds; default `15`.

## Anti-patterns

- ❌ Treating `archive.org/details/...` URLs as a substitute for the
  Wayback Machine. Wayback URLs (`web.archive.org/web/<timestamp>/<url>`)
  are *not* handled by this connector — they pass through `web_fetch`
  on the generic httpx + trafilatura path.
- ❌ Conflating IA item metadata with the underlying media. A `texts`
  item's `details/` page describes the book; the actual full text lives
  in a derivative file at `archive.org/download/<id>/<id>_djvu.txt`.
  Use `metadata['fulltext_url']` (set on `fetch()` for `texts` items)
  to fan out — don't hand-paste the detail URL into a text reader.
- ❌ Fanning out to `web_fetch` against IA detail pages without setting
  a polite `RESEARCH_USER_AGENT`. IA throttles aggressively when traffic
  has no contact info; the connector ships a UA but `web_fetch`
  fallbacks may not.
- ❌ Searching IA for *current* news / blog posts. IA's content archive
  is curated and back-cataloged — it lags weeks-to-decades behind
  publication. Use `news_search` for recent reporting.
- ❌ Using `mediatype=web` expecting Wayback-like point-in-time URL
  snapshots. `mediatype=web` returns IA's curated web *collections*
  (e.g. an Election 2020 web archive bundle), not Wayback captures.

## When to fan out

After a `mediatype=texts` hit, follow `metadata['fulltext_url']` via
`web_fetch` so the loop's PDF / text path can extract findings against
the actual book content rather than the IA item summary.

After a `mediatype=audio` hit, route `metadata['audio_files']`
(canonical mp3 / flac URLs) to the audio transcription pipeline. The
IA detail page on its own only carries item-level metadata (title,
date, creator), not the spoken content.

After a `mediatype=movies` hit, the canonical mp4 / ogv URLs are listed
under `metadata['files']` (IA's manifest format) — fan-out wiring for
video transcription is currently TODO at the orchestrator level; cite
the detail page for now.

For HTML detail pages (any mediatype), the connector kicks off a
fire-and-forget Wayback save on the first uncached fetch so the URL
stays citeable even if IA later removes the item.

## Output shape

`SearchResult` from `search()`:
- `url`: `https://archive.org/details/<identifier>`
- `title`: item title
- `snippet`: creator — date — mediatype — description (truncated)
- `published_at`: parsed IA `publicdate` (when item was added to IA)
- `extras`: `identifier`, `mediatype`, `downloads`, `creator`, `date`

`Source` from `fetch()`:
- `cleaned_text`: markdown-rendered title / creator / date / mediatype
  / description summary
- `metadata['identifier']`, `['mediatype']`, `['downloads']`,
  `['creator']`, `['date']`, `['publicdate']`, `['collection']`
- `metadata['fulltext_url']` — set when `mediatype == "texts"` and a
  derivative full-text file is present
- `metadata['audio_files']` — set when `mediatype == "audio"`, list of
  `archive.org/download/<id>/<file>` URLs for mp3 / flac files

## Auth

None required. IA's public APIs are open. A polite User-Agent is
strongly recommended — the connector reads `RESEARCH_USER_AGENT` and
falls back to a generic `research-agent/0.1` string. Without a real
contact, IA may throttle aggressively.
