---
name: wikisource
description: "Wikisource public-domain and freely licensed transcribed primary documents; fetch full text into cleaned_text."
when_to_use: "named source texts such as treaties, speeches, manifestos, court opinions, founding-era pamphlets, statutes, and historical proclamations"
when_not_to_use: "living-author or copyrighted modern content; encyclopedia facts -> web_search/Wikipedia; media assets -> commons_search"
---

# Wikisource connector

Wikisource is a multilingual library of public-domain and freely licensed
transcribed source texts. Use `wikisource_search` when the research target needs
the actual words of a primary document: treaties, speeches, manifestos, court
opinions, founding-era pamphlets, historical legislation, proclamations, and
similar source texts.

The connector uses per-language MediaWiki hosts:
`<lang>.wikisource.org/w/api.php`. The default is `lang=en`; supported language
knobs include `en`, `fr`, `es`, `de`, `it`, `pt`, `nl`, `ru`, `zh`, `ja`, and
`ar`.

The important contract is that `fetch()` returns the full transcribed body in
`Source.cleaned_text`. These are searchable primary documents, so do not leave
the document body only in metadata or snippets.

## Time-period / era filtering

Wikisource search has no first-class date filter in this connector. Include the
era in the query when a title is ambiguous: `Treaty of Versailles 1919`,
`Federalist No. 10 1787`, `Declaration of Independence 1776`.

For non-English documents, pair the connector with the
`multilingual-source-handling` strategy. Query the local-language title on the
matching language host and keep the original-language text visible in evidence.

## Query construction

- Prefer specific named documents over broad keyword discovery:
  `Treaty of Versailles`, `Federalist No. 10`, `Emancipation Proclamation`,
  `Declaration of Algerian Independence`.
- Use the title as printed where possible. Add author, year, or jurisdiction
  only when the title is ambiguous.
- For translated or multilingual topics, fan out by language: English title on
  `lang=en`, French title on `lang=fr`, Spanish title on `lang=es`, and so on.
- Wikisource is not Wikipedia. Search for source texts, not encyclopedia topic
  summaries.

## Knobs available

- `lang` - Wikisource language host. Default `en`. Minimum supported set:
  `en|fr|es|de|it|pt|nl|ru|zh|ja|ar`.
- `max_results` - client-side result cap; default 20.

## Anti-patterns

- Do not search for living-author or copyrighted modern content. Wikisource only
  carries public-domain or freely licensed transcribed texts.
- Do not use Wikisource for images, scans, or reusable media assets; use
  `commons_search`.
- Do not cite a search snippet as the document body. Emit a fetch follow-up so
  the full transcription lands in `Source.cleaned_text`.
- Do not treat a translated page as equivalent to the original-language
  source without saying which language host supplied it.

## When to fan out

Fan out across language hosts when the subject is multilingual, colonial,
diplomatic, or likely to have authoritative source texts in more than one
language. Pair that with `multilingual-source-handling` so the planner preserves
language, title, and translation caveats.

Fan out from search to fetch for every candidate document you intend to cite.
The search result is only a locator; the evidentiary content is the fetched
full text in `Source.cleaned_text`.

## Output shape

Each `SearchResult` carries a Wikisource page URL, title, snippet, and `extras`
with `wikisource_lang`, `page_title`, `page_id`, `word_count`, and `size`.

`fetch()` returns a `Source` with:

- `cleaned_text` - the full transcribed document body, headed by the page title.
- `metadata["wikisource_lang"]` - language host used.
- `metadata["page_title"]` - resolved page title.
- `metadata["revision_id"]` - MediaWiki revision ID used for citation/audit.

## Auth

No auth or API key. Requests use a project-identifying User-Agent and the shared
1 RPS Wikimedia MediaWiki limiter.
