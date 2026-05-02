# Designing an Autonomous Overnight Investigative Research Agent on a Mac M5 Ultra

*A complete, opinionated technical playbook for a hands-on builder running local LLMs via LM Studio, escalating to Claude Opus 4.7 for synthesis, and pursuing political corruption and California family law investigations with full audit + judge-validated outbound action.*

This report is written for a technical CEO who already lives in Claude Code, knows what a planner-worker-judge pattern is, and wants concrete recommendations rather than abstract architecture diagrams. Where there are tradeoffs, I make a call. Where there is genuine uncertainty (e.g., M5 Ultra Mac Studio has not formally shipped as of late April 2026), I flag it.

---

## 1. System Architecture

### 1.1 Recommended overall shape

Build a **single-process, async-first, durable orchestrator on the Mac** with a small number of long-lived components, persistent state in Postgres, and stateless workers. Treat the overnight run as one or more **investigations** decomposed into a tree of **tasks** that flow through a queue. Concretely:

- **Orchestrator (planner loop)** — owns the investigation tree, decides what to enqueue next, and triggers periodic Opus synthesis.
- **Worker pool** — async Python (or TypeScript) workers that pull tasks: API queries, web scrapes via Playwright, document analysis, entity extraction, RAG queries.
- **Judge agent** — a separate worker tier that intercepts every outbound action (email, FOIA submission, voice call, web form) and scores it before execution.
- **Synthesizer (Opus tier)** — invoked on a cadence to compress findings, regenerate hypotheses, and rewrite the plan.
- **Observability bus** — every tool call, model call, and decision flows through structured logs (OpenTelemetry spans + a Postgres `event_log` table).
- **Human review queue** — anything the judge sends to "needs human" goes here; the system pauses the relevant branch but keeps everything else running.

The mental model is closer to Pregel/super-step graph execution than to a simple chain — each tick, the planner reads global state, chooses next actions, dispatches them, waits for results, and decides whether to re-plan. LangGraph's checkpointing model formalizes this exact pattern for agentic workflows ([LangChain](https://docs.langchain.com/oss/python/langgraph/durable-execution)).

### 1.2 Orchestration: planner-worker-judge

Use a strict **plan → fan-out → judge-gate → execute → synthesize** loop:

1. **Plan.** The planner (small local model for cheap re-planning, Opus for big rewrites) consults the investigation goal, current findings graph, and recent failures. It emits a list of typed tasks: `query_fec`, `scrape_secstate`, `extract_entities_from_pdf`, `cross_reference_donors_to_pacs`, etc.
2. **Fan-out.** Tasks are enqueued in a Postgres-backed queue (the simplest durable option — a `tasks` table with `pending|running|done|failed` status, locked via `SELECT ... FOR UPDATE SKIP LOCKED`). Workers pull, execute, write results, write events.
3. **Judge gate.** Any task with `outbound: true` (email, call, form fill, FOIA) is held; the judge agent scores and either approves, rejects with reason, or escalates to the human queue.
4. **Execute.** Approved actions run; their results (delivery confirmations, voicemails transcribed, response received) are written back as new findings.
5. **Synthesize.** On a cadence, Opus reads the current state and rewrites the plan.

**Cadence recommendation for Opus synthesis (the hardest design decision in this system):**

- **Tick-driven re-plan**: every 30–45 minutes of wall-clock time during the overnight run.
- **Event-driven re-plan**: whenever ≥ N (e.g., 25) high-value findings have accumulated, OR a worker reports a strong signal (e.g., LLC ownership chain unmasked, suspicious donation pattern detected).
- **Failure-driven re-plan**: whenever ≥ 3 consecutive worker failures occur on the same branch, kick to Opus to decide whether to abandon, retry differently, or escalate.
- **Final synthesis pass**: one heavyweight Opus run at the end of the night that produces the morning report.

For an 8-hour overnight run this typically yields **~12–16 Opus synthesis calls**, with a final long pass. Each call uses prompt caching aggressively against a stable system prompt + tool definitions block; that delivers up to 90% cost savings on cached input ([Anthropic pricing docs](https://platform.claude.com/docs/en/about-claude/pricing)).

### 1.3 Local LLM ↔ Opus integration

LM Studio exposes both an OpenAI-compatible REST endpoint at `http://localhost:1234/v1/*` and an Anthropic-compatible Messages API ([LM Studio docs](https://lmstudio.ai/docs/developer/openai-compat), [LM Studio API](https://lmstudio.ai/docs/api/)). Use it as follows:

- All worker LLM calls go through a thin **router** library you write yourself. The router accepts a `tier` parameter (`fast`, `general`, `reasoner`, `frontier`) and maps to:
  - `fast` → Qwen 3 4B / Phi-4-mini at 4-bit on LM Studio (classification, deduplication, simple extraction)
  - `general` → Qwen3 32B or Llama 3.3 70B Q6 on LM Studio (most worker reasoning)
  - `reasoner` → DeepSeek-R1-distill or Qwen3-Next-80B-A3B on LM Studio (complex extraction, hypothesis ranking)
  - `frontier` → `claude-opus-4-7` via Anthropic API (synthesis, judge, final report)
- The router handles retry + exponential backoff, structured output validation (Pydantic), and per-tier timeouts. Normalize on the OpenAI Chat Completions schema for everything except Opus, which uses Messages — that schema delta is the only painful part and is well worth wrapping.
- Do **not** try to run Opus and the local models through the same client surface; the new Opus 4.7 API is more constrained (no `temperature`/`top_p`/`top_k`, removed extended thinking budgets, adaptive thinking only) and you will conflate concerns ([Anthropic's What's-New for Opus 4.7](https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-7)).

### 1.4 Data layer: hybrid, with Postgres as the spine

Use **Postgres + pgvector + Apache AGE (or recursive CTEs) + SQLite for durable queues**, not a graph database. Reasoning:

- For the scale you're working at (a single investigator, even running many overnight runs, generates O(10⁵–10⁶) entities and edges, not O(10⁸)), Postgres with `pgvector` and either Apache AGE or recursive CTEs handles the knowledge graph well, beats Elasticsearch on small-to-medium retrieval workloads, and crucially keeps everything in one transactional store ([DEV/Postgres knowledge graph](https://dev.to/micelclaw/4o-building-a-personal-knowledge-graph-with-just-postgresql-no-neo4j-needed-22b2); [Hugging Face benchmark](https://huggingface.co/blog/ImranzamanML/pgvector-vs-elasticsearch-vs-qdrant-vs-pinecone-vs)).
- **Neo4j** is overkill until you have a true network-analysis-first investigation. For political corruption work specifically, the queries that matter are 2–4 hop traversals (donor → committee → candidate; LLC → registered agent → other LLCs at same address); these are fine in Postgres with a `relationships(src, src_type, dst, dst_type, rel_type, evidence_jsonb)` table.
- **Qdrant** wins at very large scale and complex filtered retrieval; pgvector with HNSW + pgvectorscale wins on throughput for filter-heavy queries up to tens of millions of vectors ([Tigerdata 50M benchmark](https://www.tigerdata.com/blog/pgvector-vs-qdrant)). For an overnight personal investigator, the simplification of one database wins.
- Use **SQLite** as a sidecar for the durable task queue and ephemeral browser session state. SQLite WAL is rock-solid for single-machine queues.
- Use a **document store layer** (just a `documents` table with `raw_path`, `extracted_text`, `embedding`, `chunks_jsonb`) and treat scraped PDFs/HTML uniformly.

Recommended tables (sketch):
```
investigations (id, goal, status, created_at)
tasks (id, investigation_id, type, payload_jsonb, status, parent_id, depth, created_at, started_at, finished_at, error)
entities (id, kind, canonical_name, aliases_jsonb, attrs_jsonb, embedding vector(1024))
relationships (src, src_kind, dst, dst_kind, rel_type, weight, evidence_ids[], first_seen, last_seen)
documents (id, source_url, sha256, raw_path, kind, extracted_text, embedding, ingested_at)
findings (id, investigation_id, claim, confidence, supporting_doc_ids[], contradicting_doc_ids[])
events (id, ts, span_id, parent_span_id, actor, kind, payload_jsonb)  -- structured audit log
outbound_actions (id, kind, target, body, judge_score, judge_rationale, status, executed_at, response_jsonb)
human_review_queue (id, action_id, reason, created_at, resolved_at, decision)
```

### 1.5 State, checkpointing, resumability

Two layers:

- **Per-task durability**: every worker writes its result and updates task status in the same Postgres transaction. If the machine reboots mid-task, the task returns to `pending` and a worker re-picks it. Treat all worker actions as idempotent or at least replay-safe; for outbound actions store an `idempotency_key` (e.g., `sha256(target + body + day)`) so duplicate sends are caught at the judge layer.
- **Per-investigation checkpoint**: at every Opus synthesis tick, snapshot the *plan* and *findings DAG* into a `checkpoints` table. This gives you LangGraph-style "time travel" — you can fork an investigation from any prior synthesis point. Pydantic AI + DBOS or Pydantic AI + Temporal can do this off the shelf, but for one-machine deployment Postgres-native checkpointing is simpler ([LangGraph durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution); [Pydantic AI + DBOS](https://pydantic.dev/articles/pydantic-ai-dbos)).

### 1.6 Observability and audit logging

This is non-negotiable for the use case. Required signals:

- **Every model call**: model id, tier, prompt hash, input tokens, output tokens, latency, cache hit/miss, finish reason. Use OpenTelemetry spans; Logfire/Pydantic AI emits these natively ([Pydantic AI](https://ai.pydantic.dev/)).
- **Every tool call**: tool name, args (truncated/hashed), result hash, latency, errors.
- **Every outbound action**: full body, recipient, judge score, judge rationale, send-time response, idempotency key. This is what makes the system legally defensible.
- **Every state mutation**: append-only events table, never UPDATE-in-place on findings (versioned).
- **Cost meter**: rolling sum of Opus tokens, with a hard kill switch when daily budget exceeds threshold.

Run a small Grafana + Tempo + Postgres-as-log-store stack locally if you want pretty graphs, but the events table alone with a few SQL views (`v_outbound_today`, `v_judge_rejections`, `v_high_confidence_findings`) gets you 90% there.

---

## 2. Agent Framework Recommendation

### 2.1 Comparison of current frameworks (April 2026)

| Framework | Local LLM (OpenAI-compat) | Anthropic API | Long-horizon / checkpoint | Human-in-loop | Multi-agent | Browser/Playwright | Code-first feel |
|---|---|---|---|---|---|---|---|
| **LangGraph** | ✅ via any LLM provider | ✅ | ✅ Best-in-class — Postgres checkpointer, time travel, durable execution ([LangChain](https://docs.langchain.com/oss/python/langgraph/durable-execution)) | ✅ `interrupt()` primitive | ✅ via subgraphs | ✅ via tools | Steep but explicit |
| **CrewAI** | ✅ | ✅ | ⚠️ "Limited checkpointing" reported in 2026 reviews ([Gurusup](https://gurusup.com/blog/best-multi-agent-frameworks-2026)) | Basic | ✅ "Crew" abstraction | via tools | High-level DSL |
| **AutoGen / AG2** | ✅ | ✅ | ⚠️ Conversation-history-as-state; Microsoft has shifted focus to broader Microsoft Agent Framework ([OpenAgents](https://openagents.org/blog/posts/2026-02-23-open-source-ai-agent-frameworks-compared)) | ✅ | ✅ GroupChat | via tools | Conversational |
| **OpenAI Agents SDK** | Partial (OpenAI-only deepest) | Partial | ⚠️ Context vars, ephemeral | ✅ | ✅ handoffs | via tools | Clean |
| **Pydantic AI** | ✅ Excellent | ✅ Excellent | ✅ Native durable execution via Temporal/DBOS/Prefect integrations ([Temporal](https://temporal.io/blog/build-durable-ai-agents-pydantic-ai-and-temporal); [Prefect](https://www.prefect.io/blog/prefect-pydantic-integration); [DBOS](https://pydantic.dev/articles/pydantic-ai-dbos)) | ✅ tool approval | ✅ Capabilities | via tools | Type-safe, FastAPI-feel |
| **smolagents (HF)** | ✅ | ✅ | ❌ Minimal | Minimal | Limited | via tools | Very small |
| **Mastra (TS)** | ✅ | ✅ | Workflows w/ persistence | ✅ | ✅ | via tools | TypeScript-first |
| **Custom (Claude Code-style harness)** | ✅ | ✅ | DIY | DIY | DIY | ✅ direct | Maximum |

### 2.2 Recommendation: **Pydantic AI + a thin custom orchestrator**, with **LangGraph as the fallback**

Reasoning:

1. **Type safety is enormously valuable for a judge agent that returns structured verdicts.** Pydantic AI gives you typed `output_type=JudgeVerdict` with automatic validation and reflection retries — that's what you want when 50 different agents emit different structured outputs ([Pydantic AI agents](https://ai.pydantic.dev/agent/)).
2. **Native durable execution via Temporal/DBOS/Prefect.** The `TemporalAgent(agent)` wrapper turns any Pydantic AI agent into a fault-tolerant durable workflow with automatic checkpointing of model calls and tool calls ([Temporal](https://temporal.io/blog/build-durable-ai-agents-pydantic-ai-and-temporal)). You don't pay for Temporal if you use DBOS, which is just a Postgres-backed library — perfect for a single Mac.
3. **First-class human-in-the-loop tool approval** maps directly onto your judge agent requirement.
4. **Model-agnostic** — supports OpenAI, Anthropic, OpenAI-compatible (LM Studio), Ollama, and many more.
5. **It's code-first.** You'll feel at home coming from Claude Code; the API is FastAPI-like, decorators around plain Python functions.

**When to fall back to LangGraph:** if you discover you need fine-grained graph-level "time travel" — i.e., literally rewinding and forking the whole investigation from a prior super-step — LangGraph's checkpointer with `PostgresSaver` is more battle-tested and offers `graph.get_state_history()` that lets you inspect any prior checkpoint ([LangChain checkpoints](https://reference.langchain.com/python/langgraph/checkpoints)). Most real overnight investigations don't need this — they need durable retry, which Pydantic AI + DBOS gives you.

**Avoid CrewAI** for this use case despite its prototyping speed — its checkpointing limitations and coarser error handling are a poor fit for an unattended overnight run. **Avoid AutoGen** — Microsoft has effectively put it in maintenance mode, and the GroupChat pattern adds 5–6× token cost vs. LangGraph for similar work ([Lushbinary 2026 benchmark](https://lushbinary.com/blog/langgraph-vs-crewai-vs-autogen-ai-agent-framework-comparison/); [OpenAgents](https://openagents.org/blog/posts/2026-02-23-open-source-ai-agent-frameworks-compared)).

**Don't go fully custom unless you must.** The "Claude Code-style" harness pattern (planner + tools + sandbox) is appealing because you've already built things that way, but the framework choice is mostly about *durability and observability infrastructure*, which is the part you don't want to write twice. Let Pydantic AI carry that weight.

---

## 3. Agent Roster and Configuration

### 3.1 Roles, model tiers, and invocation triggers

| Agent | Tier | Model | Invoked when | Tools |
|---|---|---|---|---|
| **Planner** | Frontier (rare) + General (frequent) | `claude-opus-4-7` for big rewrites; Qwen3 32B for tactical re-plans | Start of investigation; every 30–45 min; on signal events | Read state, write plan tasks |
| **Researcher / Worker** | General | Qwen3 32B (or Llama 3.3 70B Q6 if memory allows) | Per task | All API tools, RAG search, doc analyzer |
| **Browser Operator** | General | Qwen3 32B with vision (or Qwen3-VL) | Sites lacking APIs (FOIA portals, FPPC complaint portal, county court systems) | Playwright CLI |
| **Document Analyzer** | General + Vision | Qwen3-VL 8B for screenshots, Qwen3 32B for text | PDF/image ingestion | OCR, chunking, hierarchical summarization |
| **Entity Extractor** | Fast | Qwen3 4B or 8B | Every new document | NER, alias resolution |
| **Link/Connection Finder** | Reasoner | DeepSeek-R1-distill 32B or Qwen3-Next-80B-A3B | After entity extraction, on synthesis tick | Graph queries, embedding similarity |
| **Synthesizer** | Frontier | `claude-opus-4-7` | Cadence ticks; final report | Read-only access to entire state |
| **Judge / Validator** | Frontier (small calls) | `claude-opus-4-7` with adaptive thinking | Every outbound action, every "publish" | Read-only state access; no tools |
| **Communications Drafter** | General | Qwen3 32B | When the planner decides to send something | Email/voice templates, PII redaction |
| **FOIA Specialist** | General + Frontier | Qwen3 32B drafts; Opus reviews | Drafting agency-specific FOIA requests | FOIA template lib, agency lookup |
| **Verifier (pre-publication)** | Frontier | `claude-opus-4-7` | Before any finding is marked "published" | Read sources, check for hallucinations |

### 3.2 Local model recommendations for 128GB M5 Ultra

Important context: as of April 25, 2026, the **M5 Ultra Mac Studio has not formally launched**; Apple has shipped M5 Pro/Max MacBook Pros but the Mac Studio M5 line is widely expected at WWDC 2026 with up to 256GB unified memory and ~1100 GB/s bandwidth, with a possible slip to October due to RAM shortages ([Macworld](https://www.macworld.com/article/2973459/2026-mac-studio-m5-release-date-specs-price-rumors.html); [Felloai](https://felloai.com/m5-ultra-mac-studio/)). On a confirmed M4/M5 Max-class with 128GB you have ~92GB usable for model weights (the 70–75% rule, accounting for OS + KV cache + runtime — [Will It Run AI](https://willitrunai.com/blog/best-llm-for-mac-apple-silicon-2026)).

**Concrete picks (all via LM Studio's MLX backend where available — MLX delivers ~20–87% higher generation throughput than llama.cpp for sub-14B models on Apple Silicon, with llama.cpp catching up on 27B+ where bandwidth is the bottleneck — [Groundy](https://groundy.com/articles/mlx-vs-llamacpp-on-apple-silicon-which-runtime-to-use-for-local-llm-inference/), [Starmorph](https://blog.starmorph.com/blog/apple-silicon-llm-inference-optimization-guide)):**

- **General reasoning workhorse**: **Llama 3.3 70B Q6** (~55GB) — leaves headroom for context. ~22 tok/s on M5 Max class hardware ([Felloai M5 Ultra projections](https://felloai.com/m5-ultra-mac-studio/)). Alternative: **Qwen3 32B Q8** (~32GB) at 15–22 tok/s for higher quality per parameter.
- **High-throughput entity extraction / classification**: **Qwen3 4B** at Q4_K_M (~3GB) — runs at 100+ tok/s, exceptional for the cost. Use it for everything cheap.
- **Reasoning / deductive logic**: **DeepSeek-R1-Distill 32B** at Q6 or **Qwen3-Next-80B-A3B** (Mixture-of-Experts, ~22B active per token, frontier-class on a 128GB box at 5–10 tok/s — [Insiderllm](https://insiderllm.com/guides/best-local-llms-mac-2026/)).
- **Vision / PDF / screenshot analysis**: **Qwen3-VL 8B** (or 2B for fast cases) — Qwen3-VL-2B has been benchmarked beating closed-source APIs on cross-modal retrieval ([Milvus benchmark 2026](https://milvus.io/blog/choose-embedding-model-rag-2026.md)).
- **Embeddings**: **Qwen3-Embedding-4B** (best-in-class open multilingual, instruction-aware) or **BGE-M3** for hybrid dense+sparse retrieval ([ZenML](https://www.zenml.io/blog/best-embedding-models-for-rag); [Milvus](https://milvus.io/blog/choose-embedding-model-rag-2026.md)). Expose them via LM Studio's `/v1/embeddings` endpoint.
- **Tiny classifier / safety screen**: **Qwen3 0.6B** at Q4 — for the judge's first-pass spam/relevance triage.

LM Studio's idle TTL + auto-evict means you can keep all of these "loaded" in your config and let the daemon swap models in and out of GPU memory between calls — critical because you can't fit all of them simultaneously even at 128GB.

### 3.3 Coordination

- **Shared state via Postgres** (no in-memory message bus). Workers read tasks, write findings; the planner reads everything.
- **Handoffs as task spawning**, not async messages. When the entity extractor finds a new LLC, it spawns a `unmask_llc` task; it doesn't directly call another agent. This gives you full observability and replay.
- **Message-passing only inside the synthesis tick**: Opus reads state, emits a structured plan, the orchestrator translates that plan into queue inserts.

### 3.4 The Judge Agent (deeper coverage in Section 7)

Single instance, runs on Opus 4.7 with adaptive thinking. Stateless — pulls the action plus minimal context (investigation goal, recent findings cluster, target's prior contacts) and returns a structured verdict.

---

## 4. Investigative Techniques (the "what would a relentless investigator do" question)

This is the section where you give the agents *playbooks*, not just tools. Each technique below is implementable as a tool plus a prompt template.

### 4.1 Following the money

- **Campaign finance core**: pull every contribution from candidate X, every contribution to committee Y, every disbursement from committee Y, and every contribution from a donor Z across cycles. Use the FEC API for federal and Cal-Access/Power Search for California state ([FEC.gov](https://www.fec.gov/data/browse-data/); [Power Search](https://powersearch.sos.ca.gov/)). Build a normalized contributor table — donors are messy strings, the same person appears as "John Smith / John A Smith / Smith, John A. III" across filings; entity resolution on (name, employer, occupation, zip) catches most.
- **Dark money**: 501(c)(4)s and 501(c)(6)s don't disclose donors. The chain you can build is *recipient PAC ← (c)(4) transfer ← (c)(4)'s 990 expenses → known vendors / consultants*. Use ProPublica Nonprofit Explorer's API for 990s and link via EIN and officer overlap ([ProPublica Nonprofit Explorer](https://projects.propublica.org/nonprofits/api)).
- **LLC unmasking**: an LLC that donates is interesting *because* it's nominally anonymous. Cross-reference the LLC name against state Secretary-of-State business databases (CA SOS, DE, NV, WY for the usual suspects) for registered agent + organizers + officers. Same registered agent across many LLCs at the same address is a red flag. **OpenCorporates** is the global aggregator — free for public-benefit projects via their Service Desk application; commercial pricing starts at £2,250/yr ([Bellingcat OpenCorporates guide](https://www.bellingcat.com/resources/2023/08/24/following-the-money-a-beginners-guide-to-using-the-opencorporates-api/); [Zephira pricing breakdown](https://zephira.ai/opencorporates-pricing-explained-2026-plans-api-limits-licensing-and-what-it-means-in-production/)).
- **Beneficial ownership** (the actual humans): per Bellingcat and OCCRP playbooks, the chain is `LLC → directors/agents → search those people across other entities → look for shared addresses, phone numbers, email reuse → cross-reference with leaked corpora` (Panama/Paradise/Pandora Papers via ICIJ Offshore Leaks DB / Aleph) ([GIJN Karrie Kehoe tips](https://gijn.org/stories/tracking-shell-companies-secret-owners/); [Spotlight EBU 6-step UBO method](https://spotlight.ebu.ch/p/tracing-beneficial-ownership-with)).
- **Shell-pattern signals**: same registered agent used by many LLCs; mailing address that's a CMRA / virtual office; LLC formed shortly before a large donation; LLC dissolved shortly after. All flaggable in code.

### 4.2 Network analysis

- Build the entity graph as you ingest. Edges have types (`donated_to`, `serves_on_board_of`, `married_to`, `lobbies_for`, `share_address_with`, `cited_in_complaint_against`).
- Compute centrality metrics nightly: betweenness centrality on the donor-PAC-candidate subgraph reveals "broker" donors who connect otherwise-disconnected clusters.
- Family-network expansion: for each person of interest, the agent should attempt to enumerate spouses (FEC contributions often include spousal donations on the same date), siblings (financial disclosure forms include some family), and adult children (state business registries sometimes show shared addresses). Cross-check with social-media reachability (image search of society-page photos, per Karrie Kehoe).
- **Co-occurrence scoring**: count how often two entities appear in the same documents, board memberships, or events; sort descending. Surprisingly powerful for surfacing relationships nobody has written about.

### 4.3 Document techniques

- **Cross-referencing**: every document carries (date, author/agency, claim). For each claim, search for *contradicting* claims by the same actor at a different date. A vote that contradicts a public statement, a financial disclosure that contradicts a contract — those are the headlines.
- **Court filing → FEC filing → lobbying disclosure** triangulation: the same name appearing in PACER as plaintiff/defendant in regulatory disputes, then on Senate LDA as a lobbyist, then on FEC as a donor to the relevant committee chairman is a pattern worth surfacing.
- **Hierarchical summarization** is mandatory for long docs — chunk to 4–8K tokens, summarize each, summarize the summaries, and only feed the top-level summary to Opus with the option to drill down on demand.

### 4.4 OSINT techniques

- **Wayback / archive.today** every web finding at ingest time. Store the archived URL alongside the live URL. Sites change; sources disappear; capture-now-or-lose-later.
- **DomainTools / SecurityTrails** WHOIS history to link sites to operators (most useful for super-PAC-adjacent shell websites).
- **GDELT 2.0 DOC API** for global news monitoring with no key required — query by keyword, source country, language; rate-limited but generous; updates every 15 min ([GDELT DOC 2.0](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)).
- **Common Crawl** for historical web snapshots at scale (overkill for most overnight runs; useful for big-picture studies).
- **Regulatory comment letters** at Regulations.gov reveal who is lobbying on what — comments by industry association staff are a corporate-influence signal; their API rate limit is 50 req/min, 500 req/hour with a key ([Regulations.gov API](https://open.gsa.gov/api/regulationsgov/)).

### 4.5 Timing analysis

- **Donation-vote correlation**: for every member-vote pair, compute the donation flow from interested parties in the 90 days before and 30 days after. Outliers (large donations immediately preceding a key vote) flag.
- **Contract-disclosure correlation**: for each federal contract awarded to company X, check whether the awarding official disclosed any X-related stock holdings or honoraria.
- Time-series anomaly detection on lobbying expenditures by topic — sudden spending spikes precede legislation.

### 4.6 FOIA strategy

Effective FOIA requests share a few traits, derived from FOIA.gov, DOJ template regulations, and the Reporters Committee:

- **Tight scope**: 90-day window, named offices, 5–10 keywords. Broad "any and all" language inflates costs and time ([RequestLetters](https://requestletters.com/home/5-foia-request-letter-templates-free-samples-writing-tips); [FOIA.gov](https://www.foia.gov/faq.html)).
- **Fee waiver justification**: explicit statement that disclosure is in the public interest, will contribute significantly to public understanding, and not primarily commercial. Cite the specific agency regulation. Templates from FOIA Basics ([FOIABasics](https://www.foiabasics.org/writing-your-request)).
- **Expedited processing**: only granted for "compelling need" — life/safety threat or "urgency to inform the public" by someone "primarily engaged in disseminating information." Cite news articles to establish urgency. Agency must respond within 10 calendar days on this question ([Archives.gov journalist guide](https://www.archives.gov/ogis/resources/foia-for-journalists)).
- **Identifying the right agency**: FOIA.gov has a master directory; for cross-agency topics, file in parallel.
- **Appeals**: must be filed within agency-specified window (usually 90 days). Appeal denials of fee waiver, scope, or expedited processing — the appeal is itself fast.
- **Tracking**: assign one named contact per request, store agency tracking numbers, set per-request SLA timers, and auto-escalate when statutory deadlines pass.

### 4.7 Court records mining

- **PACER** for federal: $0.10/page, capped at $3/document, but the **CourtListener / RECAP** project has cached half a billion documents for free ([CourtListener APIs](https://www.courtlistener.com/help/api/rest/)). Authenticated APIs allow 5,000 queries per hour ([CourtListener REST](https://www.courtlistener.com/help/api/rest/)). Use RECAP first; fall back to PACER fetch via CourtListener for missing items.
- **State courts**: highly variable. California Superior courts are mostly per-county portals (LA County, SF County, SD County have different systems, all without APIs); use Playwright to scrape. CA Supreme Court / Court of Appeal admin records via PAJAR ([CA Courts public records](https://courts.ca.gov/policy-administration/public-records?rdeLocaleAttr=en)).
- **Family court patterns** (your second focus): family case files are mostly sealed under California rules, but case caption + docket events are usually accessible; patterns of repeat appearances of the same minor's counsel, the same 730 evaluator, the same court-appointed receiver across many cases is exactly the kind of thing only a patient agent finds.

### 4.8 Whistleblower-style pattern matching

- Subscribe to GAO reports, Inspector General reports across agencies via Oversight.gov, federal IG hotlines' public summaries, state auditor reports, and CA State Auditor reports ([Oversight.gov](https://www.oversight.gov/); [California State Auditor](https://www.auditor.ca.gov/reports/)).
- Cross-reference *names of officials criticized* in IG reports against subsequent contracts, FEC donations to their successors, lobbying registrations, and revolving-door appointments.

### 4.9 Pre-publication verification

Before any synthesis output is committed to a "report," run a **Verifier pass** with Opus that:

1. Re-queries each source URL/document and confirms the cited claim is actually in the source.
2. Flags any claim with only one source.
3. Flags any quote not exactly matching source text.
4. Flags any LLM-generated bridge sentences that aren't traceable to documents.

This is the single highest-leverage anti-hallucination control in the system.

---

## 5. Prioritized Data Sources by Focus Area

### 5A. Federal political corruption & elections

| Source | Access | Auth | Rate / Notes |
|---|---|---|---|
| **OpenFEC API** (`api.open.fec.gov`) | REST | api.data.gov key | 1,000 req/hr with key, 40 req/hr anonymous ([Apify FEC notes](https://apify.com/pink_comic/fec-campaign-finance-search)). Returns committees, candidates, contributions, IEs, disbursements. Cache-Control: 1 hour. The authoritative source. |
| **FEC bulk data files** (FTP) | CSV/ZIP downloads | none | Use for full-cycle cross-reference; updated daily-to-weekly ([FEC bulk](https://www.fec.gov/data/browse-data/)). |
| **OpenSecrets API** | REST | API key | **Discontinued April 15, 2025** ([OpenSecrets API page](https://www.opensecrets.org/api)). Use **OpenSecrets bulk data** (CC BY-NC-SA, educational use only) instead. |
| **Congress.gov API** (v3) | REST | api.data.gov key | 5,000 req/hr; 250 max page size; covers bills, members, committees, hearings, Congressional Record ([Congress.gov API](https://api.congress.gov/); [LOC docs](https://www.loc.gov/apis/additional-apis/congress-dot-gov-api/)). |
| **ProPublica Congress API** | REST | API key | **Discontinued; new keys not available** ([ProPublica](https://projects.propublica.org/api-docs/congress-api/)). Migrate to Congress.gov v3. |
| **Senate LDA API** (`lda.senate.gov` → migrating to `lda.gov` by 6/30/2026) | REST | API key (anonymous works but stricter throttling) | Lobbying registrations LD-1, quarterly LD-2, contributions LD-203 ([Senate LDA](https://lda.senate.gov/api/); [LDA TOS](https://lda.senate.gov/api/tos/)). |
| **House Lobbying Disclosure** | site search + bulk | none | Less developer-friendly than Senate LDA; use Senate LDA which contains both chambers' filings. |
| **USAspending.gov API** | REST | none | No key, generous limits; contracts/grants/loans/financial assistance with award-modifications gotcha (file by base award) ([USAspending API](https://api.usaspending.gov/); [Grantsights guide](https://www.grantsights.com/blog/how-to-read-usaspending-data)). |
| **SAM.gov** | REST | API key (account required) | Public 10 req/day, registered entity 1,000/day, federal 10,000/day ([SAM.gov rate limits](https://govconapi.com/sam-gov-rate-limits-reality)). Entity Management API and Opportunity Management API both v3. Major exclusions database. |
| **SEC EDGAR** | REST + bulk + EFTS full-text | User-Agent header required | Hard 10 req/sec cap site-wide; over → IP block for ~10 min ([SEC.gov rate limits](https://www.sec.gov/filergroup/announcements-old/new-rate-control-limits)). Use the `sec-edgar-api` Python wrapper which auto-throttles ([sec-edgar-api](https://sec-edgar-api.readthedocs.io/)). |
| **DOJ press releases** | RSS / scrape | none | No API; scrape press release index, parse for new actions. |
| **FARA database** | site search + bulk | none | `efile.fara.gov` has filings; OpenSecrets Foreign Lobby Watch is more searchable ([FARA eFile](https://efile.fara.gov/ords/fara/f?p=1235:10); [OpenSecrets FARA](https://www.opensecrets.org/fara/search)). MFA enrollment required for filers as of 2/6/2026 ([FARA MFA notice](https://www.justice.gov/nsd-fara)). |
| **IRS 990 / ProPublica Nonprofit Explorer API** | REST | none documented | Search organizations and pull full Form 990 data and PDFs by EIN ([Nonprofit Explorer API](https://projects.propublica.org/nonprofits/api)). Treat as the primary 501(c) data source. |
| **State business entity DBs** | per-state portals | varies | Critical for LLC unmasking. Most are scrape-only. Build per-state Playwright collectors for CA, DE, NV, WY, FL, NY at minimum. |
| **OpenCorporates API** | REST | API token required | Free for approved public-benefit projects via service desk; commercial £2,250/yr (Essentials) → £12,000/yr (Basic) at ~£0.20/call ([Zephira pricing](https://zephira.ai/opencorporates-pricing-explained-2026-plans-api-limits-licensing-and-what-it-means-in-production/); [OC API ref](https://api.opencorporates.com/documentation/API-Reference)). 100 results/page max; track quota via `/account_status`. |
| **PACER / CourtListener / RECAP** | REST | CourtListener token | 5,000 queries/hr authenticated; RECAP Archive holds ~500M PACER objects free; Fetch API requires PACER credentials and is subject to PACER's 180-day password rotation ([CourtListener APIs](https://www.courtlistener.com/help/api/); [RECAP APIs](https://www.courtlistener.com/help/api/rest/recap/)). |
| **FOIA.gov submission portals** | per-agency web | varies | No unified API; each agency has its own portal. Build a small per-agency adapter library; many accept email to a foia@ address. |
| **Federal Register API** | REST, CSV/JSON | **no key required** | Returns Federal Register documents from 1994 onward and Public Inspection desk; pagination capped at 2,000 results so use date range filters ([Federal Register dev docs](https://www.federalregister.gov/developers/documentation/api/v1)). |
| **Regulations.gov v4 API** | REST | api.data.gov key | 50 req/min, 500 req/hr default; demo key has tighter limits ([Regulations.gov API](https://open.gsa.gov/api/regulationsgov/)). |
| **GAO reports** | RSS + site | none | Scrape monthly index; use as IG-style oversight signal source. |
| **Inspector General reports** | Oversight.gov + per-agency | none | Aggregator for 70+ federal IGs ([Oversight.gov](https://www.oversight.gov/)). |
| **Whistleblower complaint summaries** | per-agency IG | varies | Most agency IGs publish anonymized summaries; OSC handles PPP. |
| **State campaign finance portals** | varies | varies | NY, IL, FL all critical; Power Search (CA) covered below. |

### 5B. California family law & state corruption

| Source | Access | Notes |
|---|---|---|
| **California Superior Court systems** | per-county web portals | No unified API; LASC, SF, SD, OC, Alameda each have different systems. **Family law case files are largely confidential under CRC 2.400+ rules**, but docket events, case captions, party names, attorneys of record, and assigned judges are typically public. Build per-county Playwright collectors; treat state of access as fragile. |
| **California Public Records Act (CPRA) requests** | per-agency email/portal | Modeled on FOIA. Recommend submitting via email to each agency's designated PRA address; agencies have 10 calendar days for initial response (with possible 14-day extension) ([SCO Guidelines](https://www.sco.ca.gov/eo_about_records.html)). Build per-agency contact registry. |
| **California Secretary of State business search** | site (`bizfileonline.sos.ca.gov`) | No API; scrape via Playwright. Returns LLC/Corp basics, status, agent for service, statements of information. Critical for state-level LLC unmasking. |
| **Cal-Access / Power Search** (`powersearch.sos.ca.gov`) | site search | Covers electronically reported state-level campaign contributions and independent expenditures from 2001 onward, refreshed daily; **MapLight open-source code** powers it ([SOS Power Search](https://powersearch.sos.ca.gov/); [MapLight launch](https://www.maplight.org/post/press-release-power-search-tool-for-cal-access-launched-today)). The underlying CAL-ACCESS data dumps are the API substitute — a daily "DATA" download is the right backend. |
| **CARS (Cal-Access Replacement System)** | TBD | Modernization underway; track as it ships. |
| **California State Auditor reports** | site | Reports back to 1993 ([CA State Auditor](https://www.auditor.ca.gov/reports/)). Subscribe to RSS; ingest reports into doc store on publication. |
| **Bureau of State Audits** | rolled into State Auditor | Same source. |
| **Judicial Council of California reports** | `courts.ca.gov` + PAJAR | Administrative records via Public Access to Judicial Administrative Records (rule 10.500). 10-day response; small fees ([Judicial Branch public records](https://courts.ca.gov/policy-administration/public-records?rdeLocaleAttr=en)). |
| **Commission on Judicial Performance (CJP)** | site (`cjp.ca.gov`) | Public discipline database from 1961 onward; pending cases publicly noticed; complaints are confidential until formal charges filed ([CJP](https://cjp.ca.gov/)). Scrape the public discipline page for new actions. CPRA email: `CPRA@fppc.ca.gov` is FPPC, separate. |
| **California State Bar disciplinary records** | `apps.calbar.ca.gov` | Attorney profile shows full discipline history; State Bar Court dockets searchable; certified copies $26 per case via mail/email ([State Bar Court records](https://www.statebarcourt.ca.gov/public-records-information)). Daily new disciplinary actions list ([Recent Disciplinary Actions](https://www.calbar.ca.gov/public/concerns-about-attorney/recent-disciplinary-actions)). |
| **County court systems** | per-county | LA County (LASC online), SF Superior, SD Superior — Playwright. |
| **CA legislative info** (`leginfo.ca.gov`) | site + bulk XML | Bills, statuses, votes, committee analyses; download bulk XML. |
| **California FPPC** | site + Complaint Portal | The Complaint and Case Information Portal lists complaints + cases by jurisdiction, respondent, complainant; portal supports filtering by violation type and disposition ([FPPC Complaint Portal](https://www.fppc.ca.gov/enforcement/complaint-and-case-information-portal/)). Form 700 and 800-series searches are also exposed ([FPPC Search Filings](https://www.fppc.ca.gov/transparency.html)). For records not on site: CPRA request to `CPRA@fppc.ca.gov`. |
| **Local agency accountability portals** | varies | LA Ethics Commission, SF Ethics Commission, San Diego, etc. — each has a local campaign finance portal; build per-jurisdiction adapters. |

### 5C. General OSINT / corporate accountability

| Source | Access | Notes |
|---|---|---|
| **Wayback Machine** | REST + browser extension | Save Page Now API for capturing on-demand. Critical for evidence preservation. |
| **archive.today / archive.ph** | site + URL pattern | Use when Wayback is blocked or fails (paywalled news, JS-heavy sites). |
| **DomainTools / SecurityTrails** | REST | Paid; WHOIS history, passive DNS, reverse-IP. Budget item. |
| **Social media archives** | varies | X firehose effectively closed; LibX/Bsky/Threads have partial APIs. Build content archivers via Playwright for snapshot evidence. |
| **GDELT 2.0 DOC API** | REST | No key; updates every 15 min; supports sentiment, language, country filters ([GDELT 2.0](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)). Alex Smith's `gdelt-doc-api` Python client is good ([GitHub](https://github.com/alex9smith/gdelt-doc-api)). |
| **Common Crawl** | S3 / index | For deep-history web mining at scale. |
| **News APIs** | varies | Newscatcher, NewsAPI.ai, Aylien. Most have free tiers but rate-capped. |
| **CourtListener / RECAP** | REST | See above; covers federal + many state courts. |
| **OpenCorporates** | REST | Global corporate structure; see pricing above. |
| **PACER Fetch (via CourtListener)** | REST | $0.10/page passed through; use sparingly. |
| **Justia, Casetext / CoCounsel** | site + scraper | Free case-law mirrors; useful for legal background context. |
| **ICIJ Offshore Leaks DB / OCCRP Aleph** | site + Aleph API | Panama/Paradise/Pandora Papers + tens of millions of public records; Aleph has an API for verified investigators ([Spotlight EBU UBO guide](https://spotlight.ebu.ch/p/tracing-beneficial-ownership-with)). |

---

## 6. Outbound Action Systems

### 6.1 Email (FOIA, follow-ups, agency correspondence)

- **ESP**: **Postmark** for transactional. The reason is unambiguous: agency mail systems are notoriously aggressive on filtering, and Postmark specializes in transactional deliverability with separate marketing/transactional sending infrastructure and consistent inbox placement metrics ([Postmark](https://postmarkapp.com/); [Sender review](https://www.sender.net/compare/postmark-vs-sendgrid/)). SendGrid works but its dual marketing+transactional architecture means a noisy IP can drag down delivery, and price competitiveness reverses at scale.
- **Setup**: Authenticate the sending domain with SPF, DKIM (Postmark provides), and DMARC `p=quarantine` minimum. Use a dedicated subdomain (`foia@inv.yourdomain.com`) so a personal domain reputation isn't tied to bulk agency mail.
- **Templates** (drafted by Comms agent, judge-validated, sent only after approval):
  - Initial FOIA request (with fee waiver + expedited processing language; see §4.6)
  - Acknowledgment chase (Day 11 and Day 21 — federal agencies have 20 working days)
  - Appeal letter (denial, partial denial, fee waiver denial, expedited denial — within 90 days)
  - Status check
- **Inbound parsing**: Postmark's Inbound webhook gives you parsed JSON of incoming mail; route to the correspondence agent which classifies (acknowledgment, response, denial, fee estimate, request for clarification) and updates the FOIA tracking table.
- **Deliverability hygiene**: keep volume per-domain modest (a single agency receiving 50 different requests in one week from your domain looks like attack traffic); spread sends; respect any opt-out language in agency auto-replies.

### 6.2 Voice (Twilio + ElevenLabs)

The integration pattern is well-established. Two options:

- **Native ElevenLabs ↔ Twilio integration** (simpler): import your Twilio number into ElevenLabs, link to a configured agent, assign incoming/outgoing calls; ElevenLabs auto-configures the webhooks. Use this for inbound + simple outbound ([ElevenLabs native](https://elevenlabs.io/docs/eleven-agents/phone-numbers/twilio-integration/native-integration)).
- **Register-call / your-own-WebSocket** (more control, needed for dynamic content): Twilio's `\u003cConversationRelay\u003e` TwiML noun now supports ElevenLabs as a TTS provider directly with voice/model/speed/stability customization ([Twilio + ElevenLabs ConversationRelay tutorial](https://www.twilio.com/en-us/blog/integrate-elevenlabs-voices-with-twilios-conversationrelay)). This pattern lets you stream from your local LLM (or Opus) into ElevenLabs Flash 2.5 / Turbo 2.5 for sub-second latency. Use the Flash model for low-latency conversations.
- **Voicemail handling**: Twilio's recording feature plus its transcription (or ElevenLabs's STT, or Whisper running locally) feeds the voicemail back to the correspondence agent.
- **Critical legal constraints (much more in §8)**: an AI-generated voice call is, under TCPA + the FCC's February 2024 Declaratory Ruling, an "artificial or prerecorded voice" robocall and **requires prior express consent** of the called party for non-emergency calls to mobile numbers. Statutory damages run $500–$1,500 per illegal call ([FCC ruling](https://www.fcc.gov/document/fcc-confirms-tcpa-applies-ai-technologies-generate-human-voices); [Henson Legal 2026](https://www.henson-legal.com/ai-voice-compliance)). **Practical implication: outbound voice calls in this system should generally be limited to (a) calls to public-facing official lines that publicly invite contact, (b) calls where you have documented prior express consent, or (c) live-agent-on-the-line patterns rather than fully autonomous AI dial. Build the system assuming the judge will reject 95% of proposed AI outbound calls.**
- **Recording**: California is a two-party consent state (Cal. Penal Code § 632); confidential communications cannot be recorded without all-party consent. Statutory penalties up to $2,500/violation, $10,000 for repeat offenses; recordings are inadmissible in CA civil court ([Cal. Penal Code § 632](https://codes.findlaw.com/ca/penal-code/pen-sect-632/); [RCFP California](https://www.rcfp.org/reporters-recording-guide/california/)). Build a per-jurisdiction consent matrix and have the system play a "this call may be recorded" disclosure on every recorded call regardless of jurisdiction.

### 6.3 Form filing (FOIA portals, complaint forms)

- **Playwright CLI** (`@playwright/cli`) released in early 2026 by Microsoft is the right tool for agent-driven form filling. It saves snapshots to disk (YAML accessibility trees) instead of streaming them into the model context, yielding ~4× lower token consumption than Playwright MCP for the same workflow ([Test-Lab](https://www.test-lab.ai/blog/playwright-mcp-vs-cli-agentic-testing); [TestDino](https://testdino.com/blog/playwright-cli/); [Playwright CLI docs](https://playwright.dev/docs/getting-started-cli)).
- **Pattern**: launch a persistent browser (`playwright-cli open https://foia-portal/...`), navigate, take a snapshot, the LLM picks the right element refs (`e15`, `e22`), `fill`/`click` them, submit, screenshot the confirmation, archive the receipt URL.
- **Per-portal recipe library**: for each FOIA portal, complaint form, etc., maintain a stored recipe (a sequence of CLI commands plus selector hints). When the recipe still works the agent uses it without LLM involvement — saves tokens and is more reliable. When the recipe fails (selector drift), the LLM regenerates it.

### 6.4 FOIA template library

Maintain templates per agency. At minimum:

- **Generic federal request** with: scope (date range, named offices, keywords), fee category (news media, scholarly research, or "other" — set a $250 cap), fee waiver request with full 6-factor public-interest justification, expedited processing request with cited compelling need, electronic delivery preference, contact info ([RequestLetters](https://requestletters.com/home/5-foia-request-letter-templates-free-samples-writing-tips); [DOJ template regulations](https://www.justice.gov/oip/template-agency-foia-regulations)).
- **Agency-specific overlays**: DHS, DOJ, IRS, ICE, DEA, FBI each have nuanced fee waiver standards in their CFR; tailor language to the cited regulation.
- **CPRA template** (California) with the Cal. Government Code citation and 10-day response expectation.
- **Appeal templates**: for fee waiver denial, expedited denial, full/partial records denial, format denial.
- **Track everything**: agency, tracking number, submitted date, ack date, statutory deadline, current status, any fees in dispute.

---

## 7. Judge Agent Deep Dive

The judge is the difference between a system that's a research tool and a system that's a liability. Spec it carefully.

### 7.1 Architecture

- One stateless judge process, on Opus 4.7 with adaptive thinking.
- Inputs (single structured prompt):
  1. The proposed action (kind, target, body, attachments, sender identity).
  2. The investigation goal and the specific finding(s) cited as the rationale.
  3. The recipient's recent contact history with this investigation (last 30 days of outbound to the same target).
  4. The system's hard limits (TCPA, defamation, jurisdictional recording laws, FOIA scope).
  5. The judge rubric (below).
- Output: a structured `JudgeVerdict` (Pydantic model):
  ```python
  class JudgeVerdict(BaseModel):
      decision: Literal["approve", "reject", "needs_human"]
      score: float  # 0-100, composite legitimacy score
      criteria_scores: dict[str, int]  # each criterion 0-10
      blocking_issues: list[str]
      improvement_suggestions: list[str]
      rationale: str  # plain-English explanation
      recommended_changes: str | None  # rewritten body if minor fixable issues
  ```
- The judge has read-only tools (it can pull a full document a worker cited, query the entity graph, check the FOIA tracking table for prior contact). It cannot send anything itself.

### 7.2 Rubric

Score each on 0–10; minimum 60 composite to approve, below 40 reject, in between escalate to human.

1. **Relevance to research goal** (does the action serve the stated investigation? not just a fishing expedition)
2. **Correct recipient agency / person** (right FOIA office, right phone number, right legal jurisdiction)
3. **Factual accuracy of all claims in the body** (every assertion must trace to a finding with evidence)
4. **Tone — professional, neutral, non-accusatory** (no "I demand," no editorializing about wrongdoing not yet established)
5. **Non-spam / non-harassment patterns** (not the 5th identical FOIA to the same office in 30 days; not 6 calls to a private cell number)
6. **Legal compliance**:
   - No impersonation (claim of identity must match real identity; explicit "this is an AI agent acting on behalf of [user]" disclosure for voice calls per FCC NPRM direction)
   - No fraud (no false statements, no false threats, no false claims of authority)
   - TCPA: prior express consent for AI voice to mobile lines
   - Recording laws: jurisdiction consent matrix
   - No publishing of unverified defamatory claims
   - FOIA scope is reasonable (no obvious abusive/cost-inflation pattern)
7. **Privacy / minimization** (don't include third-party PII unless necessary)
8. **Cost / proportionality** (the action's cost — agency staff time, money — is proportionate to the public-interest value)

### 7.3 Decision rules (hard, not learned)

- **Hard reject** if any blocking issue is present:
  - Recipient is a private individual (non-public-figure) and the action is unsolicited
  - Recipient is a private cell number and the action is an AI voice call without documented consent
  - Body contains a factual claim with no supporting document
  - Body contains a quoted phrase that does not appear verbatim in any source
  - Action would be the 4th+ outbound to the same recipient in 7 days
  - Target jurisdiction is a state with all-party consent and the call would be recorded
  - Action implies legal authority the user does not have (cease-and-desist, subpoena, etc.)
  - Action contains an unverified accusation of crime
- **Soft reject (improve and resubmit)** if criteria scores include any below 5 but no hard issues.
- **Needs human** if the judge's confidence is low (it explicitly outputs `confidence: low`), the recipient is an elected official's personal contact, or the action is novel relative to prior approved actions for this investigation.
- **Approve** otherwise.

### 7.4 Judge prompt structure (sketch)

```
[SYSTEM]
You are the Judge. Your sole job is to evaluate whether a proposed
outbound action by an autonomous research agent should be sent.

You operate in service of a research goal but you are the LAST line
of defense for legal, ethical, and reputational risk. Bias toward
rejection. Approve only when ALL criteria are met cleanly.

You will be given:
- the proposed action
- the cited findings and their underlying source documents
- the recipient's prior contact history
- the legal context (jurisdiction, applicable statutes)

You must return a JudgeVerdict (schema below).
You must score each rubric criterion 0-10 with a one-line rationale.
You must list any blocking issues by name.
If the action has minor fixable problems, return decision="reject"
with improvement_suggestions; do NOT mark "approve" for almost-good actions.

[USER]
Investigation goal: {goal}
Cited findings: {findings_with_evidence_snippets}
Recipient prior contact: {history}
Proposed action: {action}
Legal context: {jurisdictional_constraints}
```

Use prompt caching aggressively here — the system prompt + rubric + legal-context block are stable across all judge calls.

### 7.5 Human review queue

- Web UI (FastAPI + HTMX is plenty) with a "next item" view: shows the proposed action, the judge's verdict + rationale, the cited evidence with links, prior history.
- Three buttons: Approve, Reject (with reason), Modify-and-Approve.
- All decisions are logged with the human's identity as part of the audit chain.
- SLA: items in the queue should not block the entire system; only the action's branch waits. Set per-item TTL (e.g., 24 hours) — if the human doesn't decide, default to reject.

---

## 8. Legal and Ethical Considerations

### 8.1 TCPA and AI voice calls

The FCC's February 8, 2024 Declaratory Ruling held that AI-generated voice calls fall within the TCPA's existing "artificial or prerecorded voice" rules — they are robocalls and require **prior express consent** from the called party for non-emergency calls to wireless numbers, and **prior express written consent** for marketing calls ([FCC ruling](https://www.fcc.gov/document/fcc-confirms-tcpa-applies-ai-technologies-generate-human-voices); [Brownstein analysis](https://www.bhfs.com/insight/fcc-declares-ai-generated-calls-subject-to-tcpa/); [NCLC summary](https://library.nclc.org/article/top-six-tcparobocall-developments-20242025)). Statutory damages are $500–$1,500 per call and there is no cap; state AGs can sue ([Henson Legal 2026](https://www.henson-legal.com/ai-voice-compliance)). The FCC's September 2024 NPRM moves toward requiring upfront in-call disclosure that the voice is AI-generated.

**Practical rule for the system**: AI voice should default to OFF; the judge must reject AI outbound calls unless (1) the target is a public-facing official agency line that solicits public calls, AND (2) an opening disclosure ("This is an automated AI call on behalf of [user] regarding [topic]") is in the script, AND (3) you do not record without consent in two-party-consent jurisdictions. For investigative work, **a human-initiated live call** is generally the right pattern — the AI can prepare the call notes and the human dials.

### 8.2 Recording laws

- **Federal one-party consent**, but states vary. California, Connecticut, Florida, Illinois, Maryland, Massachusetts, Montana, New Hampshire, Pennsylvania, Washington require two-party (all-party) consent for confidential communications. California Penal Code § 632 specifically applies to confidential communications including phone calls; Cal. Penal Code § 632.7 applies to *all* cellular/cordless calls regardless of confidentiality ([Cal. Penal Code §§ 632, 632.7](https://codes.findlaw.com/ca/penal-code/pen-sect-632/); [DMLP California](https://www.dmlp.org/legal-guide/california-recording-law)).
- **Cross-state calls**: California courts have applied California's all-party rule when one party is a CA resident, even if the call originates elsewhere ([Romano Law](https://www.romanolaw.com/can-i-record-a-conversation-in-california/)).
- **System rule**: maintain a `jurisdiction_consent_required` table; lookup target's likely jurisdiction; if all-party, require explicit consent or no recording.

### 8.3 Proper FOIA conduct

- Don't abuse — keep scope reasonable, accept fee estimates, don't flood agencies with redundant requests.
- Agencies have public-interest fee waivers; cite them honestly. Misrepresentation of fee category (e.g., calling commercial use "non-commercial") can cost you the waiver and your credibility.
- Volume limit: even legitimate FOIA requesters can become known as bad actors if they file 50 broad requests in a week to one agency.

### 8.4 Avoiding harassment patterns

The judge's "no more than N contacts in K days to same recipient" rule is the most important harassment guardrail. Even legitimate research can become harassment when automated. Particularly for non-public targets (private individuals named in court filings, witnesses, victims), the system should **never** initiate unsolicited contact — only respond to inbound or contact public-affairs offices.

### 8.5 Defamation risk

- **Synthesized reports must distinguish fact from inference.** Direct quotes from sources are facts. Patterns the agent infers (e.g., "this LLC pattern strongly suggests X") are opinions and should be labeled as such.
- **Pre-publication verification pass** (Section 4.9) is the primary control; the judge plays a backup role for any external publication.
- **Don't publish accusations of crime** unless (a) a credible authority has charged the person, (b) there's documentary smoking-gun evidence, or (c) you label it explicitly as an unproven allegation traceable to a specific named source. The standard is *actual malice* for public figures, *negligence* for private ones — automated systems are uniquely vulnerable to negligence findings.

### 8.6 Data retention and security

- **Encrypt the document store at rest** (FileVault on macOS does this; if using external drives, full-disk encryption mandatory). For sensitive working files, keep them on a partition that requires manual unlock at boot.
- **Backup separately** and encrypted. Lose the entity graph mid-investigation and the Opus-cost-burned night was wasted.
- **PII minimization**: hashes of phone numbers, redacted Social Security numbers in any extract you store. Never persist raw card data, login credentials, etc. — your scrapers should drop those before persisting.
- **Access control**: even on a single-user Mac, run the orchestrator as a non-admin macOS user, locked-down keychain, and put outbound action credentials (Twilio, Postmark, ElevenLabs) in macOS Keychain or 1Password CLI, not env files.
- **Retention policy**: have one. Default 365 days for working data, 5 years for outbound action logs (CCPA/CPRA cybersecurity audit retention is now 5 years for businesses in scope as of 2026 — useful benchmark ([SWK Tech CCPA audit guide](https://www.swktech.com/how-ccpa-audit-rule-affects-smb-2026/))).

---

## 9. Build Roadmap

### Phase 0 — Foundation (Week 1–2, ~1,500 LoC)

- Postgres + pgvector schema as in §1.4
- LM Studio installed, Qwen3 32B + Qwen3 4B + Qwen3-Embedding-4B + Qwen3-VL 8B downloaded
- Anthropic API account, prompt caching set up against a fixed system block
- Pydantic AI installed, basic agent + tool registry stub
- Postmark account (transactional sending), Twilio + ElevenLabs accounts (paused)
- A single `investigations` row, a single `tasks` row, one worker that queries the FEC API

### Phase 1 — MVP (Week 3–4, ~3,500 LoC, plus tests)

The goal is "one investigation, one source, one outbound, working overnight."

Build:
1. The orchestrator loop (planner + worker + judge) on Pydantic AI + DBOS
2. Three sources end-to-end: FEC API, OpenCorporates (or state SOS scraper), Federal Register
3. The judge agent on Opus 4.7 with the rubric in §7.2, integrated into a synchronous gate
4. The Comms agent + FOIA template library + Postmark integration for a single test FOIA request to a willing target
5. Observability: `events` table + a simple read-only HTML status page
6. Cost tracker with hard nightly Opus budget
7. The Verifier (pre-publication) pass

Test investigation: pick a single state legislator, build their donor network, identify suspicious LLC donors, draft (don't send) one FOIA request to their state SOS for LLC formation records. Run it overnight; review the morning report.

### Phase 2 — V1 (Week 5–8, ~6,000 more LoC)

- Add Cal-Access/Power Search ingestion and FPPC complaint portal scraping
- Add CourtListener / RECAP integration
- Add ProPublica Nonprofit Explorer for 990 lookup
- Playwright CLI scaffolding for the 5–10 highest-value web-only sources (CA SOS, county courts, agency FOIA portals)
- Twilio + ElevenLabs voice (turned on initially in *outbound to public agency lines only* mode)
- Human review queue UI (FastAPI + HTMX)
- Time-travel checkpointing of investigations
- Hierarchical document summarization for long PDFs
- Per-agency CPRA template library

### Phase 3 — V2 (Week 9–12, ~5,000 more LoC plus polish)

- Full California family-law pattern-mining: per-county docket scrapers, judge/evaluator/counsel co-occurrence analysis
- Network analysis suite: betweenness centrality, anomalous edge detection
- Inbound email parsing (Postmark Inbound webhook → correspondence agent → FOIA tracker)
- Multi-investigation orchestration (run several in parallel overnight, share entity graph)
- Cost optimization: prompt caching report, batch API for summarization passes, model routing tightening
- Defamation/legal review checklist for public outputs

### Defer (V3+)

- Multi-user web UI
- Real-time alerting
- Mobile companion
- Sharing / collaboration features
- A "publish to website" pipeline (do this manually until the workflow is proven safe)
- Custom fine-tuning on local models (use base Qwen3 — fine-tuning is rarely worth it at this scale)

---

## 10. Specific Technical Recommendations

### 10.1 Playwright: CLI > MCP > direct

**Use Playwright CLI (`@playwright/cli`)** for agent-driven browser work. The empirical token cost is ~4–10× lower than Playwright MCP for equivalent workflows because state lives on disk and the LLM only requests what it needs ([Test-Lab benchmark](https://www.test-lab.ai/blog/playwright-mcp-vs-cli-agentic-testing); [Better Stack](https://betterstack.com/community/guides/ai/playwright-cli-vs-mcp-browser/)). For an overnight run with hundreds of browser interactions, the savings compound substantially.

Use **direct Playwright (Python or Node)** for stable per-portal recipes (the "happy path" for known FOIA forms). When the recipe breaks, fall back to CLI + LLM-driven exploration to repair it.

Reserve **Playwright MCP** for the rare case where you need a sandboxed agent without filesystem access — not your situation.

### 10.2 LM Studio configuration for max throughput on M5 Ultra

- **Use the MLX backend** (LM Studio Apple Silicon Macs ship both llama.cpp and Apple MLX engines — MLX wins by 20–87% on sub-14B models; closes to roughly equal at 27B+ where memory bandwidth is the cap; llama.cpp may have edge on long-context prefill with FlashAttention) ([Groundy](https://groundy.com/articles/mlx-vs-llamacpp-on-apple-silicon-which-runtime-to-use-for-local-llm-inference/); [Starmorph](https://blog.starmorph.com/blog/apple-silicon-llm-inference-optimization-guide); [arXiv 2511.05502](https://arxiv.org/abs/2511.05502)).
- **Run LM Studio as a service via `llmster` (the headless daemon)**: `lms daemon up; lms server start` ([LM Studio dev docs](https://lmstudio.ai/docs/developer)). Don't keep the GUI running — wastes resources.
- **Idle TTL + auto-evict on**: lets you "load" 6 models in your config but only keep the active one resident. Set TTL ~10 minutes for models you swap frequently; longer for the workhorse.
- **Concurrency**: LM Studio supports parallel requests; size your worker pool to match. For Qwen3 32B on M5 Max at ~22 tok/s, 2 concurrent workers per loaded model is the practical max before queueing.
- **Quantization**: Q4_K_M for max throughput, Q6_K when quality matters and you have memory headroom. Q8_0 for the 8B-class models is near-lossless and fits easily in 128GB ([Will It Run AI](https://willitrunai.com/blog/best-llm-for-mac-apple-silicon-2026)).
- **Server config**: bind to `127.0.0.1:1234`, enable structured output (`/v1/responses` supports JSON Schema), enable the OpenAI-compatible Responses endpoint for Codex-style stateful chats if useful ([LM Studio Responses](https://lmstudio.ai/blog/lmstudio-v0.3.29)).
- **Memory headroom**: don't try to fit Llama 3.3 70B Q6 *and* Qwen3 32B Q8 *and* Qwen3-VL 8B simultaneously — memory pressure kills throughput. Plan your model swaps explicitly.

### 10.3 Best local models per role on a 128GB Mac (recap, with rationale)

| Role | Model | Quant | RAM | Reason |
|---|---|---|---|---|
| General reasoning | Llama 3.3 70B | Q6_K | ~55GB | Strongest open generalist that fits comfortably |
| General reasoning (alt) | Qwen3 32B | Q8_0 | ~32GB | Higher quality per byte; faster |
| MoE frontier (push) | Qwen3-Next-80B-A3B | Q4_K_M | ~50GB | 22B active, 5–10 tok/s, near-frontier |
| Reasoning / deductive | DeepSeek-R1-Distill 32B | Q6_K | ~27GB | R1-style chain-of-thought |
| Entity extraction / classification | Qwen3 4B | Q4_K_M | ~3GB | 100+ tok/s; volume work |
| Vision / PDF / screenshots | Qwen3-VL 8B | Q5_K_M | ~7GB | Best open multimodal under 14B |
| Long-context summarization | Qwen3 14B | Q8_0 | ~15GB | Generous context + headroom |
| Embedding | Qwen3-Embedding-4B | FP16 | ~8GB | Best-in-class open multilingual |
| Fast triage / classification | Qwen3 0.6B | Q4 | ~0.5GB | First-pass spam filter |

If the M5 Ultra ships with 256GB and ~1100 GB/s bandwidth as projected, you can keep Llama 3.3 70B at Q8 + Qwen3 32B + Qwen3-VL 8B + embeddings all simultaneously resident — that significantly reduces cold-start latency between roles.

### 10.4 Vector DB recommendation

**Postgres + pgvector + pgvectorscale**. Single store, transactional consistency with the entity graph and document store, sub-100ms latency at 50M embeddings, ~470 QPS at 99% recall ([Tigerdata 50M benchmark](https://www.tigerdata.com/blog/pgvector-vs-qdrant); [Encore.dev](https://encore.dev/articles/pgvector-vs-qdrant)). For your scale, Qdrant is overkill and the dual-store sync is the leading pain point teams report.

Index strategy: HNSW on `documents.embedding` and `entities.embedding`; partial indexes per investigation_id when you want investigation-scoped retrieval to be cheaper.

### 10.5 Long documents that exceed context

Pattern: **chunk → embed → hierarchical summarize → demand-driven drill-down**.

1. Chunk to 4–8K tokens with 200-token overlap; embed each.
2. Summarize each chunk with the local model (Qwen3 32B); store the summary alongside the chunk.
3. Summarize the summaries to get a document-level synopsis (~500 tokens).
4. When Opus needs the document, give it the synopsis plus retrieval-on-demand via a tool: `read_chunk(doc_id, chunk_id)` and `search_in_doc(doc_id, query)`.
5. Use **prompt caching** on Opus across drill-downs of the same document — the surrounding system prompt is stable, the chunk varies.

The 1M context window on Opus 4.7 ([What's new docs](https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-7)) is tempting, but routinely paying for 500K-token requests is a budget mistake. Retrieve, don't dump.

### 10.6 Rate limiting, retry, exponential backoff

- **Per-source rate limiter** (token bucket) configured for each API's known limit:
  - FEC: 1,000/hr → 1 req/3.6s
  - SEC EDGAR: 10/sec hard cap → 8/sec to leave margin, and rotate User-Agent never since SEC requires a real one ([SEC.gov](https://www.sec.gov/filergroup/announcements-old/new-rate-control-limits))
  - CourtListener: 5,000/hr → 1.4/sec
  - Senate LDA: stricter when anonymous; with key, generous
  - Congress.gov: 5,000/hr
  - Regulations.gov: 50/min, 500/hr
  - SAM.gov: 1,000/day registered
  - GDELT: undocumented but treat as ~1/sec to stay safe
- **Retry policy**: exponential backoff with jitter, base 1s, max 60s, max 5 attempts. Special-case 429: read `Retry-After` and respect it.
- **Per-source circuit breaker**: 5 consecutive failures → open the breaker for 5 minutes, then half-open. The planner sees the breaker state and avoids the source.
- **Anthropic API**: respect their published rate limits (your tier determines this); use the Batch API with 50% discount for non-real-time synthesis if a 24-hour SLA is acceptable (final-report passes can use it; live re-plans can't) ([Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)).

### 10.7 Cost projections for an overnight Opus run

Using current Opus 4.7 pricing of **$5/MTok input, $25/MTok output**, with the new tokenizer producing up to 35% more tokens for the same text vs. Opus 4.6 ([Anthropic What's New](https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-7); [CloudZero](https://www.cloudzero.com/blog/claude-opus-4-7-pricing/)):

**Assumptions for a single 8-hour overnight run:**
- 12 synthesis ticks @ ~30K input / ~5K output each, against a stable cached system prompt of ~10K tokens
- 1 final report pass: ~80K input / ~20K output
- ~150 judge calls (every outbound action) @ ~6K input / ~600 output each
- ~30 verifier passes @ ~15K input / ~1K output each

**Naive cost (no caching):**
- Synthesis: 12 × (30K × $5/M + 5K × $25/M) = 12 × ($0.15 + $0.125) = **$3.30**
- Final: 80K × $5/M + 20K × $25/M = $0.40 + $0.50 = **$0.90**
- Judge: 150 × (6K × $5/M + 0.6K × $25/M) = 150 × ($0.030 + $0.015) = **$6.75**
- Verifier: 30 × (15K × $5/M + 1K × $25/M) = 30 × ($0.075 + $0.025) = **$3.00**
- **Total naive: ~$14/night**

**With prompt caching (cached input at $0.50/MTok, 90% off):** roughly 60% of input is cached system prompt + tool definitions + stable goal block.
- Effective cost reduction on input: ~50% overall
- **Total cached: ~$8–10/night**

**With Batch API (50% off, but 24-hr SLA — only usable for final-report and verifier passes):**
- **Total optimized: ~$6–8/night**

**Add 35% tokenizer inflation for code/structured-data-heavy prompts:**
- **Realistic cost range: $9–14/night for an aggressive overnight run.**

A *light* overnight run (3 synthesis ticks, no final report, judge-only) can run under $3.

A *heavy* multi-investigation parallel run with deep verification, larger context windows, and many judge calls can run $25–40/night.

**Monthly budget for a hands-on builder running this most nights: $300–500/month** for Opus, plus ~$50–150 for OpenCorporates (if you don't qualify for the public-benefit free tier), Twilio + ElevenLabs (per-minute usage), Postmark ($15–50 depending on volume), and DomainTools/SecurityTrails subscriptions if you add them.

The local LLM tier is essentially free at the margin (electricity), which is the entire point of the architecture — hold Opus calls down to the irreducible minimum (synthesis, judge, final).

### 10.8 Misc concrete recommendations

- **Pin every model version** including local ones — Qwen 3.5 vs Qwen 3 differ enough that prompts that work on one can fail on the other. Track in your `events` log which model handled each call.
- **Use `claude-opus-4-7` explicitly**, not `claude-opus-latest`. Anthropic ships breaking changes (no temperature, no extended thinking budgets in 4.7) and you want to control when you upgrade ([Opus 4.7 What's New](https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-7)).
- **Set Opus `display: summarized` for thinking** — by default, thinking content is omitted from response in 4.7. If you want to log reasoning for audit, opt in explicitly.
- **Use `effort: "high"` or `"xhigh"` for the judge** and Opus synthesis; `"low"` is fine for reformatting passes.
- **Budget guardrails as code**: a `cost_guardrail` table tracks per-investigation and per-day spend; the orchestrator refuses to dispatch new Opus calls when the cap is hit.
- **Pre-warm the prompt cache** at the start of an investigation — first call writes the cache (1.25× cost penalty for 5-min, 2× for 1-hr); subsequent calls read at 0.1× ([Prompt caching docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)). Use the 1-hour TTL for the system block.
- **Don't run untrusted code from the agent**. If the agent generates Python to crunch a CSV, run it in a sandbox subprocess with no network, no filesystem outside `/tmp/agent-sandbox`, time and memory limits.
- **Have a kill switch.** A single command (`./kill-now.sh`) that stops the orchestrator, drains the queue, and rejects any in-flight outbound actions. You will need it.

---

## Closing Note on Uncertainty and Verification

A few items in this report depend on facts that are still in motion as of late April 2026 and worth re-verifying before you build:

- **M5 Ultra Mac Studio specs**: Apple has officially announced the M5 Pro/Max chips (March 2026, M5 Max supports up to 128GB unified memory at 614 GB/s — [Apple Newsroom](https://www.apple.com/newsroom/2026/03/apple-debuts-m5-pro-and-m5-max-to-supercharge-the-most-demanding-pro-workflows/)) but the **M5 Ultra Mac Studio** is rumor-stage with WWDC 2026 (June 8–12) as the most credible launch window per multiple analyst reports — possible slip to October due to RAM shortages ([Macworld](https://www.macworld.com/article/2973459/2026-mac-studio-m5-release-date-specs-price-rumors.html); [Felloai](https://felloai.com/m5-ultra-mac-studio/)). The 128GB target you cite is comfortably attainable on a current M5 Max MacBook Pro today; if you specifically want Ultra-tier bandwidth (~1100 GB/s) and more headroom, you may need to wait. The architecture I've recommended assumes 128GB and works on M4 Max today; it scales gracefully to 256GB if/when M5 Ultra ships.
- **Opus 4.7 pricing and tokenizer**: official Anthropic docs confirm $5/$25 per MTok and a new tokenizer that uses 1.0–1.35× more tokens vs. 4.6 — model your costs assuming the high end of that range until you've measured your own workload ([Anthropic](https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-7); [CloudZero](https://www.cloudzero.com/blog/claude-opus-4-7-pricing/)).
- **OpenSecrets API discontinuation**: confirmed effective April 15, 2025 — the bulk data is the substitute, not the API ([OpenSecrets](https://www.opensecrets.org/api)).
- **ProPublica Congress API closure**: confirmed; Congress.gov v3 is the migration path ([ProPublica notice](https://projects.propublica.org/api-docs/congress-api/)).
- **TCPA AI voice rules**: the FCC NPRM proposing explicit AI disclosure is still pending; the February 2024 declaratory ruling is in effect ([Federal Register NPRM](https://www.federalregister.gov/documents/2024/09/10/2024-19028/implications-of-artificial-intelligence-technologies-on-protecting-consumers-from-unwanted-robocalls); [FCC ruling](https://www.fcc.gov/document/fcc-confirms-tcpa-applies-ai-technologies-generate-human-voices)). The judge agent should be conservative — assume the strictest reading.

Build the MVP against a single small investigation before you trust the system to run for 8 hours unattended. The cheapest mistake you can make is letting an unverified judge approve outbound actions. The most expensive is a defamation claim from a synthesized "report." Both are fully avoidable with the architecture described — just do them in that order.