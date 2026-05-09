# Open Archives + MCP Server — Implementation Handoff

> **Lifecycle note.** This file is a *transient* hand-off artifact, not a long-lived
> planning doc. Per `CLAUDE.md` and `AGENTS.md`, planning lives in GitHub issues —
> not in the repo root. Once Claude Code has used this doc to open the milestoned
> issues described below, **delete this file** (or move it to
> `.alpha-loop/handoffs/` if you want a record). Issues become the source of
> truth from that point forward.

## What this is

Two related milestones for `alpha-research`, both targeted at making the agent a
better research tool for *anyone* doing primary-source / archival work — not just
investigative-journalism / due-diligence patterns the existing connector roster
favors:

1. **Milestone A — `open-archives connector tier`.** Twenty-one new connectors
   covering historical newspapers, digitized books, declassified government
   records, Wikimedia structured data, multilingual European archives, US
   broadcast video, and academic article corpora. Free or free-with-registration
   sources only; no paid commercial APIs.
2. **Milestone B — `composable research service`.** Make alpha-research callable
   from external agents via MCP, with a stabilized programmatic Python entry
   point and a documented per-job-folder contract. The MCP server exposes both a
   *high-level lifecycle* surface (start a job, get the report) and a
   *tool-level* surface (call a single connector for one-shot lookups), so
   downstream agents can choose the granularity they need.

Both milestones are independent. Either can ship first. Both share the same
end-state: alpha-research stays a generic research engine, and other tools
(including a downstream narrative-history podcast pipeline) consume it without
forking.

---

## Milestone A — `open-archives connector tier`

### Why this exists

The existing connector roster is dense for **contemporary US public-record
investigation** — Congress, FEC, EDGAR, CourtListener, Federal Register, Senate
LDA, USAspending, GDELT, LittleSis, ProPublica Nonprofits, OpenCorporates, OFAC
sanctions, BBB, state SOS / licensing / CalAccess. Excellent for "profile this
contractor" or "track Project 2025."

It is **sparse for archival research** — pre-1990 newspapers, digitized books,
declassified war records, colonial-era state papers, structured biographical
data, multilingual European archives, US broadcast-political video, academic
article corpora. A goal like *"the Algerian war of independence, 1954–1962"* or
*"the Pullman Strike of 1894"* or *"Maya land claims in Guatemala under the
Arbenz government"* finds the existing planner reaching for `web_search` and
hoping Brave's index is up to it.

The connectors below close that gap. Each one is a free or free-with-registration
source with a real API or a stable scrape surface; none require commercial
contracts.

### Per-connector pattern

Every connector below follows the existing in-tree convention. Use
`tools/nonprofits.py` and `tools/fedregister.py` as reference implementations.
The contract:

- **Module:** `src/research_agent/tools/<name>.py`
- **Public surface:**
  - `async def search(query, *, max_results=20, **knobs) -> list[SearchResult]`
  - `async def fetch(url) -> Source | None`
- **HTTP:** `httpx.AsyncClient` for JSON APIs; `tools/browser.py` shared
  Playwright session for scrape connectors. `httpx` first; only fall back to
  Playwright when there is no JSON endpoint or the site requires JS rendering.
- **Polite rate:** default 1 RPS per host via `_rate_limit_gate()` helper.
- **Cache:** JSON responses under `corpus/.cache/<connector>/`.
- **Models:** return `SearchResult` / `Source` from `tools/models.py`. Stash any
  source-specific fields under `Source.metadata`.
- **Auth:** API key reads via `research_agent.config.get(...)`. Add the key to
  `config.py:EXPECTED_ENV_KEYS` and document in `.env.example` and `docs/API_KEYS.md`.
- **Doctor:** add a presence check in `doctor.py` only when the connector is
  unusable without the key (most are not — anonymous tier is fine).
- **Planner:** register the new `<name>_search` kind in
  `prompts/planner.md` under both the **Direct connector kinds** table and the
  **Hard rules** allowlist. Add a one-line example query.
- **Dispatch:** wire the kind in `orchestrator/loop.py` (or wherever search-task
  dispatch lives) so the loop can route to the new module.
- **Smoke:** add a fixture under `tests/fixtures/` with at least one recorded
  response and a `tests/research_agent/tools/test_<name>.py` covering happy
  path, empty result, and rate-limit retry. Wire `_smoke-tool <name>_search` in
  `cli.py`.
- **Docs:** add a row to the README's *Direct connector kinds* table (the same
  one the planner reads) and the env-key tables.

Per-issue acceptance criteria:

- [ ] Module file with `search()` + `fetch()` exists and matches the pattern.
- [ ] `SearchResult.url`, `.title`, `.snippet` populated; `Source.metadata`
      carries the connector-specific structured fields named in the spec below.
- [ ] Polite rate limit enforced; no concurrent requests against the same host.
- [ ] Wayback save attempt on first `fetch` (for HTML/PDF surfaces) — reuse the
      existing `archive.py` helper.
- [ ] Planner prompt updated; allowlist now permits `<name>_search`.
- [ ] Loop dispatch handles the new kind.
- [ ] `_smoke-tool <name>_search "<query>"` returns ≥1 result against a known
      query (recorded fixture or live, depending on auth).
- [ ] Unit tests cover happy / empty / rate-limit / 4xx-fallback / 5xx-retry.
- [ ] README + `.env.example` + `docs/API_KEYS.md` updated.
- [ ] `research doctor` check added if API key is required.

Implementation note for Claude Code: the simplest robust pattern for a new
connector is to *fork `tools/nonprofits.py` first, then rename and modify*.
That file already has the rate limiter, cache, headers, and `Source.metadata`
shape correct. Modifying is faster than building from scratch.

### Connector roster (21 issues)

Group naming follows the README's table conventions. Each row below is one
issue. The `Kind` column is the planner-registered name; the `Module` column is
the file to create.

#### US government & cultural archives (free, no key required)

| # | Kind | Module | Source | Endpoint | Notes |
|---|---|---|---|---|---|
| A1 | `chronam_search` | `tools/chronam.py` | Chronicling America (LOC) | `https://chroniclingamerica.loc.gov/search/pages/results/?format=json` | Historical US newspapers 1690–1963. JSON. Pagination via `&page=`. Returns OCR text in `ocr_eng` field — keep it; it's the whole point. |
| A2 | `loc_search` | `tools/loc.py` | Library of Congress | `https://www.loc.gov/search/?fo=json` | Photos / manuscripts / sound recordings / maps / prints. Add `&fa=` filters via a `format` knob (`format: prints\|maps\|manuscripts\|recordings`). IIIF image URLs in `image_url` field. |
| A3 | `iarchive_search` | `tools/iarchive.py` | Internet Archive (separate from Wayback) | `https://archive.org/advancedsearch.php?output=json` | Books / audio / video / texts via Metadata + Advanced Search APIs. Add `mediatype` knob (`texts\|audio\|movies\|web`). **Audio mediatype covers the "historical-primary-source audio" use case** (period radio, oral histories) — register it as part of this connector, not a separate one. |
| A4 | `nara_search` | `tools/nara.py` | US National Archives Catalog (OPA) | `https://catalog.archives.gov/api/v2/records/search` | Declassified federal records, military records, photos. JSON. Note: API moved off the older `/v1/` shape — use v2. |
| A5 | `si_search` | `tools/smithsonian.py` | Smithsonian Open Access | `https://api.si.edu/openaccess/api/v1.0/search` | 4M+ digitized objects across the Smithsonian collections. Free key via `https://api.data.gov/`. Re-uses `DATA_GOV_API_KEY` already in env (FEC connector also uses it). |

#### Aggregators & multilateral archives (free, key required)

| # | Kind | Module | Source | Endpoint | Notes |
|---|---|---|---|---|---|
| A6 | `dpla_search` | `tools/dpla.py` | Digital Public Library of America | `https://api.dp.la/v2/items` | Aggregator over 100s of US cultural institutions. Free key; register at `https://pro.dp.la/developers/api-key`. Env: `DPLA_API_KEY`. |
| A7 | `europeana_search` | `tools/europeana.py` | Europeana | `https://api.europeana.eu/record/v2/search.json` | 50M+ items European archives, multilingual. Free key at `https://pro.europeana.eu/page/get-api`. Env: `EUROPEANA_API_KEY`. |
| A8 | `trove_search` | `tools/trove.py` | Trove (National Library of Australia) | `https://api.trove.nla.gov.au/v3/result` | Australian newspapers / photos / oral histories. Free key at `https://trove.nla.gov.au/about/create-something/using-api`. Env: `TROVE_API_KEY`. |

#### UK & Commonwealth archives (free, no key)

| # | Kind | Module | Source | Endpoint | Notes |
|---|---|---|---|---|---|
| A9 | `ukna_search` | `tools/ukna.py` | UK National Archives Discovery | `https://discovery.nationalarchives.gov.uk/API/search/v1/records` | Britain's national archive — essential for any colonial-era story. JSON. No auth. Foreign Office records, War Office records, Colonial Office records all surface here. |

#### Wikimedia & structured data (free, no key)

| # | Kind | Module | Source | Endpoint | Notes |
|---|---|---|---|---|---|
| A10 | `wikidata_search` | `tools/wikidata.py` | Wikidata SPARQL | `https://query.wikidata.org/sparql` | Structured biographical / relational data — birth/death dates, family ties, occupations, places, entity IDs. Power-mapping for historical figures. SPARQL queries; the `query` field accepts either a raw SPARQL block or a natural-language query that the connector translates via a prompt template (start simple — raw SPARQL only — natural-language can be a follow-on). |
| A11 | `commons_search` | `tools/commons.py` | Wikimedia Commons | `https://commons.wikimedia.org/w/api.php?action=query&list=search` | Public-domain / CC-licensed images, audio, video. The MediaWiki API; share the helpers with `wikisource`. License field MUST land in `Source.metadata["license"]` so downstream consumers can filter. |
| A12 | `wikisource_search` | `tools/wikisource.py` | Wikisource | `https://en.wikisource.org/w/api.php` (and per-language hosts) | Transcribed primary documents (treaties, speeches, manifestos, court opinions, founding-era pamphlets). Add `lang` knob (`en\|fr\|es\|de\|...`). |

#### Books & catalogs (free, mostly no key)

| # | Kind | Module | Source | Endpoint | Notes |
|---|---|---|---|---|---|
| A13 | `hathi_search` | `tools/hathitrust.py` | HathiTrust Digital Library | `https://catalog.hathitrust.org/api/volumes/brief/...` (Bib API) + `https://babel.hathitrust.org/cgi/htd/...` (Data API) | Millions of digitized books with full-text search where in-copyright is search-only. No auth for Bib API; Data API requires registered access. Start with Bib API only; mark Data API as a follow-on. |
| A14 | `openlibrary_search` | `tools/openlibrary.py` | OpenLibrary | `https://openlibrary.org/search.json` | Book metadata + IA scan links. JSON, no auth. Cheap and reliable; routinely backfills HathiTrust gaps. |
| A15 | `worldcat_search` | `tools/worldcat.py` | WorldCat (OCLC) | `https://www.worldcat.org/api/search` (or scrape `https://search.worldcat.org/search`) | OCLC catalog — useful when the goal is "find which library has this manuscript." OCLC's search-discovery API requires a key with institutional credentials; **default to scrape** of the public search page via `tools/browser.py` and skip the API path. |

#### Imperial War Museum (no public API → scrape)

| # | Kind | Module | Source | Endpoint | Notes |
|---|---|---|---|---|---|
| A16 | `iwm_search` | `tools/iwm.py` | Imperial War Museum | `https://www.iwm.org.uk/collections/search` | 20th-century war/conflict archives — photos, oral histories, ephemera. **No public API** — Playwright scrape via `tools/browser.py`. Use the same recipe shape as `tools/calaccess.py` and `tools/bbb.py`. |

#### Multilingual European archives

| # | Kind | Module | Source | Endpoint | Notes |
|---|---|---|---|---|---|
| A17 | `gallica_search` | `tools/gallica.py` | Gallica (Bibliothèque nationale de France) | `https://gallica.bnf.fr/SRU?operation=searchRetrieve` (SRU/CQL) | Major French national-library digital archive — newspapers, books, manuscripts, maps. SRU is verbose but stable; alternative `https://gallica.bnf.fr/services/Search` is JSON-ish but undocumented. Start with SRU. No auth. |
| A18 | `persee_search` | `tools/persee.py` | Persée | `https://www.persee.fr/api/...` (limited) or scrape | French academic journals, especially humanities and social sciences. Public API is partial; scrape via `tools/browser.py` is the safer default. |
| A19 | `bne_search` | `tools/bne.py` | Hemeroteca Digital, Biblioteca Nacional de España | `https://hemerotecadigital.bne.es` | Spanish historical press — Latin-American movements, Spanish Civil War, colonial-era press. **No public API** — Playwright scrape. |

#### Academic article corpora (registered free)

| # | Kind | Module | Source | Endpoint | Notes |
|---|---|---|---|---|---|
| A20 | `jstor_search` | `tools/jstor.py` | JSTOR Constellate (Data for Research) | `https://constellate.org/api/...` | Academic articles — humanities, social sciences. Free for non-commercial; requires registration. Env: `JSTOR_CONSTELLATE_TOKEN`. May overlap with `scholar_search` (SerpAPI Google Scholar) — JSTOR is canonical for the article itself, Scholar is canonical for citation graphs. |

#### US broadcast video (no public API → scrape)

| # | Kind | Module | Source | Endpoint | Notes |
|---|---|---|---|---|---|
| A21 | `cspan_search` | `tools/cspan.py` | C-SPAN Video Library | `https://www.c-span.org/search/?searchtype=Videos&query=...` | US political broadcast video with **transcripts** — congressional floor speeches, hearings, presidential events, post-1979. **No public API** — Playwright scrape via `tools/browser.py`. Per-clip transcript text MUST be returned in `Source.metadata["transcript"]` so downstream extract sees it. |

### Issue sequencing

Open as a single milestone (`open-archives connector tier`). Implementation
order, fastest payback first:

1. **A1 Chronicling America** — single richest unlock for any pre-1963 US story; clean JSON.
2. **A2 LOC** — broad surface, clean JSON; reuses headers from A1.
3. **A3 Internet Archive** — covers IA-audio path too; clean JSON.
4. **A14 OpenLibrary + A15 WorldCat (scrape variant)** — bibliographic backbone, both small.
5. **A11 Commons + A12 Wikisource + A10 Wikidata** — Wikimedia trio, share MediaWiki helpers.
6. **A6 DPLA + A7 Europeana + A8 Trove** — aggregator trio; same key-handling pattern.
7. **A4 NARA + A9 UKNA** — gov archive pair.
8. **A13 HathiTrust + A20 JSTOR** — bibliographic + academic deepening.
9. **A17 Gallica + A18 Persée + A19 BNE** — multilingual tier; touches `tools/browser.py` recipes.
10. **A5 Smithsonian + A16 IWM + A21 C-SPAN** — final tail; A16/A21 are the two scrape-heavy ones.

### Cross-cutting changes

These don't deserve their own connector issue, but do deserve issues of their own:

- **AC-X1: Planner allowlist refactor.** Once the connector kinds list grows by 21,
  the hard-rules allowlist in `prompts/planner.md` should move to a generated
  list keyed off a single source-of-truth registry (e.g., decorate each tool
  module with a `KIND = "<name>_search"` and a build-time generator stamps the
  allowlist into the prompt). Today the allowlist is hand-maintained in two
  places (Direct-connector-kinds table + Hard-rules sentence) and will rot.
- **AC-X2: Language-aware `web_search`.** Add a `lang` knob to `tools/web_search.py`
  (Brave supports it natively). Plumb through the planner. Documented as
  optional knob — default unchanged.
- **AC-X3: Translation pass on extract.** Optional pipeline step in
  `orchestrator/loop.py` — when an extracted finding's source is non-English,
  route the body through `frontier_speed` for a translated mirror, store as
  `<finding>.translation.md` alongside the original. Off by default; opt-in via
  a new YAML field on `task_template.payload` or a per-job config knob.

### Out of scope (named explicitly so they don't get added by accident)

- **No commercial newspaper APIs.** ProQuest, Newspapers.com, NewspaperArchive,
  Genealogy Bank — all paywalled, all out. The 21 connectors above plus the
  existing `news_search` cover the realistic free corpus.
- **No Google Books.** Books API is deprecated for search; OpenLibrary +
  HathiTrust + IA cover the same ground.
- **No production-asset connectors.** SFX libraries (BBC Sound Effects,
  Freesound), royalty-free music corpora (Free Music Archive), and stock-image
  APIs are *production* tooling, not research tooling. Out of scope for
  alpha-research; consumers handle their own asset pipelines.
- **No paid data brokers beyond what's already wired.** SerpAPI / Brave /
  Proxycurl / Lix exist in v1; do not add Tavily, NewsCatcher, RapidAPI,
  ScrapingBee, etc.

---

## Milestone B — `composable research service`

### Why this exists

alpha-research today is a CLI-shaped tool. To call it from external agents,
consumers must shell out via subprocess, parse `events.jsonl`, and read
`jobs/<id>/report.md` after the daemon finishes. That works, but it makes the
agent feel like a black box.

Three layered changes turn alpha-research into a first-class composable service
without changing the daemon's lifecycle or the per-job folder format:

1. **Per-job folder contract** — formalize the existing layout as a documented
   exchange format. (Mostly already there; this is mostly docs + a tiny stable
   API around `job.json` parsing.)
2. **Stable Python entry points** — `from research_agent import …` works for
   in-process embedding by other Python agents.
3. **MCP server** — exposes both *lifecycle* and *tool-level* surfaces to
   any MCP-aware consumer (Claude Code, Claude Agent SDK, Cowork, custom
   agents).

A follow-on (B4) ships a thin FastAPI wrapper for non-Python / non-MCP
consumers; not required for v1 of this milestone.

### B1 — Per-job folder contract

**Issue:** "Document and stabilize per-job folder as exchange contract"

Add `docs/JOB_FOLDER_CONTRACT.md` capturing:

- Every file the daemon writes under `jobs/<id>/` with field-level shape.
- Stability guarantees: filename / schema versions; what's safe to rename vs.
  what external consumers depend on.
- A pure-Python `research_agent.contract` module exposing typed readers:
  `read_job(path) -> JobMetadata`, `iter_findings(path) -> Iterable[Finding]`,
  `read_report(path) -> Report`, `tail_events(path) -> Iterable[Event]`.
  Read-only — never mutates the folder.
- A test that reads a recorded fixture job folder and round-trips through
  the contract module without errors.

**AC:**
- [ ] `docs/JOB_FOLDER_CONTRACT.md` exists and is referenced from README.
- [ ] `research_agent.contract` is importable from outside the package and has
      unit tests against a fixture job.
- [ ] AGENTS.md gains a "When this contract changes, bump the schema version
      in `job.json`" rule.

### B2 — Stable Python entry points

**Issue:** "Stabilize programmatic Python API"

The CLI commands in `cli.py` already wrap underlying functions; this issue
ensures those underlying functions are documented as the public API and
don't assume a Typer context.

Public surface (target):

```python
from research_agent import (
    start_job,         # equivalent to `research start --skip-intake --goal …`
    get_job_status,    # `research status`
    list_jobs,         # `research list`
    stop_job,          # `research stop`
    resume_job,        # `research resume`
    get_report,        # `research view --report`
    get_findings,      # finding rows from a job
    search_findings,   # cross-job hybrid search (`research search`)
    export_job,        # `research export --md-bundle` / `--zip`
)
```

Each function takes plain Python args, returns plain Pydantic models from
`tools/models.py`, raises typed exceptions, and never touches stdin/stdout.

**AC:**
- [ ] Each function above is importable from `research_agent` (top-level).
- [ ] Each is type-annotated and has a docstring with one usage example.
- [ ] CLI commands now wrap these functions instead of reimplementing.
- [ ] Existing tests for the CLI still pass (no behavior change).
- [ ] `tests/test_programmatic_api.py` covers each entry point with a fixture
      job folder.

### B3 — MCP server (`research-mcp`)

**Issue (epic):** "Ship `research-mcp` MCP server with lifecycle + tool-level surfaces"

Implementation lives in a new package: `src/research_agent/mcp/server.py` plus
a console-script entry point `research-mcp` in `pyproject.toml`. Use the
official Python MCP SDK (`mcp` on PyPI). Stdio transport for v1; HTTP transport
can come later.

Two MCP tool surfaces, both exposed by the same server. Consumers pick what
they need by tool name.

**B3a — Lifecycle surface (high-level).** Wraps the Python entry points from
B2 1:1.

| MCP tool | Wraps | Returns |
|---|---|---|
| `start_research_job` | `start_job` | `{job_id, daemon_pid}` |
| `get_job_status` | `get_job_status` | `{status, spent_usd, time_elapsed, current_iteration, last_event_summary}` |
| `list_jobs` | `list_jobs` | `[{job_id, goal, status, created_at, updated_at}]` |
| `stop_job` | `stop_job` | `{stopped: bool}` |
| `resume_job` | `resume_job` | `{resumed: bool, daemon_pid}` |
| `get_report` | `get_report` | `{report_md, sources}` |
| `get_findings` | `get_findings` | `[{kind, title, body, citations, source_url, created_at}]` |
| `search_findings` | `search_findings` | `[{score, kind, snippet, source_url, job_id}]` |
| `export_job` | `export_job` | `{path, bytes}` |

This is the "delegate the whole investigation" surface. A consumer says
*"do an investigation on X"* and gets back a folder path plus a report.

**B3b — Tool-level surface (low-level).** One MCP tool per connector kind,
mirroring the existing `_smoke-tool` plumbing in `cli.py`. A consumer reaches
into the tool drawer for one specific lookup without spinning up a job.

Examples:

| MCP tool | Wraps | Use case |
|---|---|---|
| `web_search` | `tools/web_search.py:search` | "Search the web for X" |
| `web_fetch` | `tools/web_fetch.py:fetch` | "Pull this URL as cleaned markdown + Wayback copy" |
| `congress_search` | `tools/congress.py:search` | "Look up bills / members / committees" |
| `chronam_search` | `tools/chronam.py:search` | (after A1 lands) "Search historical newspapers" |
| `loc_search` | `tools/loc.py:search` | (after A2 lands) "Search Library of Congress" |
| … | one tool per connector | … |

Implementation:
- A registry decorator in `tools/__init__.py` (e.g., `@register_kind("chronam_search")`)
  marks each connector module with its kind name + payload schema.
- The MCP server iterates the registry on startup and dynamically registers
  one MCP tool per kind. New connectors added under Milestone A get exposed
  automatically once they're registered — no MCP changes needed.

**AC for B3:**
- [ ] `pip install -e .` provides a `research-mcp` console script.
- [ ] Stdio transport works with the official MCP SDK; tested by a smoke test
      that spawns the server, calls `list_jobs`, and asserts the response shape.
- [ ] Lifecycle surface (B3a) covers all nine tools above with structured input
      / output schemas.
- [ ] Tool-level surface (B3b) auto-registers one MCP tool per connector kind
      from the registry decorator.
- [ ] `docs/MCP.md` describes how to wire the server into Claude Code, Claude
      Agent SDK, and Cowork (with sample config snippets for each).
- [ ] Telemetry: every MCP tool call emits a structured event to a
      `mcp_events.jsonl` log under `data/` so consumers can be audited.
- [ ] Server respects the same budget cap as the CLI — calls that would tip a
      job past its budget return a typed MCP error, not a partial result.

### B4 — Optional: HTTP daemon mode

Out of scope for the initial composability epic, but worth opening as a
follow-on issue: a thin FastAPI wrapper around the same Python entry points,
for non-Python / non-MCP consumers. Defer until a real consumer asks for it.

### Composability sequencing

1. **B1 — Job folder contract** — small, mostly docs + thin reader module.
2. **B2 — Programmatic Python API** — refactor with no behavior change.
3. **B3 — MCP server** — both surfaces in one ship; depends on B2.
4. **B4 — HTTP daemon (optional)** — defer.

---

## Cross-milestone notes

### Naming hygiene

The two milestones do not share a name with any consumer project. Issue
labels: `area:open-archives` and `area:composability`. Milestones: literally
`open-archives connector tier` and `composable research service`. Don't
co-name with downstream projects (e.g., narrative-history podcast pipelines)
even if a specific consumer is the catalyst — alpha-research is generic
infrastructure.

### Testing posture

- **Connector unit tests** — fixture-driven; record one good response per
  connector under `tests/fixtures/<connector>/` with a real query, then mock
  `httpx` against the recording. Live-against-the-network tests live under
  `tests/integration/` and run on demand, not in CI.
- **MCP tests** — spawn the stdio server in a subprocess, drive it with the
  Python MCP SDK's client harness. Cover startup, tool listing, one lifecycle
  call, one tool-level call, error-on-missing-job.

### Doctor changes

`research doctor` should grow:
- One row per new env-var-required connector (DPLA, Europeana, Trove, JSTOR).
- A presence-only check (does the key look populated) — not a network call,
  same shape as the existing OPENROUTER key check.
- A "MCP server importable" check (`research_agent.mcp.server` imports without
  exception) for B3.

### Documentation order

When opening issues, order README updates to land *with* their connectors —
don't ship code without docs. The README's *Direct connector kinds* table is
the single most-read piece of operator docs in this repo.

### What this work doesn't change

- The daemon lifecycle (`start/status/stop/resume`) stays exactly as is.
- The model-tier routing (`fast/general/reasoner/vision/embeddings/frontier/
  frontier_alt/frontier_speed`) stays exactly as is.
- The cost cap, disk cap, and time cap behaviors stay exactly as is.
- The synthesis / critique / report.md surface stays exactly as is.
- Existing connectors — including `web_search`, `news_search`, `reddit_search`,
  `arxiv_search`, and the 18 existing direct-connector kinds — are not
  modified except for the planner allowlist refactor in AC-X1.

---

## Issue-creation summary (for `alpha-loop add`)

Two milestones. Twenty-five issues. Suggested ordering:

**Milestone `open-archives connector tier`:**
- A1 chronam_search
- A2 loc_search
- A3 iarchive_search
- A4 nara_search
- A5 si_search
- A6 dpla_search
- A7 europeana_search
- A8 trove_search
- A9 ukna_search
- A10 wikidata_search
- A11 commons_search
- A12 wikisource_search
- A13 hathi_search
- A14 openlibrary_search
- A15 worldcat_search (scrape default)
- A16 iwm_search (scrape)
- A17 gallica_search
- A18 persee_search (scrape default)
- A19 bne_search (scrape)
- A20 jstor_search
- A21 cspan_search (scrape)
- AC-X1 planner allowlist refactor
- AC-X2 language-aware web_search
- AC-X3 translation pass on extract (optional, mark `enhancement`)

**Milestone `composable research service`:**
- B1 per-job folder contract + readers
- B2 stabilize programmatic Python API
- B3 ship `research-mcp` server (epic; sub-issues B3a lifecycle, B3b tool-level)
- B4 HTTP daemon wrapper (optional, mark `later`)

When `alpha-loop add` opens these, each issue body should include:
- The relevant section from this doc, transcribed verbatim.
- The acceptance-criteria checklist for that connector / component.
- A "References" footer pointing at the existing reference module
  (`tools/nonprofits.py`, `tools/fedregister.py`, `tools/calaccess.py` for
  scrape) and the relevant section of the README + AGENTS.md.

Once issues exist, this handoff doc has done its job. Delete it (or move to
`.alpha-loop/handoffs/`) and let the issue tracker drive from there.
