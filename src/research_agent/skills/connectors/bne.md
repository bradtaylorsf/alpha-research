---
name: bne
description: "BNE Hemeroteca Digital Spanish historical press, scraped through the public BNE Digital search UI."
when_to_use: "Spanish-language primary newspaper sources, Latin-American independence movements, Spanish Civil War, Franco era, post-Franco transition, colonial-era press"
when_not_to_use: "French press -> gallica_search; academic journal context -> persee_search; broad web facts or non-Spanish discovery -> web_search first"
---

# BNE connector

The BNE connector searches Hemeroteca Digital / BNE Digital, the Biblioteca
Nacional de Espana historical newspaper and periodical collection. It is a
Spanish-language primary-source connector for newspaper and periodical evidence,
especially Latin-American independence movements, the Spanish Civil War
(1936-1939), the Franco era, the post-Franco transition, and colonial-era
press.

Use this as the first stop for Spanish colonial-era press before falling back
to broad web discovery.

The connector uses a Playwright scrape. There is no stable public search API,
and the 2024-2025 migration into the newer BNE Digital platform means selectors
can drift. If results suddenly go empty for known-good terms, check
`data/diagnostics/bne/` first and recalibrate result-card, metadata, and
download-link selectors before changing planner behavior.

Pair this connector with the `multilingual-source-handling` strategy when the
goal is written in English, actors have Spanish aliases, or synthesis needs
translated OCR snippets.

## Time-period / era filtering

Use date terms in the query and, when precise range filtering is needed, pass
`fechaDesde` and `fechaHasta`. Useful anchors:

- Latin-American independence movements: late 18th and early 19th centuries,
  using Spanish movement names, leaders, colonial offices, and places.
- Spanish Civil War: `1936`, `1937`, `1938`, `1939`, plus Spanish faction,
  city, front, or newspaper terms.
- Franco era: `1939` through `1975`, with controlled vocabulary around the
  regime, exile, censorship, labor, church, and opposition movements.
- Post-Franco transition: `1975` through early 1980s, with party, union,
  constitution, autonomy, and amnesty terms.

## Query construction

- The canonical search route is `text=<query>` on `/hd/es/results`.
- Prefer Spanish terms: `guerra civil 1936`, `reforma agraria`, `Frente
  Popular`, `exilio republicano`, `independencia Cuba`, `Manila`, `Puerto
  Rico`, or local newspaper titles.
- Use `fechaDesde=` and `fechaHasta=` for date ranges when the planner has a
  known era window.
- Use `localizacion=` when publication place matters, such as Madrid,
  Barcelona, Habana, Manila, Valencia, or Sevilla.
- Keep separate fan-out queries for names, places, newspaper titles, and dated
  event phrases. OCR quality varies across digitized periodicals.

## Knobs available

- `max_results` - default 20. Limits parsed BNE result cards.
- `fechaDesde` - optional lower date bound passed through to BNE Digital.
- `fechaHasta` - optional upper date bound passed through to BNE Digital.
- `localizacion` - optional publication-place filter passed through to BNE
  Digital.

## Anti-patterns

- Do not assume English keywords will recall Spanish historical newspapers.
  Translate, add Spanish aliases, and use period vocabulary.
- Do not treat a search hit as the cited evidence when the claim needs the
  article image or OCR. Fetch the BNE page and preserve
  `metadata["fulltext_url"]` for downstream PDF/OCR extraction.
- Do not discard results only because OCR snippets look noisy. Digitized
  periodicals are image/PDF-first sources with uneven OCR.
- Do not paper over zero hits on common Spanish Civil War queries. That usually
  means selector drift after the BNE Digital migration, not real absence.

## When to fan out

Fan out by Spanish aliases, date windows, publication places, and newspaper
titles. For Civil War work, split event terms from actor names and run separate
queries for 1936-1939 plus city or newspaper title. For Latin-American or
colonial-era work, fan out on Spanish office names, colonies, ports, and place
spellings that changed over time.

Pair with `gallica_search` for French-language European press, `persee_search`
for scholarly context, `openlibrary_search` or `iarchive_search` for books and
scanned monographs, and `multilingual-source-handling` for translation and
alias management.

## Output shape

Each `SearchResult` has `source_kind="bne_search"`, a BNE result/detail URL,
title, snippet, optional `published_at`, and extras:

- `publication`
- `pub_date`
- `place`
- `lang`
- `fulltext_url`

`fetch(url)` returns a `Source` with the same metadata keys. The
`metadata["fulltext_url"]` value is the best available PDF/download/viewer URL
for downstream extraction of the digitized periodical page. `cleaned_text`
contains visible page metadata and OCR/page text when BNE exposes it.

## Auth

No auth or API key is required. The connector uses the shared Playwright
session and rate-limits BNE Digital/Hemeroteca hosts to 0.5 RPS.
