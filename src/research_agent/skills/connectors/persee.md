---
name: persee
description: "Persee French academic journal articles in humanities and social sciences, scraped through the public search UI."
when_to_use: "French academic literature, humanities and social sciences journals, colonial-era studies, Annales-school history, Francophone scholarly article leads"
when_not_to_use: "English-first keyword discovery; broad web facts; books/newspapers better served by gallica_search, openlibrary_search, iarchive_search, or web_search"
---

# Persee connector

Persee is a French academic-journal platform with strong coverage in the
humanities and social sciences. It is useful for scholarly context and article
leads on French, Francophone, colonial, and social-history topics, including
colonial-era studies and Annales-school history.

The connector uses a Playwright scrape of the public search UI because Persee's
public API is partial and less reliable for the planner's article-discovery
workflow. Prefer `persee_search` before `web_search site:persee.fr` when the
target is likely covered by French academic journals.

Pair this connector with the `multilingual-source-handling` strategy when the
goal is in English, the topic has French names, or the downstream synthesis
needs translated snippets.

## Time-period / era filtering

Persee covers older and modern journal literature, but the search UI does not
expose a stable structured era filter through this connector. Put years,
decades, places, movements, and event names directly in the query. For Algerian
war work, combine French topic terms with dates such as `1954`, `1956`, `1958`,
or `1962`.

## Query construction

- Use French keywords first: `guerre d'Algerie`, `decolonisation`, `Algerie
  francaise`, `FLN`, `pieds-noirs`, `harkis`.
- Keep queries compact. Persee is FR-first; exact French concepts usually beat
  long English descriptions.
- Try people, journal vocabulary, and historiographic labels as separate
  fan-out queries.
- If a DOI appears in `metadata["doi"]`, preserve it and consider a downstream
  `web_fetch` fan-out against the DOI or article URL to pull the full PDF or
  richer open-access article text when available.

## Knobs available

- `max_results` - default 20. Limits the number of parsed article cards from
  the Playwright-rendered search page.

## Anti-patterns

- Do not query English-only terms against French academic literature and treat
  sparse results as absence of scholarship. Translate or add French aliases.
- Do not use Persee for newspaper primary sources; use `gallica_search` first.
- Do not assume every article exposes a DOI. The connector only fills
  `metadata["doi"]` when the page surfaces one.
- Do not cite a search hit as full article evidence if the claim requires the
  article body. Fetch the article URL, and fan out to PDF/web fetch when needed.

## When to fan out

Fan out when the research target has multilingual names, colonial-era place
names, organizations, or historiographic terms. Run separate French searches
for the event, key actors, journal vocabulary, and a year-bounded variant. Pair
with Gallica for French primary sources and with OpenLibrary/Internet Archive
for books or scanned monographs.

When `metadata["doi"]` is present, a follow-up `web_fetch` on the Persee article
or DOI URL can sometimes reach a full PDF or article landing page. Keep the
Persee article URL as the citation anchor because it is the connector's
canonical discovery surface.

## Output shape

Each `SearchResult` has `source_kind="persee_search"`, a Persee `/doc/...`
article URL, title, snippet, optional `published_at`, and extras:

- `doi`
- `journal`
- `volume`
- `pub_year`
- `authors`
- `lang`

`fetch(url)` accepts Persee article URLs and returns a `Source` whose
`metadata` repeats `doi`, `journal`, `volume`, `pub_year`, `authors`, and
`lang`. `cleaned_text` contains a compact bibliographic header plus visible
article text scraped from the page.

## Auth

No auth or API key is required. The connector uses the shared Playwright
session and rate-limits `www.persee.fr` / `persee.fr` to 0.5 RPS.
