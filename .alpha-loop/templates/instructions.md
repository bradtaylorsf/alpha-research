<!-- managed by alpha-loop -->
# Alpha Research

## Overview
Repo for an **autonomous overnight investigative research agent** that runs on a Mac workstation, uses LM Studio for local model work and OpenRouter for cloud synthesis, and persists research as markdown + SQLite. The Python package (`research_agent`, CLI `research`) has a functional v1 CLI/daemon baseline: intake, job lifecycle, planner loop, connector dispatch, synthesis/critique, search, export, compare, source dedupe, checkpoint resume, and report history are in-tree. Connector depth, open-archive coverage, cornerstone-document handling, and long-run quality are still expanded issue-by-issue via alpha-loop. The top-level `*.md` playbooks remain strategic source material and project deliverables. Output of this work feeds the broader `Alpha*` agent ecosystem.

## Tech Stack
- Language: **Python 3.12+** (typed, async-first; no TypeScript at the core)
- Agent framework: **Pydantic AI** + a thin custom orchestrator (no LangGraph in v1)
- CLI: **Typer** commands, **Rich** status/progress rendering, **Questionary** interactive intake; entry point `research = "research_agent.cli:app"` and `python -m research_agent` via `__main__.py`
- Storage: **SQLite** in WAL mode at `data/index.sqlite` for jobs, tasks, plans, checkpoints, events, sources, findings, FTS5, embeddings, and LLM call ledger; separate `data/llm_cache.sqlite` for wipeable LLM cache; markdown + JSON sidecars for per-job content
- Model providers: **LM Studio** at `http://localhost:1234/v1` for local tiers and embeddings; **OpenRouter** at `https://openrouter.ai/api/v1` for cloud tiers. Logical tiers are `fast`, `general`, `reasoner`, `vision`, `embeddings`, `frontier`, `frontier_alt`, and `frontier_speed`; `config/models.local.yaml` maps every tier to LM Studio for `--local` runs.
- Sources/connectors: public-first in-tree connectors using `tools/models.py` (`SearchResult`, `Source`) plus direct-kind registration in `tools/_registry.py`: Playwright/DDG/Google/Brave web search, `web_fetch` via `httpx` + `trafilatura` + `readability-lxml`, Wayback + archive.today archival, arXiv, RSS/news, Reddit, local corpus/cornerstone retrieval, PDF/OCR/audio/YouTube, public records/disclosures (EDGAR, FEC, Congress, FedRegister, CourtListener, LDA, USAspending, GDELT, LittleSis, Nonprofits, OpenCorporates, Sanctions, BBB, SoS, Licensing, CalAccess, Scholar, LinkedIn), and open-archive/scholarly surfaces (LoC, Commons, C-SPAN, Internet Archive, Trove, Wikidata, Wikisource, OpenAlex, OpenLibrary, Persee, BNE, HathiTrust fetch-only enrichment)
- Browser automation: shared Playwright session in `tools/browser.py`, reused by search/fetch and scrape connectors with host-level rate limits and diagnostics under `data/diagnostics/`
- Package manager: **uv** with committed `uv.lock`; `.venv/`, caches, runtime DBs, logs, jobs, and large artifacts stay gitignored

## Directory Structure
- `ai-agent-investigation-playbook.md` - investigative patterns and source taxonomy
- `ai-agent-research-setup.md` - strategic architecture, model routing, and operator setup context
- `research-agent-implementation-guide.md` - **the v1 build spec; treat as source of truth for architecture and contracts**
- `OPEN_ARCHIVES_AND_MCP_HANDOFF.md` - transient open-archives/MCP handoff; convert to issues, then delete or move under `.alpha-loop/handoffs/`
- `CLAUDE.md` - alpha-loop planning/build/PR conventions for this repo
- `AGENTS.md`, `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `LICENSE`, `.env.example` - public-facing docs and operator template files
- `.alpha-loop.yaml`, `.alpha-loop/`, `.agents/skills/` - alpha-loop config, local loop state, and repo-specific Codex skills
- `.github/ISSUE_TEMPLATE/` - issue templates for agent-ready work, bugs, and epics
- `pyproject.toml`, `uv.lock` - Python package metadata, scripts, dependencies, package data, and lockfile
- `scripts/` - repo maintenance helpers, including direct-connector README table regeneration
- `src/research_agent/`
  - `cli.py`, `daemon.py`, `intake.py`, `doctor.py`, `config.py`, `__main__.py` - CLI, lifecycle, env loading, and process entry points
  - `orchestrator/` - planner/task loop, synthesis, critique, checkpointing, retry/error boundaries, cornerstone extraction/querying, and translation pass
  - `llm/` - tier router, budget ledger, response cache, and LLM smoke helpers
  - `tools/` - connector modules plus shared browser, registry, media helpers, source models, and error helpers
  - `storage/` - SQLite schema/migrations, job folders, markdown/JSON writers, sources, tasks, hybrid search, export, disk cap
  - `observability/events.py` - append-only `events.jsonl` writer plus SQLite event mirror
  - `ui/render.py` - Rich renderers for jobs, status, logs, search results, exports, and comparisons
  - `prompts/` - packaged markdown prompts loaded via `prompts.loader`
  - `skills/` - packaged connector/strategy guidance with YAML frontmatter loaded via `skills.loader`
- `config/` - `models.yaml`, `models.local.yaml`, `sources.yaml`, `url_blocklist.yaml`, and placeholder `default.yaml`
- `docs/API_KEYS.md`, `docs/CONFIG.md` - operator notes for credentials and runtime/job knobs
- `tests/` - mirrors `src/research_agent/`, with fixtures and integration templates
- `corpus/` - local research corpus root; contents are gitignored
- `jobs/<job-id>/` - per-job folders; gitignored
- `data/index.sqlite`, `data/llm_cache.sqlite`, `data/sanctions/`, `data/diagnostics/` - runtime indexes, caches, and diagnostics; gitignored
- `.worktrees/`, `runs/`, `logs/`, `sessions/`, `.venv/`, `models/` - machine-local runtime/build artifacts; gitignored

## Code Style
- **Read the implementation guide before designing anything new.** `research-agent-implementation-guide.md` locks the v1 decisions: Pydantic AI, SQLite queue, per-job folders, Typer CLI, model tiers, and source/sidecar contracts.
- **Per-job folder is self-contained.** A job lives under `jobs/<job-id>/` with `job.json`, `intake.json`, `goal.md`, `plan/`, `findings/`, `sources/`, `synthesis/`, `critique/`, `report.md`, `report.history/`, `archive/`, `events.jsonl`, `daemon.pid`, `daemon.{out,err}.log`, and optional `STOP`.
- **Job IDs are deterministic and safe.** They use `YYYY-MM-DD-<slug>` from the goal, with slug validation in `storage/jobs.py`; do not bypass that constructor.
- **Markdown for content, JSON sidecars for metadata.** Findings, translations, plans, sources, syntheses, critiques, reports, and exports must remain human-readable on disk and machine-indexable in SQLite.
- **Use atomic file writes.** Project writers use `*.tmp` + `os.replace`; new writers under job/data paths should follow the same pattern.
- **Type structured model outputs with Pydantic AI** (`output_type=MySchema`) and rely on schema retries rather than hand-parsing model text.
- **Prompts live in `src/research_agent/prompts/*.md`.** Persona or recipe behavior belongs in markdown and is loaded through `prompts.loader`, not embedded as Python string literals.
- **Skills live in `src/research_agent/skills/{connectors,strategies}/*.md`.** Each skill needs YAML frontmatter with at least `description:`; planner sees descriptions, and the orchestrator deep-loads bodies only when relevant.
- **Direct connector kinds are registry-driven.** New planner-callable connectors register through `tools/_registry.py`; the planner prompt, orchestrator handlers, `research doctor`, and README direct-kind table all derive from that registry. Do not hand-maintain parallel connector allowlists.
- **Connector modules use `SearchResult`/`Source`.** Planner-callable connectors expose async `search()` and usually `fetch()`; fetch-only enrichers such as HathiTrust are explicit exceptions. Stash connector-specific fields in `.extras`/`.metadata`, enforce polite rate limits, update `SourceKind`, host-dispatch, skills, and smoke registry as applicable.
- **Model routing is config-driven.** Business logic selects tiers (`general`, `frontier`, etc.); `config/models.yaml` and `config/models.local.yaml` decide providers/models. Do not hardcode model names outside routing/config-specific code.
- **Cloud spend is explicit.** Normal LLM calls go through `Router` and `BudgetTracker` in `llm/budgets.py`; local-tier degradation fallbacks and PDF/OCR VLM escalations must be configured, visible in events, and budget-tracked or WARN-gated before the call.
- **Checkpoint every state transition.** The daemon must be resumable with `research resume <job-id>` from the latest checkpoint after a crash or kill.
- **Emit structured observability.** Tool calls, model calls, decisions, warnings, checkpoints, skill loads, fan-outs, and completion reasons append to `events.jsonl` and mirror into SQLite via `observability/events.py`.
- **Sources are deduped by content hash in SQLite and materialized per job.** `storage/sources.py` hashes cleaned content, links via `job_sources`, writes `sources/<sha256>.{md,json}` into the active job, and rehydrates pruned files when refetched.
- **Cornerstone documents have a separate path.** Plans can declare `cornerstone_url`; large PDFs may trigger section-walk extraction, per-job chunk indexing as `cornerstone_chunk` sources with `parent_source_id`, and `cornerstone_query` follow-ups.
- **Translation is opt-in.** `translate_non_english` belongs in job intake or task payload, writes `findings/NNNNNN.translation.md`, uses `frontier_speed`, and skips rather than exceeding budget.
- **Archive first-fetch URLs best-effort.** `web_fetch` spawns Wayback Save Page Now and falls back to archive.today; archive failures should warn, not fail the fetch.
- **Respect disk caps.** Writers under `jobs/` and `data/` must work with `storage/disk_cap.py`; pruning removes low-relevance source markdown but keeps audit rows.
- **Env vars are centrally registered.** New runtime keys belong in `config.py:EXPECTED_ENV_KEYS`, `.env.example`, README/API key docs as applicable, and should be read through `research_agent.config.get()`.
- **Naming:** snake_case modules, PascalCase Pydantic models, and verb-on-noun CLI subcommands mirroring `git` shape (`research start`, `research view`, `research config cache-clear`).
- **No root scratch docs.** Plans belong in GitHub issues/comments; long-form research belongs in the existing top-level playbooks. Do not add ad-hoc `NOTES.md`, `PLAN.md`, or similar files in the repo root.

## Non-Negotiables
- **GitHub Issues are the source of truth.** No work without an issue; PRs must close their issue (`Closes #N`). See `CLAUDE.md` for the alpha-loop workflow.
- **Never commit secrets or large artifacts.** `.env*` files except `.env.example`, API keys, model weights, `data/`, `jobs/`, `runs/`, `logs/`, `sessions/`, `models/`, `.worktrees/`, and local corpus contents stay out of git unless deliberately promoted.
- **Outbound actions go through the judge gate.** Any task with `outbound: true` (email, FOIA, web form, voice) must wait for judge/human review; planner and workers must not dispatch outbound side effects directly.
- **SQLite uses WAL mode.** Do not disable WAL or foreign-key enforcement on the main index; concurrent CLI/UI readers and daemon writers depend on it.
- **No silent paid reroutes.** Cheap iterative work should stay on local tiers by default; any cloud fallback must be configured, visible in events, and budget-tracked. `--local` means all tiers use LM Studio and cloud health checks are skipped.
- **Do not break the per-job folder contract.** CLI commands, exports, comparisons, search, and future UIs read `jobs/<job-id>/` by convention.
- **Research playbooks are deliverables, not scratch.** Edit `ai-agent-investigation-playbook.md`, `ai-agent-research-setup.md`, and `research-agent-implementation-guide.md` only when the issue is about those artifacts.
- **No unapproved paid data APIs.** Prefer public/free APIs or Playwright recipes. Existing explicit paid/gated exceptions are OpenRouter, Scholar via SerpAPI, and LinkedIn via Proxycurl/Lix; keep them documented, env-gated, and used only through explicit planner tasks.
