<!-- managed by alpha-loop -->
# Alpha Research

## Overview
Research workspace for designing and building an **autonomous overnight investigative research agent** that runs on a Mac, uses LM Studio for local model work and OpenRouter for cloud synthesis, and persists everything as markdown + SQLite. The repo currently holds the strategic playbooks and the v1 implementation guide; the actual `research_agent` Python package will be built here issue-by-issue via alpha-loop. Output of this work feeds the broader `Alpha*` agent ecosystem.

## Tech Stack
- Language: **Python 3.12+** (typed, async-first; no TypeScript at the core)
- Agent framework: **Pydantic AI** + a thin custom orchestrator (no LangGraph in v1)
- CLI: **Typer** (commands) + **Rich** (live progress) + **Questionary** (interactive intake)
- Storage: **SQLite** (WAL mode) for the cross-job index/queue/checkpoints; **markdown + JSON sidecars** for content
- Model providers: **LM Studio** at `http://localhost:1234/v1` (local, MLX), **OpenRouter** at `https://openrouter.ai/api/v1` (cloud synthesis)
- Sources: web search (Tavily/Brave/Exa), `httpx` + `trafilatura`, GitHub, arXiv, news, Reddit (PRAW), Wayback archive
- Package/runtime conventions: keep dependencies minimal; lockfiles are committed, caches (`.venv/`, `.ruff_cache/`, etc.) are gitignored

## Directory Structure
Today the repo is mostly research artifacts; the code layout below is the target the implementation guide locks in.

- `ai-agent-investigation-playbook.md` — investigative patterns (the "what to do" library)
- `ai-agent-research-setup.md` — strategic architecture and agent roster
- `research-agent-implementation-guide.md` — **the v1 build spec; treat as source of truth for code structure and decisions**
- `CLAUDE.md` — how alpha-loop drives planning/build/PR flow in this repo
- `.alpha-loop.yaml` — loop config (repo, label, base branch, test/dev commands)
- `src/research_agent/` *(to be created)* — `cli.py`, `daemon.py`, `orchestrator/`, `llm/` (router + lmstudio + openrouter + budgets + cache), `tools/` (one connector per source), `storage/` (jobs, db, markdown, sources, findings), `observability/` (events, progress), `ui/` (stub for later)
- `config/` *(to be created)* — `default.yaml`, `models.yaml` (tier→model routing), `sources.yaml`
- `corpus/` — local research corpus (PDFs, notes) — content lives here, gitignored if large
- `jobs/`, `runs/`, `data/`, `logs/`, `sessions/`, `.alpha-loop/` — **all gitignored**, regenerable, machine-local

## Code Style
- **Read the implementation guide before designing anything new.** `research-agent-implementation-guide.md` already locks the v1 calls (Pydantic AI, SQLite queue, per-job folder, Typer, model routing). Don't re-litigate those decisions in code; if you genuinely need to deviate, raise it in the issue first.
- **Per-job folder is self-contained.** A job lives entirely under `jobs/<job-id>/` (`job.json`, `intake.json`, `goal.md`, `plan/`, `findings/`, `sources/`, `synthesis/`, `critique/`, `report.md`, `events.jsonl`, `daemon.pid`). Cross-job state goes in `data/index.sqlite`. Do not scatter job state elsewhere.
- **Markdown for content, JSON sidecars for metadata.** Every finding/source/synthesis pass is one `.md` file readable by humans plus one `.json` with the structured fields a UI or indexer needs.
- **Type structured outputs with Pydantic AI** (`output_type=MySchema`); rely on its retry-on-schema-violation. Don't hand-parse model output.
- **Model routing is config-driven.** Pick a *tier* (`fast_local`, `accurate_local`, `synth_cloud`, etc.) in code; `config/models.yaml` decides which model serves it. Never hardcode model names in business logic.
- **Cost cap is enforced in `llm/budgets.py`** at the OpenRouter wrapper layer — every cloud call passes through it.
- **Every state transition is checkpointed** via `orchestrator/checkpoint.py`. A daemon killed mid-run must be resumable from the last checkpoint with `research resume <job-id>`.
- **Observability:** every tool call, model call, and decision emits a structured event to `jobs/<job-id>/events.jsonl` *and* mirrors into the SQLite `events` table. JSONL is the surface a future TUI/web UI tails — don't print-and-forget.
- **Sources stay deduped and archived.** A fetched URL is hashed (sha256), stored once under `sources/<sha256>.{md,json}`, and a Wayback save is attempted on first fetch.
- **Naming:** snake_case modules, PascalCase Pydantic models, verb-on-noun for CLI subcommands (`research start`, `research view`, mirroring `git`'s shape).
- **No planning docs as ad-hoc files in the repo root.** Plans go in the GitHub issue body or comments; long-form research goes in the existing top-level `*.md` playbooks (and links from issues). Don't create scratch `NOTES.md` / `PLAN.md` files.
- **Tests live in `tests/`** mirroring `src/research_agent/` layout; fixtures under `tests/fixtures/`.

## Validation playbook

Alpha-loop's preflight and post-change validation both run `test_command` from `.alpha-loop.yaml` (currently `uv run pytest -q`). That covers unit tests only. **Before opening the PR you must also run the CLI-level checks below** — pytest passing is necessary but not sufficient for a CLI/daemon project.

### Environment setup (one-time per worktree)
```bash
uv sync                                 # creates .venv from pyproject.toml + uv.lock
uv pip install -e .                     # editable install so `research ...` resolves
cp -n .env.example .env                 # then fill in OPENROUTER_API_KEY etc. (never commit)
```
LM Studio must be serving the configured local models on `http://localhost:1234/v1` for any test that hits a `fast_local`/`accurate_local` tier. If LM Studio is not running, skip those tests with `-m "not requires_lmstudio"` and note the skip in the PR.

### Static checks (run on every change)
```bash
uv run ruff check .                     # lint
uv run ruff format --check .            # formatting
uv run mypy src                         # type-check the package
uv run pytest -q                        # unit tests (same as test_command)
```
Each must exit 0. If pyproject.toml doesn't yet declare ruff/mypy configs, skip with a one-line note in the PR — but never silently bypass a failure.

### CLI wiring smoke tests (run after any change touching `src/research_agent/cli.py`, `daemon.py`, or `orchestrator/`)
```bash
uv run research --help                  # CLI imports cleanly, subcommands list
uv run research doctor                  # env health: API keys, LM Studio reachable, SQLite WAL writable, dirs exist
uv run research config get model_routing.fast    # config loads, routing resolves
```
`research doctor` is the canonical "is this thing wired up" check — its exit code is the truth. If you added a new dep, env var, or path, extend `doctor` so future runs catch a broken setup.

### Component smoke tests (run when you touch the matching layer)
```bash
# LLM router — verify the tier you changed actually hits the intended provider
uv run research _smoke-llm fast "Say hello in one word"            # must hit LM Studio
uv run research _smoke-llm frontier_speed "Say hello in one word"  # must hit OpenRouter (Haiku tier)

# Tool/connector you added or modified
uv run research _smoke-tool web_search "openai gpt-5"
uv run research _smoke-tool arxiv "agent orchestration"
# ...one per connector touched. Each must return a non-empty list[SearchResult].

# Cross-job search (after touching storage/db.py or storage/markdown.py)
uv run research search "test query" --all
```

### End-to-end smoke (run before merging anything that touches the orchestrator, daemon, or checkpointing)
```bash
uv run research start \
  --skip-intake \
  --goal "Investigate the history of the Python pickle module" \
  --time-cap 1 \
  --budget-usd 2
# Watch it:
uv run research logs <job-id> -f
# Inspect when it finishes (≤ 30 min):
uv run research view <job-id> --report
```
**Pass criteria:** the daemon exits 0, `jobs/<job-id>/report.md` exists and is non-trivial, `events.jsonl` has events from every stage (plan → search → extract → synth), and the SQLite `events` table mirrors the JSONL count exactly.

### Resume / crash-recovery smoke (run after touching `orchestrator/checkpoint.py` or `daemon.py`)
```bash
uv run research start --skip-intake --goal "..." --time-cap 1 &
sleep 60
uv run research stop <job-id> --kill           # SIGKILL the daemon mid-run
uv run research resume <job-id>                # must pick up from last checkpoint, not restart
uv run research view <job-id> --report
```

### Cost & budget guard (run after touching `llm/budgets.py` or `llm/openrouter.py`)
Run a normal e2e smoke with `--budget-usd 0.05` and confirm the daemon halts cleanly when the cap trips, writes a `BUDGET_EXCEEDED` event, and `report.md` documents the partial state — no half-written sidecars, no orphaned PID file.

### What to write in the PR body
Under a `## Validation` heading, paste:
- The exact commands you ran (copy-pasteable).
- Their exit codes / one-line summaries.
- Anything you skipped and why (e.g., "LM Studio not running locally — skipped `_smoke-llm fast`").

If a smoke test that *should* be relevant to your change isn't run, treat that as a blocker, not an oversight.

## Non-Negotiables
- **GitHub Issues are the source of truth.** No work without an issue; PRs must close their issue (`Closes #N`). See `CLAUDE.md` for the alpha-loop workflow — don't bypass it.
- **Never commit secrets or large artifacts.** `.env*` files, API keys (OpenRouter, Tavily, GitHub, Reddit, etc.), `*.gguf`/`*.safetensors`/`*.pt` model weights, and the contents of `data/`, `jobs/`, `runs/`, `logs/`, `sessions/`, `models/` are all gitignored — keep it that way. If a publishable artifact must be tracked, promote it to a curated dir and `git add -f` deliberately.
- **Outbound actions go through the judge gate.** Any task with `outbound: true` (email, FOIA, web form, voice) is held for the judge agent / human review queue — never let the planner or a worker dispatch outbound side-effects directly. (v1 keeps outbound out of scope; if you're adding it, you're adding the gate at the same time.)
- **SQLite uses WAL mode.** Don't disable it; concurrent reader (UI/CLI) + long-running writer (daemon) depends on it.
- **Local vs. cloud routing is not optional.** Cheap/iterative work (query rewriting, source filtering, per-source extraction) goes to LM Studio. Synthesis, planning rewrites, and gap analysis go to OpenRouter. Don't run cheap loops against the cloud — the cost cap will trip and the run will halt.
- **Don't break the per-job folder contract.** External tools (future UI, exporters, `research export`) read `jobs/<job-id>/` by convention. Renaming files or skipping sidecars silently breaks them.
- **Research playbooks are content, not scratch.** `ai-agent-investigation-playbook.md`, `ai-agent-research-setup.md`, and `research-agent-implementation-guide.md` are the project's deliverables-in-progress. Edit them deliberately (via an issue), not as a side effect of unrelated work.
