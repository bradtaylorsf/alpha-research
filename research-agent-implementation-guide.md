# Research Agent — Implementation Guide (v1, CLI-first)

A concrete, build-it-this-week guide for the autonomous research agent described at a strategic level in `ai-agent-research-setup.md` and the investigative-pattern playbook in `ai-agent-investigation-playbook.md`. This document focuses on **the v1 you actually ship** — a single-machine CLI tool that runs unattended for 48 hours to a week, uses **LM Studio for local research/gathering** and **OpenRouter for synthesis**, stores everything as **markdown files + a SQLite index**, and is structured so a basic UI can sit on top later without refactoring the core.

Written for: a single technical operator on a Mac, comfortable in Python, who wants minimal dependencies, full control, and no framework lock-in.

---

## 0. The locked-in v1 decisions

These are the calls. Each is justified in the section noted; flagged where the strategic doc takes a different position so you know what you're trading.

| Decision | v1 choice | Why |
|---|---|---|
| Language | **Python 3.12+** | Best ecosystem for research/RAG, native LM Studio + OpenRouter clients, mature async, Pydantic for typed state. (§2) |
| Agent framework | **Pydantic AI + a thin custom orchestrator** | Type-safe structured outputs, model-agnostic, lightweight. Same recommendation as the strategic doc (§2 there). (§3) |
| Durable execution | **Custom SQLite-backed task queue + WAL checkpoints** | The strategic doc recommends DBOS/Temporal on Postgres; you chose SQLite, so we hand-roll a small, dependency-free queue. Clean upgrade path to DBOS+Postgres if v2 outgrows it. (§4) |
| CLI | **Typer + Rich + Questionary** | Typer for commands (FastAPI-style), Rich for live progress, Questionary for the interactive intake. (§5) |
| Local model runtime | **LM Studio** at `http://localhost:1234/v1` | Your choice. OpenAI-compatible, MLX backend on Apple Silicon, idle-TTL auto-evict so you can keep many models "loaded" and let the daemon swap. |
| Cloud model gateway | **OpenRouter** at `https://openrouter.ai/api/v1` | Your choice. Lets you swap between Opus, Kimi K2, GPT-5, Gemini 2.5, etc. with a single API key + one client. Costs ~5% over native pricing. |
| Storage — content | **Markdown files** in `jobs/<job-id>/` with JSON sidecars | Human-readable, greppable, git-friendly, easy for a future UI to render. |
| Storage — index | **SQLite** at `data/index.sqlite` (WAL mode) | Single file, atomic, fast enough for cross-job search, week-long writes are no problem. |
| Sources (v1) | **Web search + fetch, local corpus, GitHub, arXiv, news, Reddit** | Per your selections. Per-source connector files; add more later. (§7) |
| Process model | **One daemon process per job**, PID file, can survive terminal exit | Simpler than a multi-job supervisor; a job is a self-contained unit you can `nohup`/`pm2`/`launchd` if you want. (§6) |
| Observability | **JSONL event log per job + SQLite events table** | Same data, two views; the JSONL is what a future TUI or web UI tails. (§8) |
| Future UI | **Stub `ui/` package; design schema + events so it can be added without touching the core** | FastAPI + HTMX *or* Textual TUI later, both work against the same SQLite + JSONL surface. (§11) |

**What this guide deliberately does NOT include:** the planner/judge/synthesizer agent prompts (those live in the agent roster section of `ai-agent-research-setup.md`), the legal/judge architecture (§7 of the setup doc), and the investigative playbooks (the playbook doc). This is the build, not the strategy.

---

## 1. Scope of v1

**In scope:**
- One binary: `research` CLI.
- `research start` → interactive intake → spawns a background daemon that runs autonomously for hours to a week.
- `research list` / `status` / `logs` / `stop` / `resume` / `view`.
- Local-model research loop (LM Studio): query rewriting, source filtering, extraction, per-source summarization.
- Cloud synthesis (OpenRouter): planning, gap analysis, final report generation.
- Sources: web search (Tavily or Brave), web fetch (httpx + readability), local corpus (recursive markdown/PDF), GitHub, arXiv, news (NewsCatcher or RSS), Reddit (PRAW).
- Markdown + JSON output to `jobs/<job-id>/`, SQLite index at `data/index.sqlite`.
- Crash recovery: every state transition is checkpointed; a daemon that's killed mid-run resumes from the last checkpoint.
- Cost cap per job (USD), enforced at the OpenRouter wrapper.

**Explicitly OUT of scope for v1** (bring them in v2 once v1 is stable):
- The judge agent / outbound action gating (Section 7 of the setup doc).
- Email/voice/FOIA outbound (Section 6 of the setup doc).
- Postgres / multi-machine scaling.
- A web UI (we'll design FOR one, not build one).
- Multi-investigation orchestration (one daemon = one investigation; run several in parallel by starting several daemons).

---

## 2. Why Python (and not TypeScript)

You asked me to pick. Python for these specific reasons, in order:

1. **Pydantic AI is the right framework** for what you want (type-safe agents, native LM Studio + Anthropic + OpenAI compat, durable-execution integrations) and it's Python. The TS equivalent (Mastra, Vercel AI SDK) is good but younger and weaker on local-model integration.
2. **The strategic doc is already specced in Python.** Reusing those agent definitions, prompt templates, and the planner/judge/verifier roster is a one-language exercise.
3. **The local-model and OSINT ecosystems are Python.** `llama-cpp-python`, `mlx-lm`, `arxiv`, `praw`, `sec-edgar-api`, `newspaper4k`, `trafilatura`, `playwright` (cross-language but its Python API is excellent), `pyalex`, `pypdf`, `unstructured` — all native Python.
4. **A future TS/React UI doesn't need TS at the core.** The UI sits over SQLite + JSONL; the core can be Python and the UI can be whatever.

Use TypeScript instead only if you intend to deploy the agent to a serverless edge runtime (you don't — it's a Mac daemon).

---

## 3. Why Pydantic AI + thin custom orchestrator (and not LangGraph or "build it all")

The strategic doc made this call already (§2.2 there); v1 honors it.

**What Pydantic AI gives you:**
- Typed `output_type=MySchema` with reflection/retry on schema violations. This is huge for a multi-stage research pipeline where every stage emits structured data.
- Model-agnostic provider layer: same `Agent(...)` works against `openai-compat:lmstudio`, `openai-compat:openrouter`, `anthropic`, `google`. You hand it a base URL and credentials.
- Native tool definitions as plain Python functions with type hints; the agent picks them up automatically.
- Native logging via Logfire or any OTEL collector.
- Lightweight (no graph compilation step, no checkpointer to configure).

**What you write yourself (the "thin orchestrator"):**
- The job lifecycle (start → intake → plan → loop → synthesize → finish).
- The SQLite-backed task queue.
- Checkpoint/resume.
- Model routing (LM Studio vs. OpenRouter, by tier).
- Storage (markdown writers, SQLite indexers).
- The CLI.

**Why not LangGraph for v1:** LangGraph's superpower is graph-level "time travel" — rewinding to any prior super-step. You don't need that for the first version. You need durable retry of individual stages and crash-resumable runs, which a few hundred lines of SQLite + checkpoint code give you. If v2 needs graph-level forking, the migration is mechanical (Pydantic AI agents drop into LangGraph nodes unchanged).

**Why not "build it all from scratch":** the part you'd build (typed agent calls + retry on schema failure + model routing) is exactly what Pydantic AI gives you for free. The part Pydantic AI doesn't give you (durable execution on SQLite, your specific job folder layout, your CLI) is exactly the part where you want full control.

---

## 4. Project layout

```
alpha-research/
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore
├── config/
│   ├── default.yaml          # global defaults
│   ├── models.yaml           # tier → model routing rules
│   └── sources.yaml          # which sources are enabled
├── src/
│   └── research_agent/
│       ├── __init__.py
│       ├── cli.py            # Typer entry point
│       ├── intake.py         # interactive Q&A
│       ├── daemon.py         # the long-running process
│       │
│       ├── orchestrator/
│       │   ├── __init__.py
│       │   ├── job.py        # Job lifecycle, state machine
│       │   ├── plan.py       # Planner (cloud) + tactical replan (local)
│       │   ├── loop.py       # Main research loop
│       │   ├── synth.py      # Synthesis stages
│       │   ├── critique.py   # Gap analysis
│       │   └── checkpoint.py # Save/restore state
│       │
│       ├── llm/
│       │   ├── __init__.py
│       │   ├── router.py     # tier → provider selection
│       │   ├── lmstudio.py   # local client wrapper
│       │   ├── openrouter.py # cloud client wrapper
│       │   ├── budgets.py    # cost cap enforcement
│       │   └── cache.py      # response cache (sqlite)
│       │
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── web_search.py # Tavily / Brave / Exa
│       │   ├── web_fetch.py  # httpx + trafilatura
│       │   ├── local_corpus.py
│       │   ├── github.py
│       │   ├── arxiv_tool.py
│       │   ├── news.py
│       │   ├── reddit.py
│       │   └── archive.py    # Wayback Machine save-on-fetch
│       │
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── jobs.py       # job folder management
│       │   ├── db.py         # SQLite schema + queries
│       │   ├── markdown.py   # MD writers / parsers
│       │   ├── sources.py    # source dedup, archival
│       │   └── findings.py   # finding objects, JSON sidecars
│       │
│       ├── observability/
│       │   ├── __init__.py
│       │   ├── events.py     # JSONL event log + SQLite mirror
│       │   ├── progress.py   # rich progress bars
│       │   └── telemetry.py  # OTEL spans (optional)
│       │
│       └── ui/               # placeholder — see §11
│           └── __init__.py
│
├── jobs/                     # research outputs (gitignored)
│   └── <job-id>/
│       ├── job.json          # canonical job metadata
│       ├── intake.json       # answers from the start prompts
│       ├── goal.md           # human-readable research goal
│       ├── plan/
│       │   └── 0001.md       # versioned plans
│       ├── findings/
│       │   ├── 000001.md
│       │   └── 000001.json   # sidecar: source, confidence, embeddings ref
│       ├── sources/
│       │   ├── <sha256>.md   # cleaned content
│       │   └── <sha256>.json # url, fetched_at, archive_url, hash
│       ├── synthesis/
│       │   ├── 0001.md       # synthesis pass output
│       │   └── 0001.json
│       ├── critique/
│       │   └── 0001.md
│       ├── report.md         # latest report (overwritten each synthesis)
│       ├── report.history/   # all prior reports, timestamped
│       ├── events.jsonl      # append-only event log
│       └── daemon.pid        # daemon PID (deleted on clean stop)
│
├── data/
│   ├── index.sqlite          # cross-job index, queues, checkpoints
│   └── llm_cache.sqlite      # response cache (separate so it can be wiped)
│
├── corpus/                   # your local research corpus (PDFs, notes)
│   └── <topic>/
│
└── tests/
    ├── test_cli.py
    ├── test_router.py
    ├── test_storage.py
    └── fixtures/
```

**Why this layout works:**
- **Per-job folder is self-contained** — you can zip a job and send it to someone, or git-track a single job, without touching anything else.
- **Markdown for content, JSON for metadata** — humans can read and edit findings; programs can index from sidecars.
- **`data/` for cross-job state** — task queue, checkpoint table, events mirror, embedding index, LLM cache. The future UI reads from here.
- **`config/` is YAML** — model routing and source enablement should be editable without touching code.
- **`ui/` is a stub** — same package, you add modules later (Textual app or FastAPI app) and they read from `data/` and `jobs/`.

---

## 5. CLI command surface

Built with Typer; subcommand structure follows git's "verb on noun" pattern.

```
research start [--corpus PATH] [--budget-usd N] [--time-cap HOURS]
    Launch interactive intake, then start a job daemon.

research list [--status STATE]
    List all jobs in jobs/ with status, age, cost, last activity.

research status <job-id> [--watch]
    Detailed status. --watch refreshes every 2s.

research logs <job-id> [-f] [--level INFO|DEBUG]
    Tail the event log; -f follows.

research stop <job-id> [--graceful|--kill]
    --graceful (default): writes a STOP signal; daemon finishes current
    task, runs a final synthesis pass, exits cleanly.
    --kill: SIGKILLs the PID; resume from last checkpoint with `resume`.

research resume <job-id>
    Restart the daemon from the last checkpoint.

research view <job-id> [--report|--findings|--sources]
    Opens the relevant markdown file in $EDITOR (or prints if no TTY).

research export <job-id> [--zip|--md-bundle]
    Bundle a job for sharing. --md-bundle concatenates the report,
    findings, and source list into a single markdown file.

research search "<query>" [--job ID|--all]
    Full-text + semantic search across findings/sources.

research config get|set <key> [<value>]
    Inspect or modify config/default.yaml or models.yaml.

research doctor
    Health check: LM Studio reachable, OpenRouter key valid, model
    aliases resolve, SQLite WAL writable, etc.
```

### 5.1 The intake flow

`research start` is interactive. The flow:

1. **Free-text prompt:** "Who or what do you want to research?"
2. **Goal clarification:** "In one sentence, what would a successful answer look like?"
3. **Domain selector** (multiple-choice via Questionary):
   - Political / corruption
   - Corporate / financial
   - Legal / regulatory
   - Technical / scientific
   - Media / public figure
   - Other (free text)
4. **Time cap:** 4h / 12h / 24h / 48h / 1 week / open-ended
5. **Budget cap (cloud only):** $5 / $25 / $100 / $500 / no cap
6. **Output orientation:** Substack-ready long-form / internal brief / raw findings dump / research dossier
7. **Aggressiveness:** conservative (verify everything, slow) / balanced / aggressive (more speculation, faster)
8. **Optional local corpus path:** "Any local files I should index first? (default: skip)"
9. **Adaptive follow-ups (1–3):** the agent (running on a local model — cheap) reads the answers so far and asks 1–3 topic-specific clarifying questions. Examples:
   - For "investigate XYZ Corp": "Are you most interested in (a) financials, (b) governance, (c) ESG/environmental, (d) all of the above?"
   - For "everything about person Y": "Is this for a profile/background piece or for an accountability investigation? They differ in tone and standard of evidence."
10. **Confirm:** the agent prints a 5-line summary of the planned investigation. User accepts (`y`) or revises (`n`, drops back into Q&A).
11. **Spawn daemon:** writes `intake.json` + `goal.md`, then `os.fork()` (or `subprocess.Popen`) the daemon, writes the PID file, returns control to the user.

The user sees: `Started job 2026-05-01-xyz-corp-financials (daemon pid 84211). Tail logs with: research logs 2026-05-01-xyz-corp-financials -f`

### 5.2 Stop semantics

- `research stop <job> --graceful`: writes `jobs/<job>/STOP` flag. The daemon checks this flag between every task. When set, it finishes the current task, runs one final synthesis pass to commit findings, writes `report.md`, deletes the PID file, exits 0.
- `research stop <job> --kill`: SIGTERM the PID, then SIGKILL after 10s if still running. The next `resume` picks up from the last checkpoint.
- The daemon also catches SIGTERM and SIGINT and degrades to the graceful path.

---

## 6. The daemon — long-running process design

This is the heart of the system. It must run unattended for hours to a week without losing state on a crash, a power loss, an OOM kill, or an LM Studio model swap stall.

### 6.1 Process supervision

For v1, **don't build a supervisor** — the daemon is a single Python process with a watchdog inside it. Outside-of-process supervision can be added by you (`launchd`, `pm2`, or a one-liner `while true; do research resume <job>; sleep 5; done`). Keeping the v1 daemon a single Python process also keeps debugging trivial.

The daemon's main loop:

```python
async def run_daemon(job_id: str):
    job = Job.load(job_id)
    job.set_status("running")
    install_signal_handlers(job)        # SIGTERM/SIGINT → graceful stop
    install_stop_flag_watcher(job)      # poll jobs/<id>/STOP every 2s
    await ensure_lm_studio_alive()      # health check, retry up to 60s
    await ensure_openrouter_reachable()

    plan = job.current_plan() or await initial_plan(job)
    while not job.should_stop() and not plan.is_complete():
        task = plan.next_task()
        await checkpoint(job, plan, task)
        try:
            result = await run_task(task, job)
            plan.record_result(task, result)
            if plan.should_synthesize():
                await synthesize(job, plan)
            if plan.should_critique():
                await critique(job, plan)
                plan = await replan(job, plan)
        except RetriableError as e:
            await backoff_and_retry(task, e)
        except FatalError as e:
            await escalate(job, task, e)

    await final_synthesis(job, plan)
    job.set_status("completed" if plan.is_complete() else "stopped")
    job.cleanup()
```

### 6.2 Checkpoint strategy

Checkpoints are written to SQLite at every state transition:

| Event | Checkpoint contents |
|---|---|
| Job started | full intake + initial plan |
| Task pulled from queue | task ID + plan version |
| Task completed | task result hash + plan deltas |
| Synthesis pass complete | synthesis version + cost-to-date |
| Critique complete | critique version + new plan version |
| Stop requested | reason + clean shutdown flag |

A checkpoint is **just a row in `data/index.sqlite`'s `checkpoints` table** with `(job_id, ts, kind, payload_jsonb)`. Resume reads the last row.

`fsync` after every checkpoint is overkill for week-long runs; trust SQLite WAL with `synchronous=NORMAL`. If you want belt-and-suspenders, set `synchronous=FULL` for checkpoints only.

### 6.3 Surviving a week

Things that break a week-long run if you don't plan for them:

1. **LM Studio model swap stalls.** When LM Studio idle-evicts a model and a request triggers a reload, the request can hang for 30–60 seconds. Wrap every local-LLM call in `asyncio.wait_for(timeout=120)`. On timeout, retry once after a 5s sleep; if still failing, mark the worker tier `degraded` and route that tier's tasks to OpenRouter for the next 10 minutes (with cost notification).
2. **Network blips on OpenRouter.** Tenacity-style exponential backoff: 1s, 2s, 4s, 8s, 16s, 30s, 60s — give up after ~2 minutes. Cache the partially-completed synthesis result so you can resume mid-stream.
3. **Disk fills up.** Source content is the biggest disk hog (you'll fetch and clean thousands of pages over a week). Cap per-job disk at `--disk-cap-gb` (default 10 GB); when exceeded, drop the lowest-relevance sources first.
4. **Macbook lid close.** The daemon survives sleep; just `caffeinate -i -w <pid>` is the right hygiene. Document in the README.
5. **macOS auto-reboot for updates.** Disable in System Settings; or set up `launchd` so the daemon restarts on boot and `resume`s automatically.
6. **OpenRouter cost runaway.** The budget cap is the safety. See §9.
7. **Infinite loops on the planner.** Hard cap on plan iterations (default: 200). On hit, stop and run one big final-synthesis pass.

### 6.4 The job state machine

```
pending  → running  → synthesizing  → critiquing  → replanning  → running  ...
              ↓                                                          ↓
              ↓                                                          ↓
              ├──→ stopping → stopped                       (loop until done or cap)
              ├──→ failed (with reason)                                  ↓
              └──→ completed (final report written)              completed
```

Stored as `status` column on the `jobs` table. The CLI's `list` command renders this with colors (running=green, stopping=yellow, failed=red).

---

## 7. Sources and tools

You said: web + local corpus + GitHub + arXiv + news + (likely) Reddit/social. v1 connector list:

| Tool | Library | Auth | Notes |
|---|---|---|---|
| Web search | **Tavily** (recommended) or Brave | API key | Tavily's `advanced` depth + `include_raw_content=true` returns clean content directly, fewer fetch calls. Brave is cheaper, less curated. |
| Web fetch | `httpx` + `trafilatura` (fallback `readability-lxml`) | none | Trafilatura wins for boilerplate removal on news/blogs; readability for academic. Always set a polite User-Agent and respect robots.txt. |
| Web fetch (JS-heavy) | `playwright` (lazy, only when fetch returns < 500 chars or fails) | none | Headless Chromium. Heavy; gate behind a `requires_js` flag the planner sets. |
| Wayback archival | `waybackpy` | none | On every successful fetch, fire-and-forget a Save Page Now. Stores `archive_url` in the source's JSON sidecar. |
| Local corpus | `unstructured` (PDFs, docx, html) + `pypdf` (fast PDF text) + native md/txt readers | none | One-time index on `research start --corpus PATH`. Embeds via LM Studio's `/v1/embeddings`. |
| GitHub | `httpx` against the REST API + `gh` CLI fallback | PAT | Repos, issues, code search, releases, contributor lists. |
| arXiv | `arxiv` Python lib | none | Search by query/author/date; fetches PDFs to local cache. |
| News | RSS feeds (built-in) + **NewsCatcher** (paid) for breadth | NewsCatcher API key (optional) | Start with curated RSS lists per domain (politics/business/tech). Add NewsCatcher only if RSS coverage is too thin. |
| Reddit | `praw` | OAuth | Subreddit search, comments, user posts. Stay within 60 req/min. |
| Hacker News | `httpx` against Algolia HN search | none | Free, fast, no key. |
| GDELT | `gdelt-doc-api` | none | Per the strategic doc; useful for global news anomaly detection. |
| SEC EDGAR | `sec-edgar-api` | User-Agent only | Auto-throttled to 10 req/sec. |
| FEC | `httpx` against OpenFEC | api.data.gov key | Per the strategic doc. |
| CourtListener / RECAP | `httpx` against the REST API | CourtListener token | Per the strategic doc. |

Each connector lives in `src/research_agent/tools/<name>.py` and exports two things:

```python
async def search(query: str, **kwargs) -> list[SearchResult]: ...
async def fetch(url: str, **kwargs) -> Source | None: ...
```

`SearchResult` and `Source` are Pydantic models with consistent fields (`url`, `title`, `snippet`, `published_at`, `source_kind`). The planner only ever sees these uniform shapes.

### 7.1 Tool registration with Pydantic AI

```python
from pydantic_ai import Agent
from .tools import web_search, web_fetch, github, arxiv_tool

researcher = Agent(
    "openai:lmstudio:qwen3-32b",
    system_prompt=RESEARCHER_PROMPT,
    tools=[web_search.search, web_fetch.fetch, github.search, arxiv_tool.search],
    output_type=ResearcherOutput,
)
```

The agent automatically gets the function signatures, type hints, and docstrings as tool definitions — no manual JSON schema authoring.

---

## 8. Model routing — LM Studio for gathering, OpenRouter for synthesis

### 8.1 Tier definitions

This is the routing table you put in `config/models.yaml`. Edit freely; the router reads it at startup.

```yaml
tiers:
  fast:
    provider: lmstudio
    model: qwen3-4b-instruct-q4_k_m
    timeout_s: 30
    purpose: |
      Classification, deduplication, language detection, simple
      relevance scoring. ~100 tok/s on M-series.

  general:
    provider: lmstudio
    model: qwen3-32b-instruct-q6_k
    timeout_s: 90
    purpose: |
      Research worker default. Query rewriting, source extraction,
      per-source summarization, finding generation.

  reasoner:
    provider: lmstudio
    model: deepseek-r1-distill-32b-q6_k
    timeout_s: 300
    purpose: |
      Complex extraction, hypothesis ranking, contradiction detection.
      Use sparingly.

  vision:
    provider: lmstudio
    model: qwen3-vl-8b-instruct
    timeout_s: 60
    purpose: PDF page screenshots, chart reading.

  embeddings:
    provider: lmstudio
    model: qwen3-embedding-4b
    purpose: Semantic search across findings and sources.

  frontier:
    provider: openrouter
    model: anthropic/claude-opus-4-7
    fallback_model: openai/gpt-5
    timeout_s: 600
    purpose: |
      Major synthesis, critique, final report, planner rewrites.
      Cost-tracked.

  frontier_alt:
    provider: openrouter
    model: moonshotai/kimi-k2-1t
    timeout_s: 600
    purpose: |
      Diverse second opinion. Use for the critique pass so the
      synthesizer and critic disagree productively.

  frontier_speed:
    provider: openrouter
    model: anthropic/claude-haiku-4-5
    timeout_s: 60
    purpose: |
      Fast cloud calls when a local model isn't enough but Opus is
      overkill. Adaptive intake follow-ups, micro-summaries.
```

### 8.2 The router

```python
# src/research_agent/llm/router.py

from typing import Literal
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.models.openai import OpenAIModel

Tier = Literal["fast", "general", "reasoner", "vision", "frontier",
               "frontier_alt", "frontier_speed", "embeddings"]

class Router:
    def __init__(self, config: dict, budget: BudgetTracker):
        self.config = config
        self.budget = budget

    def model_for(self, tier: Tier) -> OpenAIModel:
        spec = self.config["tiers"][tier]
        if spec["provider"] == "lmstudio":
            return OpenAIModel(
                spec["model"],
                provider=OpenAIProvider(
                    base_url="http://localhost:1234/v1",
                    api_key="lm-studio",  # ignored
                ),
            )
        elif spec["provider"] == "openrouter":
            return OpenAIModel(
                spec["model"],
                provider=OpenAIProvider(
                    base_url="https://openrouter.ai/api/v1",
                    api_key=os.environ["OPENROUTER_API_KEY"],
                ),
            )
        raise ValueError(f"unknown provider: {spec['provider']}")

    async def call(self, tier: Tier, agent: Agent, *args, **kwargs):
        if self.config["tiers"][tier]["provider"] == "openrouter":
            self.budget.precheck(tier)
        try:
            result = await agent.run(*args, **kwargs)
        except OpenAIRateLimitError:
            return await self._fallback(tier, agent, *args, **kwargs)
        if self.config["tiers"][tier]["provider"] == "openrouter":
            self.budget.charge(tier, result.usage)
        return result
```

The agents themselves are constructed lazily and cached:

```python
@cache
def planner_agent(router: Router) -> Agent:
    return Agent(
        router.model_for("frontier"),
        system_prompt=PLANNER_PROMPT,
        output_type=Plan,
    )

@cache
def researcher_agent(router: Router) -> Agent:
    return Agent(
        router.model_for("general"),
        system_prompt=RESEARCHER_PROMPT,
        output_type=ResearcherOutput,
        tools=ALL_RESEARCH_TOOLS,
    )
```

### 8.3 Why OpenRouter (not native Anthropic + native OpenAI)

You explicitly want to swap between Opus, Kimi K2, OpenAI, etc. OpenRouter lets you do that with **one API key, one endpoint, and one client** — no per-provider SDK juggling. The trade is ~5% over native pricing, which is well worth it for a personal-tool single-machine build.

If you later find a specific feature you need that OpenRouter doesn't expose (e.g., Anthropic prompt caching for very long judge prompts), drop down to the native SDK for that one tier. The router's per-tier provider field makes this trivial.

---

## 9. Cost cap and budget tracking

Single class, lives at `src/research_agent/llm/budgets.py`:

```python
class BudgetTracker:
    def __init__(self, job_id: str, cap_usd: float | None):
        self.job_id = job_id
        self.cap = cap_usd
        self.spent = self._load_from_db()

    def precheck(self, tier: str):
        if self.cap is None: return
        if self.spent >= self.cap:
            raise BudgetExceeded(self.job_id, self.spent, self.cap)
        if self.spent >= 0.9 * self.cap:
            log_warning(f"budget 90% used: ${self.spent:.2f} / ${self.cap:.2f}")

    def charge(self, tier: str, usage: TokenUsage):
        cost = self._compute_cost(tier, usage)
        self.spent += cost
        self._persist(tier, usage, cost)
```

`_compute_cost` reads per-model pricing from `config/models.yaml` (you'll keep this updated manually; OpenRouter's pricing API is also fine but adds a dependency).

When `BudgetExceeded` raises, the daemon catches it, runs one final synthesis pass with whatever budget remains (or skips if zero), writes `report.md`, marks the job `completed (budget cap)`, and exits.

---

## 10. SQLite schema (the cross-job index)

One file, `data/index.sqlite`. WAL mode. ~12 tables.

```sql
-- Jobs
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,                -- '2026-05-01-xyz-corp-financials'
    goal TEXT NOT NULL,
    domain TEXT,
    status TEXT NOT NULL,               -- pending|running|synthesizing|...|completed|failed
    intake_json TEXT NOT NULL,          -- the full intake answers
    time_cap_hours INTEGER,
    budget_cap_usd REAL,
    aggressiveness TEXT,                -- conservative|balanced|aggressive
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    last_activity_at INTEGER,
    pid INTEGER,
    cost_so_far_usd REAL DEFAULT 0
);

-- Plan versions
CREATE TABLE plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,         -- the full Plan
    created_at INTEGER NOT NULL,
    UNIQUE(job_id, version)
);

-- Tasks (the queue)
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    plan_version INTEGER NOT NULL,
    kind TEXT NOT NULL,                 -- 'web_search', 'fetch', 'extract', ...
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,               -- pending|running|done|failed|skipped
    parent_task_id INTEGER REFERENCES tasks(id),
    depth INTEGER DEFAULT 0,
    started_at INTEGER,
    finished_at INTEGER,
    result_json TEXT,
    error TEXT,
    retry_count INTEGER DEFAULT 0
);
CREATE INDEX idx_tasks_status_job ON tasks(job_id, status);

-- Findings
CREATE TABLE findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    md_path TEXT NOT NULL,              -- relative path under jobs/<job>/findings/
    claim TEXT NOT NULL,
    confidence REAL NOT NULL,
    source_ids TEXT NOT NULL,           -- JSON array of source IDs
    contradicts TEXT,                   -- JSON array of finding IDs
    embedding BLOB,                     -- 1024-d float32, packed
    tags TEXT,                          -- JSON array
    created_at INTEGER NOT NULL
);
CREATE INDEX idx_findings_job ON findings(job_id);

-- Sources (deduplicated across jobs)
CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,        -- of cleaned content
    url TEXT,
    title TEXT,
    fetched_at INTEGER NOT NULL,
    archive_url TEXT,                   -- Wayback Machine URL
    md_path TEXT NOT NULL,              -- relative under jobs/<job>/sources/
    kind TEXT,                          -- 'web', 'pdf', 'github', 'arxiv', ...
    embedding BLOB
);

-- Job ↔ source many-to-many
CREATE TABLE job_sources (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    source_id INTEGER NOT NULL REFERENCES sources(id),
    PRIMARY KEY (job_id, source_id)
);

-- Synthesis passes
CREATE TABLE syntheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    version INTEGER NOT NULL,
    md_path TEXT NOT NULL,
    model TEXT NOT NULL,
    cost_usd REAL,
    created_at INTEGER NOT NULL,
    UNIQUE(job_id, version)
);

-- Checkpoints (one per state transition)
CREATE TABLE checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL,                 -- 'job_started', 'task_done', 'synthesis_done', ...
    payload_json TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE INDEX idx_checkpoints_job_ts ON checkpoints(job_id, ts);

-- Events (mirror of events.jsonl, for SQL queries from the future UI)
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,                -- DEBUG|INFO|WARN|ERROR
    actor TEXT,                         -- 'planner'|'researcher'|'router'|...
    kind TEXT NOT NULL,
    payload_json TEXT
);
CREATE INDEX idx_events_job_ts ON events(job_id, ts);

-- LLM call ledger (cost tracking)
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT REFERENCES jobs(id),
    ts INTEGER NOT NULL,
    tier TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    latency_ms INTEGER,
    cost_usd REAL,
    finish_reason TEXT
);

-- FTS5 over findings.claim and sources.title
CREATE VIRTUAL TABLE findings_fts USING fts5(
    claim, content=findings, content_rowid=id
);
CREATE VIRTUAL TABLE sources_fts USING fts5(
    title, content=sources, content_rowid=id
);
```

Use `sqlite-vec` (the SQLite extension) or roll your own cosine search over the BLOB embeddings — for v1, a Python-side numpy cosine is fine up to ~100K vectors. Switch to `sqlite-vec` (KNN in SQL) once a single job has more than a few thousand findings.

---

## 11. Designing for the future UI without building it

The UI will be a separate project that talks to the same `data/index.sqlite` and reads `jobs/<job>/` markdown directly. To make that easy, follow these rules in v1:

1. **Every state-changing operation writes one event.** Append-only `events.jsonl` per job + mirror to `events` table. The UI builds its activity feed by tailing the JSONL or selecting from the table.
2. **Reports are versioned files**, not in-place updates. `report.md` is always the latest; prior versions live in `report.history/`. The UI shows a timeline.
3. **Findings have stable IDs** (the auto-increment from SQLite). Markdown filenames embed the ID (`000042.md`) so the UI can deep-link.
4. **No UI-only fields in tables.** If the UI needs derived data (e.g., word count, reading time), it computes that itself.
5. **The CLI's `status` command outputs JSON when stdout isn't a TTY** (`--json` flag). The UI's "live status" can shell out to this if it doesn't want to read the DB directly.

When you're ready to build the UI, two paths:

**Path A — Textual TUI** (terminal-based, fast to build, no auth needed): one `App` with three views — Jobs list, Job detail, Findings browser. ~500 lines. `pip install textual`. Tail `events.jsonl` with `watchfiles`.

**Path B — FastAPI + HTMX + Tailwind** (web-based, shareable on the LAN, screenshotable): one FastAPI app, one set of HTMX-driven HTML templates, server-sent events for live progress. ~1000 lines. Read SQLite directly; serve markdown via `markdown2` or `mistune`.

Both options use the **same underlying surface** — that's the point.

---

## 12. Phased build plan

Each phase produces a runnable, testable thing. Don't skip the test step.

### Phase 0 — Skeleton (1 evening)

- Create the project layout above.
- `pyproject.toml` with Pydantic v2, Pydantic AI, Typer, Rich, Questionary, httpx, trafilatura, sqlite-utils, structlog.
- `research doctor` subcommand that reports environment health.
- `research --version`.

**Done when:** `pip install -e . && research doctor` prints a nicely formatted health report.

### Phase 1 — Storage + CLI shell (1–2 days)

- Implement `storage/db.py` with the schema above; `migrate()` function.
- Implement `storage/jobs.py` (create folder, write intake/goal, list jobs).
- Implement `storage/markdown.py` (finding writer, source writer with deduplication by sha256).
- CLI commands: `start --skip-intake --goal "X"` (for testing), `list`, `status`, `view`.
- Synthetic test: create a fake job with hand-written findings and verify `list`/`status`/`view` work.

**Done when:** you can hand-create a job folder, register it via Python, and query it through every CLI verb.

### Phase 2 — LLM router + minimal Pydantic AI integration (1 day)

- Implement `llm/router.py` and `llm/lmstudio.py`, `llm/openrouter.py`.
- Implement `llm/budgets.py`.
- Implement `llm/cache.py` (sqlite-backed, key on hash of prompt + model + params).
- Smoke test: `research _smoke-llm fast "Say hello in one word"` should hit LM Studio; `research _smoke-llm frontier_speed "Say hello in one word"` should hit OpenRouter Haiku.

**Done when:** a single LLM call with structured output (`output_type=str`) works at every tier.

### Phase 3 — Tools / connectors (2–3 days)

- Implement web_search, web_fetch (trafilatura), local_corpus, github, arxiv_tool, news (RSS), reddit, archive (Wayback).
- Each connector has a CLI smoke command: `research _smoke-tool web_search "openai gpt-5"`.
- `Source` deduplication via sha256.

**Done when:** every connector returns a `list[SearchResult]` or `Source` shape, and `research search "..." --all` returns hits from all enabled sources.

### Phase 4 — The orchestrator loop (2–3 days)

- Implement `orchestrator/plan.py` (planner agent that takes intake → returns initial Plan).
- Implement `orchestrator/loop.py` (queue puller, task runner, retry).
- Implement `orchestrator/synth.py` (synthesizer agent).
- Implement `orchestrator/critique.py` (critique with a different model than synthesis).
- Implement `orchestrator/checkpoint.py` (save/restore plan + queue state).

**Done when:** `research start --skip-intake --goal "Investigate company X" --time-cap 1` runs to completion and writes a coherent `report.md` in under 30 minutes.

### Phase 5 — Daemonization + lifecycle (1–2 days)

- The interactive intake (`intake.py`).
- Spawn the daemon via `subprocess.Popen` with a detached process group; write the PID file.
- Signal handlers; STOP-flag polling; graceful stop.
- `research resume <id>` reads the last checkpoint and restarts.

**Done when:** you can `research start`, close your terminal, come back in 4 hours, `research stop --graceful`, and have a clean report.

### Phase 6 — Long-run hardening (2–3 days)

- LM Studio health checking with auto-recovery.
- Per-tier timeout + fallback.
- Disk cap enforcement.
- Cost cap final-pass enforcement.
- 24-hour soak test on a real research goal; review the events log; fix what breaks.

**Done when:** a 24-hour run completes without manual intervention.

### Phase 7 — Polish (ongoing)

- Better progress bars (Rich live updates).
- `research export` (zip + concatenated md bundle).
- `research search` semantic + FTS hybrid.
- Documentation in the README for `launchd`, `caffeinate`, etc.

---

## 13. Dependency list (`pyproject.toml` essentials)

```toml
[project]
name = "research-agent"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.7",
    "pydantic-ai>=0.0.40",
    "typer>=0.12",
    "rich>=13.7",
    "questionary>=2.0",
    "httpx>=0.27",
    "trafilatura>=1.12",
    "readability-lxml>=0.8",
    "playwright>=1.45",        # lazy import
    "structlog>=24.1",
    "sqlite-utils>=3.36",
    "numpy>=1.26",
    "tenacity>=8.2",
    "watchfiles>=0.21",
    "pyyaml>=6.0",
    "python-dotenv>=1.0",
    # connectors
    "praw>=7.7",
    "arxiv>=2.1",
    "feedparser>=6.0",
    "waybackpy>=3.0",
    "pypdf>=4.2",
    "unstructured>=0.14",      # heavy; consider extras
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "ruff>=0.4", "mypy>=1.10"]
ui  = ["textual>=0.70"]        # phase 8+
web = ["fastapi>=0.110", "uvicorn>=0.30", "jinja2>=3.1"]

[project.scripts]
research = "research_agent.cli:app"
```

`.env.example`:
```
OPENROUTER_API_KEY=
TAVILY_API_KEY=
GITHUB_TOKEN=
NEWSCATCHER_API_KEY=
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=research-agent/0.1
```

---

## 14. What changes for v2 (so you don't paint yourself into a corner)

The decisions that are easy to change later:
- **SQLite → Postgres**: every query is parameterized; ORM-free. Migration is mechanical.
- **Single-job daemon → DBOS-managed multi-job**: wrap the orchestrator loop in a `@dbos.workflow`; the queue table becomes a DBOS workflow queue. Existing job folders work unchanged.
- **OpenRouter → native Anthropic for prompt caching**: per-tier provider field already supports this; flip one line in `models.yaml`.
- **Add the judge agent (§7 of the strategic doc)**: drop a new module under `orchestrator/` that intercepts any task with `outbound: true`. The current v1 has no outbound tasks, so this is purely additive.
- **Add the UI (§11)**: a new top-level package; touches no v1 code.

The decisions that are hard to change later (so get them right now):
- **Job folder layout** — every part of the system depends on it. Lock it in Phase 1.
- **SQLite schema** — migrations are doable but annoying. Get the field names right; prefer too many columns to too few.
- **Event schema** — once you start writing JSONL, changing the field names invalidates old events. Use Pydantic models for events and version them with a `schema_version` field.

---

## 15. First-run checklist

Once Phase 6 is done, the first real run:

1. Pick a low-stakes target. The strategic doc's recommendation: **George Santos backtest** (Playbook §7b, paragraph 3 of the playbook doc) — public records sufficient, known answer, validates end-to-end.
2. `research start`, choose Time cap = 12h, Budget cap = $25, Aggressiveness = conservative.
3. `research logs <id> -f` in another terminal; let it run.
4. After 12 hours: `research view <id> --report`. Compare against the known timeline.
5. Score: did it surface the $0→$11M wealth jump? Did it find the Brazilian fraud charges? Did it cite the North Shore Leader?
6. Tune `models.yaml` and the planner prompt based on what it missed.
7. **Then** point it at something live.

---

## 16. Anti-patterns to avoid

- **Don't put prompts in code.** Prompts go in `prompts/*.md` and are loaded at startup. This lets you A/B test and check prompts into git as text diffs.
- **Don't let the agent decide when it's done.** Use a hard task cap *and* a budget cap *and* a time cap. The "is this enough?" judgment is genuinely hard for LLMs over week-long horizons.
- **Don't share state between jobs in memory.** Every cross-job query goes through SQLite. Two daemons running concurrently must not race on Python-level state.
- **Don't silently fall back from local to cloud.** If LM Studio dies, that's a *log warning* and a *cost notification* — not a quiet rerouting. The user's spend should never silently increase.
- **Don't tail the live `report.md` from the user's editor while the daemon writes it.** Always write to `report.md.tmp` then atomic rename. Same for `findings/*.md`.
- **Don't skip the archive step.** Wayback every fetched URL. Sources disappear; week-long runs expose this constantly.

---

## Appendix A — Why not LangGraph (longer answer)

LangGraph is the strongest framework for stateful agent workflows in Python today. The reason it's not the v1 pick:

1. **You don't yet need graph-level time travel.** That's LangGraph's killer feature. Without it, you're using LangGraph for its checkpointer, which is ~200 lines of SQLite code in our case.
2. **The mental overhead of graph compilation** (defining nodes, edges, conditional edges, state reducers) is real. For a single operator iterating fast, the linear `while` loop in §6.1 is easier to debug.
3. **Pydantic AI's typed-output story is cleaner.** LangGraph has output parsers; Pydantic AI has `output_type=` with reflection retry baked in.
4. **Migration cost is low.** Pydantic AI agents drop directly into LangGraph nodes. If v2 needs forking/branching investigations, the lift is real but bounded — and you'll have learned what nodes you actually need by then.

If you find yourself in v1 wanting any of: (a) "rewind this investigation to before the Tuesday night re-plan and try a different direction," (b) "fork this investigation into three parallel hypotheses and run all three," or (c) "run the same plan with a different model selection and diff the results," — that's LangGraph. Add it then.

---

## Appendix B — Why not the Claude Agent SDK

Briefly: the Agent SDK is great when you want Claude to drive everything end-to-end with file/bash/web tools. Two reasons it's not the v1 pick here:

1. You explicitly want to route most calls to **local models** and **non-Anthropic frontier models** through OpenRouter. The Agent SDK is, by design, Anthropic-first.
2. You want full ownership of the orchestration loop (week-long runs, checkpoints, your specific source list). The Agent SDK is a great driver but not a great library for "I want to call this primitive seven times in a specific shape." Pydantic AI is.

The Agent SDK *would* be a good fit if/when you build the **judge agent** (§7 of the strategic doc) — a stateless Opus call that needs file-read tools to pull cited evidence. Slot it in as the judge, not the orchestrator.

---

## Appendix C — Minimal `cli.py` skeleton

To make the layout concrete, here's the entry point you'd start with in Phase 1:

```python
import typer
from rich.console import Console
from .intake import run_intake
from .daemon import spawn_daemon
from .storage.jobs import Job, list_jobs, load_job

app = typer.Typer(no_args_is_help=True, help="Autonomous research agent")
console = Console()

@app.command()
def start(
    corpus: str = typer.Option(None, help="Path to local corpus to index"),
    budget_usd: float = typer.Option(None, help="Cloud spend cap"),
    time_cap: int = typer.Option(None, help="Time cap in hours"),
    skip_intake: bool = typer.Option(False, hidden=True),
    goal: str = typer.Option(None, hidden=True),
):
    """Launch interactive intake then start a research daemon."""
    if skip_intake:
        intake = {"goal": goal, "domain": "general", "time_cap": time_cap,
                  "budget_usd": budget_usd}
    else:
        intake = run_intake(corpus=corpus, budget_usd=budget_usd, time_cap=time_cap)
    job = Job.create(intake)
    pid = spawn_daemon(job.id)
    console.print(f"[green]Started job[/green] {job.id} (daemon pid {pid})")
    console.print(f"Tail logs with: research logs {job.id} -f")

@app.command("list")
def list_cmd(status: str = typer.Option(None)):
    """List all jobs."""
    jobs = list_jobs(status=status)
    # ... render with rich.table.Table

@app.command()
def status(job_id: str, watch: bool = False):
    """Show job status."""
    job = load_job(job_id)
    # ... render

@app.command()
def stop(
    job_id: str,
    graceful: bool = typer.Option(True, "--graceful/--kill"),
):
    """Stop a running daemon."""
    job = load_job(job_id)
    if graceful:
        job.request_stop()
    else:
        job.kill()

# ... resume, view, logs, search, export, doctor, config

if __name__ == "__main__":
    app()
```

That's the skeleton. The rest of the guide tells you what fills in the modules `intake`, `daemon`, `storage.jobs`, etc.

---

## Closing

Build Phase 0 → Phase 6 in order. Don't add the judge, don't add outbound, don't add the UI until v1 has logged a successful 24-hour soak run. The temptation to plan v2 features into v1 is the single biggest risk to actually shipping.

When v1 is solid, the natural next steps in order are: (1) the judge agent and outbound gating from §7 of the setup doc, (2) the FOIA/email outbound from §6, (3) the basic UI from §11 here. By then you'll know exactly what shape they need.
