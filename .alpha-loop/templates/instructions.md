<!-- managed by alpha-loop -->
# Alpha Research

## Overview
Repo for an **autonomous overnight investigative research agent** that runs on a Mac, uses LM Studio for local model work and OpenRouter for cloud synthesis, and persists everything as markdown + SQLite. The Python package (`research_agent`, CLI `research`) is scaffolded under `src/` and is being filled in issue-by-issue via alpha-loop. The top-level `*.md` playbooks remain the strategic source of truth and are themselves project deliverables. Output of this work feeds the broader `Alpha*` agent ecosystem.

## Tech Stack
- Language: **Python 3.12+** (typed, async-first; no TypeScript at the core)
- Agent framework: **Pydantic AI** + a thin custom orchestrator (no LangGraph in v1)
- CLI: **Typer** (commands) + **Rich** (live progress) + **Questionary** (interactive intake); entry point `research = "research_agent.cli:app"`
- Storage: **SQLite** (WAL mode) at `data/index.sqlite` for the cross-job index/queue/checkpoints/events; **markdown + JSON sidecars** for per-job content
- Model providers: **LM Studio** at `http://localhost:1234/v1` (local, MLX) and **OpenRouter** at `https://openrouter.ai/api/v1` (cloud synthesis)
- Sources (in-tree connectors, all free / public): **Playwright**-driven web search and per-source recipes (`tools/browser.py`); `httpx` + `trafilatura` + `readability-lxml` (`web_fetch`); `arxiv` + `feedparser` (`arxiv_tool`, `news`); `waybackpy` (`archive`); local PDFs/notes via `pypdf` + `pdfplumber` + `unstructured` (`local_corpus`, `pdf`, `ocr`); audio via `pywhispercpp` / `mlx-whisper` (`audio`); GitHub via the operator's `gh` CLI; plus public-records and disclosure connectors (EDGAR, FEC, USASpending, FedRegister, Congress, CourtListener, GDELT, Scholar, OpenCorporates, Sanctions, LDA, SoS, BBB, Nonprofits, LittleSis, CalAccess, Licensing, LinkedIn-via-browser, Reddit-via-browser, YouTube)
- Package manager: **uv** (lockfile committed; `.venv/`, ruff/mypy caches gitignored). `uv sync` installs the `dev` group automatically (PEP 735); tests run via `uv run pytest` (wrapped by `scripts/test.sh` for alpha-loop preflight tolerance)

## Directory Structure
- `ai-agent-investigation-playbook.md` — investigative patterns (the "what to do" library)
- `ai-agent-research-setup.md` — strategic architecture and agent roster
- `research-agent-implementation-guide.md` — **the v1 build spec; treat as source of truth for code structure and decisions**
- `CLAUDE.md` — how alpha-loop drives planning/build/PR flow in this repo
- `AGENTS.md`, `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `LICENSE` — public-facing docs (the repo is open-source)
- `.alpha-loop.yaml` — loop config (repo, label, base branch, test/dev/setup commands)
- `pyproject.toml`, `uv.lock` — Python project + locked deps
- `src/research_agent/`
  - `cli.py`, `daemon.py`, `intake.py`, `doctor.py`, `config.py`, `__main__.py` — top-level CLI surface and process entry points
  - `orchestrator/` — `loop.py`, `plan.py`, `synth.py`, `critique.py`, `checkpoint.py`, `errors.py`
  - `llm/` — `router.py` (tier→provider), `budgets.py` (cost cap), `cache.py`, `smoke.py`
  - `tools/` — `browser.py`, `web_search.py`, `web_fetch.py`, plus per-source connectors (`news`, `reddit`, `arxiv_tool`, `archive`, `local_corpus`, `models`, `pdf`, `ocr`, `audio`, `youtube`, `linkedin`, `scholar`, `gdelt`, and the public-records/disclosure family: `edgar`, `fec`, `usaspending`, `fedregister`, `congress`, `courtlistener`, `opencorporates`, `sanctions`, `lda`, `sos`, `bbb`, `nonprofits`, `littlesis`, `calaccess`, `licensing`)
  - `storage/` — `db.py`, `jobs.py`, `markdown.py`, `sources.py`, `tasks.py`, `search.py`, `export.py`, `disk_cap.py`
  - `observability/events.py` — JSONL + SQLite event mirror
  - `ui/render.py` — Rich live-progress rendering (TUI/web UI deferred)
  - `prompts/` — agent-persona templates as markdown (`planner.md`, `researcher.md`, `researcher_cornerstone.md`, `critic.md`, `synthesizer.md`, `intake_followup.md`, `followup_recipes.md`, `paid_unblock_recipes.md`); loaded via `prompts/loader.py`, packaged via `[tool.setuptools.package-data]`
- `config/` — `default.yaml`, `models.yaml` (tier→model routing), `models.local.yaml` (local override), `sources.yaml`
- `tests/` — mirrors `src/research_agent/` layout; `tests/fixtures/`, `tests/integration/`
- `docs/API_KEYS.md` — operator setup notes for model/data provider keys
- `scripts/test.sh` — tolerant `uv run pytest` wrapper used by alpha-loop preflight
- `corpus/` — local research corpus (PDFs, notes); content lives here, gitignored if large
- `jobs/<job-id>/` — per-job folders (see Code Style); **gitignored**
- `data/index.sqlite`, `data/diagnostics/` — cross-job index + tool diagnostics dumps; **gitignored**
- `.alpha-loop/`, `.worktrees/`, `runs/`, `logs/`, `sessions/`, `.venv/` — machine-local, **gitignored**

## Code Style
- **Read the implementation guide before designing anything new.** `research-agent-implementation-guide.md` already locks the v1 calls (Pydantic AI, SQLite queue, per-job folder, Typer, model routing). Don't re-litigate those decisions in code; if you genuinely need to deviate, raise it in the issue first.
- **Per-job folder is self-contained.** A job lives entirely under `jobs/<job-id>/` (`job.json`, `intake.json`, `goal.md`, `plan/`, `findings/`, `sources/`, `synthesis/`, `critique/`, `report.md`, `report.history`, `events.jsonl`, `daemon.pid`, `daemon.{out,err}.log`). Cross-job state goes in `data/index.sqlite`. Do not scatter job state elsewhere.
- **Markdown for content, JSON sidecars for metadata.** Every finding/source/synthesis pass is one `.md` file readable by humans plus one `.json` with the structured fields a UI or indexer needs.
- **Type structured outputs with Pydantic AI** (`output_type=MySchema`); rely on its retry-on-schema-violation. Don't hand-parse model output.
- **Prompts live in `src/research_agent/prompts/*.md`**, not as inline string literals. Add new agent personas (or reusable recipe libraries like `followup_recipes.md`) as a new markdown file and load via `prompts.loader`. Editing a persona's behavior means editing the markdown, not the Python.
- **Model routing is config-driven.** Pick a *tier* (`fast_local`, `accurate_local`, `synth_cloud`, etc.) in code; `config/models.yaml` (with `models.local.yaml` overlay) decides which model serves it. Never hardcode model names in business logic.
- **Cost cap is enforced in `llm/budgets.py`** at the OpenRouter wrapper layer — every cloud call passes through it.
- **Every state transition is checkpointed** via `orchestrator/checkpoint.py`. A daemon killed mid-run must be resumable from the last checkpoint with `research resume <job-id>`.
- **Observability:** every tool call, model call, and decision emits a structured event to `jobs/<job-id>/events.jsonl` *and* mirrors into the SQLite `events` table via `observability/events.py`. JSONL is the surface a future TUI/web UI tails — don't print-and-forget.
- **Sources stay deduped and archived.** A fetched URL is hashed (sha256), stored once under `sources/<sha256>.{md,json}`, and a Wayback save is attempted on first fetch.
- **Disk usage is capped.** New writers under `jobs/`/`data/` go through `storage/disk_cap.py` so a runaway crawl can't fill the disk; respect the cap rather than bypassing it.
- **Naming:** snake_case modules, PascalCase Pydantic models, verb-on-noun for CLI subcommands (`research start`, `research view`, `research doctor`, `research resume`), mirroring `git`'s shape. CLI subcommand groups use a Typer sub-app (e.g. `research config cache-clear`).
- **No planning docs as ad-hoc files in the repo root.** Plans go in the GitHub issue body or comments; long-form research goes in the existing top-level `*.md` playbooks (and links from issues). Don't create scratch `NOTES.md` / `PLAN.md` files.
- **Tests live in `tests/`** mirroring `src/research_agent/` layout; fixtures under `tests/fixtures/`, slow/network-touching integration tests under `tests/integration/`.

## Non-Negotiables
- **GitHub Issues are the source of truth.** No work without an issue; PRs must close their issue (`Closes #N`). See `CLAUDE.md` for the alpha-loop workflow — don't bypass it.
- **Never commit secrets or large artifacts.** `.env*` files, API keys (OpenRouter, GitHub, etc.), `*.gguf`/`*.safetensors`/`*.pt` model weights, and the contents of `data/`, `jobs/`, `runs/`, `logs/`, `sessions/`, `models/`, `.worktrees/` are gitignored — keep it that way. If a publishable artifact must be tracked, promote it to a curated dir and `git add -f` deliberately. The repo is public, so treat every diff as if it will be read by strangers.
- **Outbound actions go through the judge gate.** Any task with `outbound: true` (email, FOIA, web form, voice) is held for the judge agent / human review queue — never let the planner or a worker dispatch outbound side-effects directly. v1 keeps outbound out of scope; if you're adding it, you're adding the gate at the same time.
- **SQLite uses WAL mode.** Don't disable it; concurrent reader (UI/CLI) + long-running writer (daemon) depends on it.
- **Local vs. cloud routing is not optional.** Cheap/iterative work (query rewriting, source filtering, per-source extraction) goes to LM Studio. Synthesis, planning rewrites, and gap analysis go to OpenRouter. Don't run cheap loops against the cloud — the cost cap will trip and the run will halt.
- **Don't break the per-job folder contract.** External tools (`research view`, `research export`, future UIs) read `jobs/<job-id>/` by convention. Renaming files or skipping sidecars silently breaks them.
- **Research playbooks are content, not scratch.** `ai-agent-investigation-playbook.md`, `ai-agent-research-setup.md`, and `research-agent-implementation-guide.md` are the project's deliverables-in-progress. Edit them deliberately (via an issue), not as a side effect of unrelated work.
- **No paid third-party data APIs.** All web search, news, and social scraping go through Playwright against public sites via the shared session manager in `tools/browser.py`. Free public APIs that don't require app registration (or only require a free, no-cost key) are OK — arXiv, Wayback, GitHub via the operator's already-authenticated `gh` CLI, and the public-records family already wired up (EDGAR, FEC, USASpending, FedRegister, Congress, CourtListener, GDELT, etc.). The only paid third party in v1 is **OpenRouter** for cloud LLM synthesis. **Do not add** Tavily, Brave Search, NewsCatcher, Reddit OAuth/PRAW, SerpAPI, or similar — add a Playwright recipe instead. (Note: `reddit.py` and `linkedin.py` are Playwright/public-endpoint connectors, not OAuth SDKs — keep them that way.)
