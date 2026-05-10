---
name: openalex
description: "OpenAlex Works scholarly articles: abstracts, DOIs, citation counts, authors, venues, and open-access URLs."
when_to_use: "academic articles, scholarly literature review, DOI lookup, citation counts, article metadata, open-access paper discovery"
when_not_to_use: "books and scan identifiers -> openlibrary/iarchive; legal cases -> scholar/courtlistener; full text extraction -> web_fetch after resolving the OA URL"
---

# OpenAlex connector

OpenAlex Works covers 250M+ scholarly works across 100K+ publishers and 90+
languages. It replaces the academic-articles slot previously assigned to
JSTOR Constellate, which sunset on 2025-07-01 and no longer has a public API.

Endpoint: `api.openalex.org/works?search=<query>&per_page=<max_results>`.

## Time-period / era filtering

OpenAlex supports strong API-side filters. For recent open-access papers, use:

`filter=publication_year:>2020,is_oa:true`

This is especially effective with the `modern-policy-era-filtering` strategy:
put the modern policy term in `search`, then use `filter` for the era and
open-access constraint instead of burying every condition in the query text.

## Query construction

- Prefer concept phrases plus a policy/event term: `Project 2025 unitary executive theory`.
- Add key author, institution, or journal terms when the topic is broad.
- Use DOI URLs with `fetch()` when you already know the article identifier.
- Use `filter` for structured constraints such as publication year and OA
  status; use `sort` only when you need a non-default ordering.

## Knobs available

- `max_results` - default 20, passed as `per_page` and capped at 200.
- `filter` - OpenAlex Works filter string, for example
  `publication_year:>2020,is_oa:true`.
- `sort` - OpenAlex sort string, for example `relevance_score:desc` or
  `cited_by_count:desc`.
- `timeout` - request timeout in seconds; default 15.

## Anti-patterns

- Do not pass OpenAlex `abstract_inverted_index` directly to synthesis.
  OpenAlex stores abstracts as inverted indexes; the connector un-inverts
  them before putting readable text in `Source.cleaned_text`.
- Do not cite an OpenAlex metadata hit as if it were the article's full text.
  Treat it as metadata unless `web_fetch` has pulled the OA PDF or landing page.
- Do not use broad `web_search site:openalex.org` queries when
  `openalex_search` can query the Works endpoint directly.
- Do not assume every hit has a DOI. Some records only have an OpenAlex ID.

## When to fan out

Fan out from high-relevance hits with `metadata["open_access_url"]`. Feed that
URL to `web_fetch` to pull the actual PDF or article landing page before making
claims about the paper's argument or evidence.

Fan out by DOI when a source cites an article by DOI but not title; `fetch()`
accepts DOI resolver URLs such as `https://doi.org/10.xxxx/example`.

## Output shape

Each `SearchResult` has an OpenAlex work URL, title, snippet, `published_at`,
and `extras` with `doi`, `openalex_id`, `pub_year`, `authors`, `host_venue`,
`abstract`, `citation_count`, and `open_access_url`.

`fetch()` returns a `Source` whose `metadata` repeats those fields and whose
`Source.cleaned_text` contains a markdown metadata summary plus the
reconstructed abstract.

## Auth

No key required today for low-volume smoke and demo requests, but set
`OPENALEX_API_KEY` for regular use under OpenAlex's February 2026 free-key
policy. The connector sends it as `api_key=<key>`.

Set `RESEARCH_USER_AGENT` to include a contact email. The connector extracts
that email and appends `mailto=<email>` so the request enters OpenAlex's polite
pool. The local rate gate uses 5 RPS when identified by email or key and 1 RPS
without identification.
