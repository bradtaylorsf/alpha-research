---
name: multilingual-source-handling
description: "Use multilingual archive connectors deliberately and opt into per-finding English translations only when the goal needs non-English primary sources."
when_to_use: "goal targets multilingual archives, non-English primary sources, European newspapers/books, Wikisource language hosts, or historical topics likely to require French/Spanish/non-English evidence"
when_not_to_use: "monolingual English goals, broad contemporary web scans where non-English material is incidental, or any run whose budget is too tight for per-finding translation"
---

# Multilingual source handling

Use this strategy when the investigation needs primary sources whose strongest evidence is likely to be outside English. The strategy does not translate whole source documents. It tells the planner when to search multilingual connectors and when to opt into the per-finding English mirror that the synthesizer can consume.

## Planner directive

When this strategy is active, include it on the plan:

```yaml
active_strategies:
  - multilingual-source-handling
```

For goals that need English synthesis over non-English evidence, set the extraction opt-in on relevant task payloads:

```yaml
task_template:
  - kind: gallica_search
    payload:
      query: guerre d'Algerie 1956
      translate_non_english: true
```

The job-level CLI/config equivalent is `translate_non_english: true`. Leave it false unless the goal really needs translated findings.

## Connectors likely to surface non-English sources

- `gallica_search`: French books, newspapers, periodicals, and Bibliotheque nationale de France metadata. Expect `dc:language` values such as `fre`.
- `persee_search`: French academic journals and article pages. Expect `lang: fr`.
- `bne_search`: Spanish National Library newspaper and periodical pages. Expect `lang: es`.
- `europeana_search`: European cultural-heritage records with mixed language metadata.
- `wikisource_search`: language-specific hosts. Any `lang` other than `en` can produce non-English transcribed primary text.
- `commons_search`: media metadata can include multilingual descriptions or captions. Treat it as supporting evidence unless the description itself is the evidence.
- `openalex_search`: non-English DOI metadata and abstracts surface through `language`; use translation only when the abstract or title is material evidence.

## Cost implications

`translate_non_english: true` adds one `frontier_speed` call per non-English finding. That is cheap compared with full synthesis, but broad multilingual goals can produce dozens or hundreds of findings. On tight budgets, enable translation only on the connector tasks that are likely to produce decisive evidence. If the estimated translation would push the job past its cap, the original finding is kept and the loop emits `translation_skipped_budget`.

## Anti-patterns

- Do not enable translation on monolingual English goals. It adds pure cost.
- Do not use this as a substitute for better search scoping. Query the right language and archive first, then translate only the findings worth carrying forward.
- Do not request whole-document or cornerstone chunk-set translation. Per-finding mirrors are the v1 scope; translating an entire vector-indexed document is a separate follow-up.
- Do not translate metadata-only leads just to make them look stronger. Fetch and extract the underlying source first.
