"""Research loop — task runner with retry, follow-ups, and synthesis cadence.

Implements §6.1 of ``research-agent-implementation-guide.md``: a single
``while not job.should_stop() and not plan.is_complete()`` loop that pulls
the next pending task from the SQLite queue, dispatches it through a
``kind → handler`` registry, and records the outcome.

The loop has three resilience boundaries:

* :class:`~research_agent.orchestrator.errors.RetriableError` — handlers
  raise this for transient failures; the loop applies a tenacity backoff
  (waits ``RETRY_WAITS`` seconds, ``RETRY_MAX_ATTEMPTS`` total attempts).
* :class:`~research_agent.orchestrator.errors.FatalError` — the task is
  marked ``failed`` and the loop continues with the next task. One bad
  task does not kill the daemon.
* ``MAX_TASKS_PER_JOB`` hard cap — anti-runaway guard from §6.3. When hit
  the loop stops and best-effort triggers a final synthesis pass so the
  user gets *something* back even if the planner went off the rails.

Synthesis and critique cadence is driven by ``HEURISTIC_CHECK_EVERY_N``
(every 25 tasks) — real heuristics live with the synthesis/critique
modules; the loop just calls handlers if registered. Failures in those
heuristic-driven calls are emitted as ``warning`` events but never abort
the loop.

The single entry point is :func:`run_loop`. It accepts an optional
``handlers`` registry override (used by tests + for swapping connector
implementations) and an optional explicit ``plan`` (otherwise the latest
``plans`` row for the job is loaded).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
)

from research_agent.observability.events import emit
from research_agent.orchestrator.checkpoint import checkpoint
from research_agent.orchestrator.errors import FatalError, RetriableError
from research_agent.orchestrator.plan import Plan, TaskKind, TaskSpec
from research_agent.skills import load_skill, load_strategies
from research_agent.storage import db
from research_agent.storage.jobs import INBOX_REPLAN_FILE, Job, _atomic_write_text
from research_agent.storage.tasks import (
    enqueue,
    mark_done,
    mark_failed,
    mark_running,
    next_pending,
)
from research_agent.tools._errors import MissingCredentialError

logger = logging.getLogger(__name__)

MAX_TASKS_PER_JOB = 10000
HEURISTIC_CHECK_EVERY_N = 25
RETRY_WAITS: tuple[int, ...] = (1, 2, 4, 8, 16, 30, 60)
RETRY_MAX_ATTEMPTS = 5
# Medium-scope default for drain-replan cap. Issue #209 made the runtime
# cap scale with ``scope_class`` via ``_MAX_DRAIN_REPLANS_BY_SCOPE``; this
# constant is preserved as the medium-scope value and is still honored as
# the live cap when a plan has no ``scope_class`` set, so external imports
# and tests that monkeypatch this symbol continue to work.
MAX_DRAIN_REPLANS = 10

# Issue #209: scope-aware cap for ``run_loop``'s drain-replan budget. A
# fixed ``MAX_DRAIN_REPLANS=10`` (introduced by #117) is too tight for
# broad/comprehensive investigations that name a cornerstone document or
# enumerate dozens of sub-questions — a single cornerstone section-walk
# (per #206) plus drill-down on each closed finding can consume 15-25
# replans on its own. The medium row preserves the historical default.
_MAX_DRAIN_REPLANS_BY_SCOPE: dict[str, int] = {
    "narrow": 5,
    "medium": 10,
    "broad": 20,
    "comprehensive": 30,
}
_MAX_DRAIN_REPLANS_FALLBACK = 10

# Issue #209: minimum findings-per-subgoal before a goal is considered
# "materially answered". When the drain-replan cap fires but open subgoals
# still fall below the floor, the loop allows up to ``cap + floor`` more
# replans targeting the under-served subgoals before terminating. Floors
# scale with ``scope_class`` for the same reason caps do.
_FINDING_FLOOR_BY_SCOPE: dict[str, int] = {
    "narrow": 5,
    "medium": 10,
    "broad": 20,
    "comprehensive": 30,
}
_FINDING_FLOOR_FALLBACK = 10

# Connectors we always check for "did this fire?" when emitting the
# ``cap_diagnostic`` event — useful when the planner's last template
# narrowed to one or two kinds and the operator wants to see which
# retrieval surfaces never got exercised.
_CORE_CONNECTOR_KINDS = (
    "web_search",
    "congress_search",
    "edgar_search",
    "fedregister_search",
    "courtlistener_search",
)

# Scope-aware default for how many top hits per search become web_fetch
# follow-ups. Issue #178: a global default of 3 starves broad/comprehensive
# investigations of fetch surface area. The planner can still override per
# task via ``payload["expand_top_k"]``.
_DEFAULT_TOP_K_BY_SCOPE: dict[str, int] = {
    "narrow": 3,
    "medium": 5,
    "broad": 7,
    "comprehensive": 10,
}
_DEFAULT_TOP_K_FALLBACK = 3

# Issue #194: how many ``web_fetch`` follow-ups ``_run_extract_findings``
# may emit per extraction for URLs cited inside the findings it just wrote.
# Mirrors the scope-class scaling pattern from #178: narrow runs do not
# auto-fan-out at all (drill-downs at the planner level only), while
# comprehensive runs ride citation chains aggressively.
_SECOND_ORDER_MAX_BY_SCOPE: dict[str, int] = {
    "narrow": 0,
    "medium": 1,
    "broad": 2,
    "comprehensive": 3,
}
_SECOND_ORDER_MAX_FALLBACK = 1
# Don't fan out from low-confidence findings — the agent's least-reliable
# signal. ``>= 0.5`` is a starting threshold that the planner can override
# per task via ``payload['second_order_confidence_threshold']``.
_SECOND_ORDER_CONFIDENCE_THRESHOLD = 0.5
# Hard cap per job across all extracts. Prevents pathological cases where
# every fetched document carries dozens of cited URLs and the queue
# explodes — the user can still get the planner to drill into specific
# paths via ``tactical_replan`` once this budget is spent.
_SECOND_ORDER_JOB_CAP = 200
# URLs in fan-out candidates: match http/https plus a healthy slug of URL-
# safe characters. Trailing punctuation (``.``, ``,``, ``)``, ``]``, ``}``,
# ``>``, quotes) is stripped post-match by ``_normalize_url_for_fanout`` so
# the regex itself stays readable.
_URL_REGEX = re.compile(r"https?://[^\s)\]\}<>\"']+")
# Marker key written into a follow-up task's payload so we can SQL-query
# how many second-order tasks have already been emitted for the job (and
# enforce the per-job cap).
_SECOND_ORDER_PARENT_FINDING_ID_KEY = "second_order_parent_finding_id"
_QUERY_STEM_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "for",
    "from",
    "how",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "what",
    "when",
    "where",
    "who",
    "why",
    "with",
}

Handler = Callable[[Job, dict[str, Any]], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class _DrainReplanOutcome:
    plan: Plan | None
    productive_task_count: int | None
    should_continue: bool


# ---------------------------------------------------------------------------
# Default handler registry
# ---------------------------------------------------------------------------


async def _not_implemented_handler(job: Job, task: dict[str, Any]) -> dict[str, Any] | None:
    """Placeholder for task kinds whose connectors haven't shipped yet.

    Raises :class:`FatalError` so the loop marks the task ``failed`` and
    continues — this keeps the loop usable end-to-end while individual
    connector handlers land in their own follow-up issues.
    """
    raise FatalError(f"handler not implemented for kind={task['kind']!r}")


# Issue #175: connector kinds dispatch directly to each tool's structured
# API. Search payloads pass ``query`` plus any of these well-known kwargs
# through to the underlying ``search()`` call — most connectors take a
# ``kind`` switch (e.g. congress: bill/member/hearing) and a few take
# ``state``/``since``/``form_type``. The set is the *union* across all
# connectors; ``_filter_kwargs_for`` then narrows to what each connector
# actually accepts, so planner drift (e.g. emitting ``kind`` for
# ``edgar_search``, which takes ``form_type`` instead) doesn't surface as
# a TypeError that bypasses the documented FatalError path.
_CONNECTOR_SEARCH_PASSTHROUGH: frozenset[str] = frozenset(
    {
        "kind",
        "max_results",
        "cycle",
        "office",
        "state",
        "district",
        "party",
        "candidate_status",
        "max_rows",
        "per_page",
        "form_type",
        "since",
        "agencies",
        "jurisdiction",
        "award_type",
        "language",
        "lang",
        "category",
        "zone",
        "sortby",
        "type",
        "collection",
        "page",
        "mediatype",
        "fechaDesde",
        "fechaHasta",
        "localizacion",
        "filter",
        "sort",
        "available_online",
        "type_of_materials",
        "result_types",
        "record_group",
        "object_category",
        "web_category",
        "objectCategory",
        "related_period",
        "period",
        "periodString",
        "records_with_media",
        "recordsWithMedia",
        "style",
        "page_size",
        "pageSize",
    }
)

_TRANSLATE_NON_ENGLISH_KEY = "translate_non_english"
_LANGUAGE_HINT_KEYS: tuple[str, ...] = (
    "source_lang",
    "detected_language",
    "detected_lang",
    "language",
    "lang",
    "dc:language",
    "wikisource_lang",
)


def _filter_kwargs_for(fn: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop kwargs the callable doesn't accept.

    Connectors have heterogeneous signatures — congress takes ``kind``,
    edgar takes ``form_type``, fedregister takes ``since``/``agencies``.
    The handler's passthrough whitelist is the union; this narrows it to
    what ``fn`` actually accepts so a planner-drift kwarg like ``kind``
    on ``edgar_search`` is silently dropped instead of crashing the call.
    Falls back to the unfiltered dict when introspection fails (e.g.
    C-implemented or wrapped callables).
    """
    import inspect

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return kwargs
    accepts_var_kw = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if accepts_var_kw:
        return kwargs
    accepted = {
        name
        for name, p in sig.parameters.items()
        if p.kind
        in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    }
    return {k: v for k, v in kwargs.items() if k in accepted}


def _language_hints_from_mapping(mapping: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only language metadata fields that downstream extraction understands."""
    if not isinstance(mapping, dict):
        return {}
    hints: dict[str, Any] = {}
    for key in _LANGUAGE_HINT_KEYS:
        value = mapping.get(key)
        if value not in (None, "", []):
            hints[key] = value
    nested = mapping.get("metadata")
    if isinstance(nested, dict):
        for key in _LANGUAGE_HINT_KEYS:
            value = nested.get(key)
            if value not in (None, "", []) and key not in hints:
                hints[key] = value
    return hints


def _task_passthrough_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Forward extraction-only knobs from search/fetch tasks to follow-ups."""
    out: dict[str, Any] = {}
    if _TRANSLATE_NON_ENGLISH_KEY in payload:
        out[_TRANSLATE_NON_ENGLISH_KEY] = payload[_TRANSLATE_NON_ENGLISH_KEY]
    out.update(_language_hints_from_mapping(payload))
    return out


def _deep_load_skills_for_connector(
    job: Job,
    module_name: str,
    payload: dict[str, Any],
) -> None:
    """Deep-load the connector skill + any active strategy skills.

    Fires ``skills/skill_loaded`` telemetry for the matching connector
    (when its file ships) and for each strategy named on the latest plan's
    ``active_strategies`` (threaded into the task payload by
    :func:`_enqueue_plan_tasks` as ``_active_strategies``). The loaded
    bodies are not consumed yet — the LLM-driven kwarg-construction step
    that will read them is its own follow-up issue. Loading here is what
    fires telemetry and warms the cache so skill content is available the
    moment a downstream caller wants it.

    Missing skills return empty strings; this never raises so a planner
    that names a not-yet-shipped connector or strategy can't break the
    connector path.
    """
    try:
        load_skill("connectors", module_name, job=job)
        active = payload.get("_active_strategies")
        if isinstance(active, list) and active:
            load_strategies([s for s in active if isinstance(s, str)], job=job)
    except Exception:  # noqa: BLE001 — skills are auxiliary; never break the connector
        logger.exception("skills_deep_load_failed module=%s", module_name)


def _make_connector_search_handler(module_name: str) -> Handler:
    """Build a thin search-handler that dispatches to ``tools.<module_name>.search``.

    Converts the connector's :class:`MissingCredentialError` (raised by
    edgar/courtlistener/scholar/linkedin when their API key/UA isn't
    configured) into :class:`FatalError` so the loop marks the task failed
    cleanly down the documented path. Other ``RuntimeError`` subclasses —
    real connector bugs — propagate to the loop's catch-all so they
    surface as ``daemon/error`` events with tracebacks instead of getting
    masked as missing-credential failures. Returns the standard
    search-result + ``follow_up_tasks`` shape so each top hit becomes a
    connector-aware ``web_fetch`` follow-up.

    Before dispatching to ``mod.search``, deep-loads the matching
    connector skill (if shipped) plus any active strategies threaded into
    the payload by :func:`_enqueue_plan_tasks`. Only the relevant skill
    bodies are loaded — the per-connector knowledge stays out of the
    planner's system prompt and is materialized exactly at task-emit time.
    """

    async def _handler(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        from importlib import import_module

        mod = import_module(f"research_agent.tools.{module_name}")
        payload = task["payload"]
        _deep_load_skills_for_connector(job, module_name, payload)
        kwargs = {
            k: v for k, v in payload.items() if k in _CONNECTOR_SEARCH_PASSTHROUGH
        }
        kwargs = _filter_kwargs_for(mod.search, kwargs)
        try:
            results = await mod.search(payload.get("query", ""), **kwargs)
        except MissingCredentialError as exc:
            raise FatalError(f"{module_name}_search: {exc}") from exc
        return _expand_search_to_fetches(job, payload, results)

    return _handler


def _make_connector_fetch_handler(module_name: str) -> Handler:
    """Build a thin fetch-handler that dispatches to ``tools.<module_name>.fetch``.

    Mirrors :func:`_make_connector_search_handler` but for the single-URL
    fetch path: persists the returned ``Source`` and converts the
    connector's :class:`MissingCredentialError` to :class:`FatalError`.
    Plain ``RuntimeError`` from inside the connector propagates to the
    loop's catch-all so unexpected failures stay diagnosable. Deep-loads
    the connector skill so the same telemetry path fires whether the loop
    enters via the search or fetch side.
    """

    async def _handler(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        from importlib import import_module

        mod = import_module(f"research_agent.tools.{module_name}")
        payload = task["payload"]
        _deep_load_skills_for_connector(job, module_name, payload)
        url = payload.get("url")
        if not url:
            raise FatalError(f"{module_name}_fetch: missing url field")
        try:
            source = await mod.fetch(url)
        except MissingCredentialError as exc:
            raise FatalError(f"{module_name}_fetch: {exc}") from exc
        return _persist_fetched_source(job, source, payload=payload)

    return _handler


def _registered_connector_module_names() -> tuple[str, ...]:
    """Return the connector ``module_name`` for every registered direct kind.

    Replaces the hand-maintained ``_CONNECTOR_KINDS`` tuple. The order is
    deterministic (alphabetical) per :func:`iter_kinds`. Used by the handler
    registry below to wire one ``<x>_search``/``<x>_fetch`` pair per kind.
    """
    from research_agent.tools._registry import iter_kinds

    return tuple(entry.short_name for entry in iter_kinds())


def default_handlers(router: Any) -> dict[str, Handler]:
    """Build the standard ``kind → handler`` registry.

    Connector kinds that have a shipping module (``web_search``, ``web_fetch``,
    ``arxiv_*``, ``news_search``, ``reddit_search``, ``local_corpus_query``,
    ``synthesize``) get thin async wrappers that call into the connector.
    Kinds whose handlers haven't been implemented yet (``github_*``,
    ``extract_findings``, ``summarize_source``, ``critique``) get
    :func:`_not_implemented_handler` so the loop stays runnable end-to-end.
    """

    async def _web_search(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        from research_agent.tools import web_search

        payload = task["payload"]
        results = await web_search.search(
            payload.get("query", ""),
            max_results=payload.get("max_results", 10),
            engine=payload.get("engine", "auto"),
            lang=payload.get("lang"),
        )
        return _expand_search_to_fetches(job, payload, results)

    async def _web_fetch(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        """Fetch ``url`` and queue an ``extract_findings`` follow-up.

        The follow-up carries the actual ``source_id`` from this fetch
        plus the ``sub_question`` propagated by ``_web_search`` (or the
        job goal as a fallback). This is where the source_id-to-extract
        binding actually happens — the planner can't predict it.
        """
        from research_agent.orchestrator.plan import TaskSpec
        from research_agent.tools import web_fetch

        payload = task["payload"]
        source = await web_fetch.fetch(payload["url"])
        result = _persist_fetched_source(job, source, payload=payload)

        source_id = result.get("source_id")
        if isinstance(source_id, int):
            sub_question = payload.get("sub_question") or job.goal
            extract_payload = {
                "source_id": source_id,
                "sub_question": sub_question,
                **_task_passthrough_payload(payload),
            }
            follow_up = TaskSpec(
                kind="extract_findings",
                payload=extract_payload,
            )
            # Issue #193: ``_persist_fetched_source`` may have already added
            # a bill-text fan-out follow-up (when web_fetch host-dispatched
            # to congress.fetch). Merge rather than overwrite so the bill
            # body still gets fetched alongside the extract on this index.
            existing = result.get("follow_up_tasks") or []
            result["follow_up_tasks"] = [*existing, follow_up.model_dump()]
        return result

    async def _arxiv_search(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        from research_agent.tools import arxiv_tool

        payload = task["payload"]
        results = await arxiv_tool.search(
            payload.get("query", ""),
            max_results=payload.get("max_results", 10),
        )
        return {"results": [r.model_dump(mode="json") for r in results]}

    async def _arxiv_fetch(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        from research_agent.tools import arxiv_tool

        payload = task["payload"]
        source = await arxiv_tool.fetch(payload["arxiv_id"])
        return _persist_fetched_source(job, source)

    async def _extract_findings(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        return await _run_extract_findings(job, task, router=router)

    async def _summarize_source(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        return await _run_summarize_source(job, task, router=router)

    async def _news_search(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        from research_agent.tools import news

        payload = task["payload"]
        results = await news.search(payload.get("query", ""))
        return _expand_search_to_fetches(job, payload, results)

    async def _reddit_search(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        from research_agent.tools import reddit

        payload = task["payload"]
        results = await reddit.search(payload.get("query", ""))
        return _expand_search_to_fetches(job, payload, results)

    async def _local_corpus_query(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        from research_agent.tools import local_corpus

        payload = task["payload"]
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            None,
            lambda: local_corpus.search(
                payload.get("query", ""),
                job,
                top_k=payload.get("top_k", 10),
            ),
        )
        return {"results": results}

    async def _cornerstone_query(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        return await _run_cornerstone_query(job, task, router=router)

    async def _synthesize(job: Job, task: dict[str, Any]) -> dict[str, Any] | None:
        from research_agent.orchestrator.synth import (
            final_synthesis,
            fragment_synth_enabled,
            synthesize,
            synthesize_fragments,
        )

        plan = _load_latest_plan(job)
        if plan is None:
            raise FatalError("synthesize: no plan persisted for job")
        payload = task.get("payload") or {}
        final = bool(payload.get("final"))
        if fragment_synth_enabled():
            output = await synthesize_fragments(job, plan, router=router, final=final)
        elif final:
            output = await final_synthesis(job, plan, router=router)
        else:
            output = await synthesize(job, plan, router=router)
        return output.model_dump()

    async def _critique(job: Job, task: dict[str, Any]) -> dict[str, Any] | None:
        from research_agent.orchestrator.critique import critique
        from research_agent.orchestrator.plan import cloud_replan

        plan = _load_latest_plan(job)
        if plan is None:
            raise FatalError("critique: no plan persisted for job")
        latest_synth = _load_latest_synthesis_md(job)
        result = await critique(job, plan, latest_synth, router=router)
        if result.should_replan:
            critique_md = _load_critique_md(job, result.md_path)
            new_plan = await cloud_replan(job, plan, critique_md, router=router)
            emit(
                job,
                "INFO",
                "critique",
                "replan_triggered",
                {
                    "from_version": plan.version,
                    "critique_version": result.version,
                },
            )
            checkpoint(
                job,
                "replan_done",
                {
                    "from_version": plan.version,
                    "to_version": new_plan.version,
                    "critique_version": result.version,
                },
            )
        return result.model_dump()

    registry: dict[str, Handler] = {
        "web_search": _web_search,
        "web_fetch": _web_fetch,
        "arxiv_search": _arxiv_search,
        "arxiv_fetch": _arxiv_fetch,
        "news_search": _news_search,
        "reddit_search": _reddit_search,
        "local_corpus_query": _local_corpus_query,
        "cornerstone_query": _cornerstone_query,
        "github_search": _not_implemented_handler,
        "github_fetch": _not_implemented_handler,
        "extract_findings": _extract_findings,
        "summarize_source": _summarize_source,
        "synthesize": _synthesize,
        "critique": _critique,
    }
    _annotate_heuristic_handler(_synthesize, tier="frontier", router=router)
    _annotate_heuristic_handler(_critique, tier="frontier_alt", router=router)
    for name in _registered_connector_module_names():
        registry[f"{name}_search"] = _make_connector_search_handler(name)
        registry[f"{name}_fetch"] = _make_connector_fetch_handler(name)
    return registry


# ---------------------------------------------------------------------------
# Loop helpers
# ---------------------------------------------------------------------------


def _should_stop(job: Job) -> bool:
    """True when the operator dropped a ``STOP`` flag in the job folder."""
    return (job.root / "STOP").exists()


def _should_synthesize(plan: Plan, tasks_done: int) -> bool:
    """v1 synthesis heuristic: every ``HEURISTIC_CHECK_EVERY_N`` tasks.

    The "real" heuristic — only synthesize if there are unsummarized
    findings — lives with the synthesis module that lands later. The loop
    just provides the cadence.
    """
    return tasks_done > 0 and tasks_done % HEURISTIC_CHECK_EVERY_N == 0


CRITIQUE_CADENCE_MULTIPLIER = 2


def _should_critique(plan: Plan, tasks_done: int) -> bool:
    """v1 critique heuristic: every 2× the synthesis cadence (default: 50).

    The original 4× (100 tasks) meant a 25-task SBI Builders run never
    got a critique pass, so the loop never noticed that 5/8 searches
    returned 0 hits and a tactical replan was needed. 2× catches that
    case after one round of synthesis-and-look.
    """
    n = HEURISTIC_CHECK_EVERY_N * CRITIQUE_CADENCE_MULTIPLIER
    return tasks_done > 0 and tasks_done % n == 0


def _configured_model_name(router: Any, tier: str) -> str | None:
    tiers = getattr(router, "tiers", None)
    if not isinstance(tiers, dict):
        return None
    spec = tiers.get(tier)
    if not isinstance(spec, dict):
        return None
    model = spec.get("model")
    return model if isinstance(model, str) and model else None


def _annotate_heuristic_handler(handler: Handler, *, tier: str, router: Any) -> None:
    """Attach static LLM context so best-effort heuristic failures are debuggable."""
    handler._heuristic_tier = tier  # type: ignore[attr-defined]
    model = _configured_model_name(router, tier)
    if model is not None:
        handler._heuristic_model = model  # type: ignore[attr-defined]
    if router is not None:
        handler._heuristic_router = router  # type: ignore[attr-defined]


def _heuristic_failure_payload(
    heuristic: str,
    exc: Exception,
    handler: Handler,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "heuristic": heuristic,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }
    tier = getattr(handler, "_heuristic_tier", None)
    if isinstance(tier, str) and tier:
        payload["tier"] = tier
    model = getattr(handler, "_heuristic_model", None)
    if isinstance(model, str) and model:
        payload["model"] = model

    router = getattr(handler, "_heuristic_router", None)
    last_call = getattr(router, "last_call_metadata", None)
    if isinstance(last_call, dict):
        last_tier = last_call.get("tier")
        last_model = last_call.get("model")
        if isinstance(last_tier, str) and last_tier:
            payload["last_llm_tier"] = last_tier
        if isinstance(last_model, str) and last_model:
            payload["last_llm_model"] = last_model
    return payload


def _load_latest_plan(job: Job) -> Plan | None:
    """Read the highest-version ``plans`` row for ``job`` and return it as :class:`Plan`."""
    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM plans WHERE job_id = ? ORDER BY version DESC LIMIT 1",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return Plan.model_validate_json(row["payload_json"])


def _load_latest_synthesis_md(job: Job) -> str | None:
    """Read the markdown content of the highest-version persisted synthesis."""
    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            "SELECT md_path FROM syntheses WHERE job_id = ? ORDER BY version DESC LIMIT 1",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    md_path = job.root / row["md_path"]
    if not md_path.exists():
        return None
    return md_path.read_text(encoding="utf-8")


def _load_critique_md(job: Job, md_rel: str) -> str:
    """Read the rendered markdown for a critique that was just written."""
    md_path = job.root / md_rel
    return md_path.read_text(encoding="utf-8")


def _load_all_findings(job: Job) -> list[dict[str, Any]]:
    """Return every persisted finding for ``job`` as a list of dicts.

    Drives the drain-replan context: the planner sees what's been learned
    so far and can pivot rather than re-emit the same template. ``tags`` and
    ``source_ids`` are stored as JSON in the column; we leave them as the
    raw string the planner can read alongside the structured ``claim`` and
    ``confidence`` fields.
    """
    import json as _json

    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT id, claim, confidence, source_ids, tags, target_fragments FROM findings"
            " WHERE job_id = ? ORDER BY id ASC",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            source_ids = _json.loads(row["source_ids"]) if row["source_ids"] else []
        except (TypeError, ValueError):
            source_ids = []
        try:
            tags = _json.loads(row["tags"]) if row["tags"] else None
        except (TypeError, ValueError):
            tags = None
        try:
            target_fragments = (
                _json.loads(row["target_fragments"]) if row["target_fragments"] else []
            )
        except (TypeError, ValueError):
            target_fragments = []
        out.append(
            {
                "id": int(row["id"]),
                "claim": row["claim"],
                "confidence": float(row["confidence"]),
                "source_ids": source_ids,
                "tags": tags,
                "target_fragments": target_fragments,
            }
        )
    return out


def _load_recent_task_results(job: Job, limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recently completed tasks for ``job`` (newest first).

    Feeds the drain-replan with concrete evidence of what the prior plan
    actually produced, so the planner can decide whether to broaden,
    deepen, or pivot.
    """
    import json as _json

    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT id, kind, payload_json, result_json FROM tasks"
            " WHERE job_id = ? AND status = 'done'"
            " ORDER BY id DESC LIMIT ?",
            (job.id, int(limit)),
        ).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = _json.loads(row["payload_json"]) if row["payload_json"] else {}
        except (TypeError, ValueError):
            payload = {}
        try:
            result = _json.loads(row["result_json"]) if row["result_json"] else None
        except (TypeError, ValueError):
            result = None
        out.append(
            {
                "task_id": int(row["id"]),
                "kind": row["kind"],
                "payload": payload,
                "result": result,
            }
        )
    return out


def _load_failed_task_signatures(job: Job) -> set[tuple[str, str]]:
    """Return signatures for failed tasks so replans can be scored for novelty."""
    import json as _json

    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT kind, payload_json FROM tasks"
            " WHERE job_id = ? AND status = 'failed'",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()

    signatures: set[tuple[str, str]] = set()
    for row in rows:
        try:
            payload = _json.loads(row["payload_json"]) if row["payload_json"] else {}
        except (TypeError, ValueError):
            payload = {}
        signatures.add(_task_signature(str(row["kind"]), payload))
    return signatures


def _load_all_task_attempts(job: Job) -> list[dict[str, Any]]:
    """Return all task rows needed for replan prior-attempt context."""
    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT id, kind, payload_json, status, result_json, error"
            " FROM tasks WHERE job_id = ?"
            " ORDER BY id ASC",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _query_stem(payload: dict[str, Any] | None, *, max_words: int = 6) -> str:
    """Normalize a task payload's query-ish field into a stable comparison stem."""
    if not isinstance(payload, dict):
        return ""
    raw: Any = None
    for key in ("query", "q", "sub_question", "url", "arxiv_id", "source_id"):
        value = payload.get(key)
        if value not in (None, "", []):
            raw = value
            break
    if raw is None:
        return ""
    text = str(raw).lower()
    words = [
        w
        for w in re.findall(r"[a-z0-9]+", text)
        if w and w not in _QUERY_STEM_STOPWORDS
    ]
    if not words:
        words = re.findall(r"[a-z0-9]+", text)
    return " ".join(words[:max_words])


def _task_signature(kind: str, payload: dict[str, Any] | None) -> tuple[str, str]:
    return (kind, _query_stem(payload))


def _productive_task_count(
    plan: Plan,
    *,
    failed_signatures: set[tuple[str, str]],
) -> int:
    """Count replan tasks that are not repeats of already-failed signatures."""
    count = 0
    for spec in plan.task_template:
        if _task_signature(spec.kind, spec.payload) in failed_signatures:
            continue
        count += 1
    return count


def _latest_inconclusive_subgoal_ids(job: Job, plan: Plan) -> set[int]:
    """Return currently open ids from the latest subgoal-status event's inconclusive set."""
    import json as _json

    open_ids = {sg.id for sg in plan.subgoals if not sg.done}
    if not open_ids:
        return set()
    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM events"
            " WHERE job_id = ? AND kind = 'plan_subgoals_updated'"
            " ORDER BY id DESC LIMIT 1",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return set()
    try:
        payload = _json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return set()
    raw_ids = payload.get("inconclusive")
    if not isinstance(raw_ids, list):
        return set()
    out: set[int] = set()
    for raw in raw_ids:
        if isinstance(raw, int) and raw in open_ids:
            out.add(raw)
    return out


def _is_exhausted_termination(
    job: Job,
    plan: Plan,
    *,
    tasks_done: int,
    max_tasks: int,
    stopped: bool,
    cap_hit: bool,
    time_cap_hit: bool,
    last_drain_productive_task_count: int | None,
) -> bool:
    """True when the loop ran out of useful work before any resource cap fired."""
    if stopped or cap_hit or time_cap_hit:
        return False
    if tasks_done >= max_tasks:
        return False
    if last_drain_productive_task_count is None or last_drain_productive_task_count > 2:
        return False
    if _latest_inconclusive_subgoal_ids(job, plan):
        return False
    from research_agent.storage import coverage

    if coverage.has_coverage(job) and not coverage.is_coverage_complete(job):
        return False
    return plan.is_complete()


def _is_goal_complete(job: Job, plan: Plan) -> bool:
    """True only when narrative subgoals and required coverage units are closed."""
    if not plan.is_complete():
        return False
    from research_agent.storage import coverage

    return coverage.is_coverage_complete(job) and not _coverage_has_confirmed_gaps(job)


def _update_coverage_from_result(
    job: Job,
    task: dict[str, Any],
    result: dict[str, Any] | None,
) -> None:
    from research_agent.storage import coverage

    try:
        coverage.update_from_task_result(job, task, result)
    except Exception as exc:  # noqa: BLE001 — coverage must not break task persistence
        emit(
            job,
            "WARN",
            "loop",
            "warning",
            {"stage": "coverage_update", "task_id": task.get("id"), "error": str(exc)},
        )


def _mark_coverage_task_failed(job: Job, task: dict[str, Any], reason: str) -> None:
    from research_agent.storage import coverage

    try:
        coverage.mark_task_failed(job, task, reason)
    except Exception as exc:  # noqa: BLE001 — coverage must not mask task failure
        emit(
            job,
            "WARN",
            "loop",
            "warning",
            {"stage": "coverage_failed_update", "task_id": task.get("id"), "error": str(exc)},
        )


def _coverage_has_confirmed_gaps(job: Job) -> bool:
    from research_agent.storage import coverage

    return bool(coverage.list_units(job, {"confirmed_gap"}))


def _is_complete_with_confirmed_gaps(job: Job, plan: Plan) -> bool:
    if not plan.is_complete():
        return False
    from research_agent.storage import coverage

    return coverage.is_coverage_complete(job) and _coverage_has_confirmed_gaps(job)


def _candidate_chamber(value: Any) -> str:
    text = str(value or "").strip()
    upper = text.upper()
    if upper in {"H", "HOUSE", "US HOUSE", "U.S. HOUSE"}:
        return "House"
    if upper in {"S", "SENATE", "US SENATE", "U.S. SENATE"}:
        return "Senate"
    return text


def _candidate_artifact_row(item: dict[str, Any]) -> dict[str, Any] | None:
    extras = item.get("extras") if isinstance(item.get("extras"), dict) else {}
    source_kind = str(extras.get("source_kind") or item.get("source_kind") or "")
    source_type = str(extras.get("source_type") or "")
    if source_kind not in {"fec", "state_election"} and source_type not in {
        "fec-filed",
        "state-ballot-qualified",
    }:
        return None

    candidate_name = str(
        extras.get("candidate_name") or extras.get("name") or item.get("title") or ""
    ).strip()
    state = str(extras.get("state") or "").strip().upper()
    chamber = _candidate_chamber(
        extras.get("chamber") or extras.get("office_full") or extras.get("office")
    )
    source_url = str(extras.get("source_url") or item.get("url") or "").strip()
    if not candidate_name or not state or not chamber or not source_url:
        return None

    district = str(
        extras.get("district_or_seat")
        or extras.get("district")
        or extras.get("district_number")
        or ""
    ).strip()
    if not district and chamber == "Senate":
        district = "statewide"
    status = str(
        extras.get("candidate_status")
        or extras.get("status")
        or ("fec-filed" if source_type == "fec-filed" else "")
    ).strip()
    confidence = extras.get("confidence")
    if confidence in (None, ""):
        confidence = item.get("score")

    return {
        "state": state,
        "chamber": chamber,
        "district_or_seat": district,
        "candidate_name": candidate_name,
        "party": str(extras.get("party") or "").strip(),
        "candidate_status": status,
        "confidence": "" if confidence in (None, "") else str(confidence),
        "official_campaign_website": str(
            extras.get("official_campaign_website") or extras.get("website") or ""
        ).strip(),
        "source_url": source_url,
        "source_kind": source_kind,
        "source_retrieved_at": str(
            extras.get("source_retrieved_at") or extras.get("retrieval_timestamp") or ""
        ).strip(),
        "notes": str(extras.get("notes") or "").strip(),
    }


def _candidate_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("state") or "").strip().upper(),
        str(row.get("chamber") or "").strip().lower(),
        str(row.get("district_or_seat") or "").strip().lower(),
        re.sub(r"\s+", " ", str(row.get("candidate_name") or "").strip().lower()),
        str(row.get("source_url") or "").strip().lower(),
    )


def _candidate_config_columns(value: Any) -> list[str]:
    columns: list[str] = []
    if isinstance(value, str):
        raw_items: list[Any] = value.split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    for item in raw_items:
        column = str(item).strip()
        if column and column not in columns:
            columns.append(column)
    return columns


def _candidate_artifact_config(job: Job) -> dict[str, Any]:
    intake = job.intake if isinstance(job.intake, dict) else {}
    raw_enrichment = intake.get("enrichment")
    enrichment = raw_enrichment if isinstance(raw_enrichment, dict) else {}
    artifact_name = str(
        enrichment.get("artifact") or intake.get("input_csv_artifact") or "candidates"
    ).strip()
    return {
        "artifact": artifact_name or "candidates",
        "key_columns": _candidate_config_columns(enrichment.get("key_columns")),
        "target_columns": _candidate_config_columns(enrichment.get("target_columns")),
        "overwrite_non_empty": bool(enrichment.get("overwrite_non_empty")),
    }


def _key_tuple_for_columns(
    row: dict[str, Any],
    key_columns: list[str],
) -> tuple[str, ...] | None:
    values = [str(row.get(column) or "").strip().lower() for column in key_columns]
    if not values or any(value == "" for value in values):
        return None
    return tuple(values)


def _schema_with_row_columns(
    schema: Any,
    rows: list[dict[str, Any]],
) -> Any:
    from research_agent.storage.artifacts import ArtifactColumn

    existing = [column.name for column in schema.columns]
    discovered: list[str] = []
    for row in rows:
        for column in row:
            if column not in existing and column not in discovered:
                discovered.append(column)
    if not discovered:
        return schema
    return schema.model_copy(
        update={
            "schema_version": schema.schema_version + 1,
            "columns": [
                *schema.columns,
                *[ArtifactColumn(name=column, required=False) for column in discovered],
            ],
        }
    )


def _enrichment_only_meta(meta: dict[str, Any]) -> dict[str, Any]:
    artifact_owned_keys = {
        "artifact_name",
        "schema_version",
        "row_count",
        "generated_at_epoch",
        "source_job_id",
        "source_coverage",
    }
    return {key: value for key, value in meta.items() if key not in artifact_owned_keys}


def _candidate_rows_to_append(
    existing_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    key_columns: list[str],
) -> list[dict[str, Any]]:
    matched_keys = {
        key
        for row in existing_rows
        for key in [_key_tuple_for_columns(row, key_columns)]
        if key is not None
    }
    seen_candidate_keys = {_candidate_key(row) for row in existing_rows}
    new_rows: list[dict[str, Any]] = []
    for row in candidate_rows:
        configured_key = _key_tuple_for_columns(row, key_columns)
        if configured_key is not None and configured_key in matched_keys:
            continue
        candidate_key = _candidate_key(row)
        if candidate_key in seen_candidate_keys:
            continue
        seen_candidate_keys.add(candidate_key)
        if configured_key is not None:
            matched_keys.add(configured_key)
        new_rows.append(row)
    return new_rows


def _update_candidate_artifact_from_result(
    job: Job,
    result: dict[str, Any] | None,
) -> None:
    if not isinstance(result, dict):
        return
    rows = result.get("results")
    if not isinstance(rows, list):
        return
    candidate_rows = [
        row
        for item in rows
        if isinstance(item, dict)
        for row in [_candidate_artifact_row(item)]
        if row is not None
    ]
    if not candidate_rows:
        return

    from research_agent.storage import artifacts, enrichment

    config = _candidate_artifact_config(job)
    artifact_name = str(config["artifact"])
    key_columns = list(config["key_columns"])
    target_columns = list(config["target_columns"])
    try:
        schema, existing_rows = artifacts.read_artifact(job, artifact_name)
    except FileNotFoundError:
        schema = artifacts.CANDIDATE_ROSTER_SCHEMA.model_copy(
            update={"name": artifact_name}
        )
        existing_rows = []
        enrichment_meta: dict[str, Any] = {}
    else:
        enrichment_meta = enrichment._read_meta(job, artifact_name)

    if key_columns and existing_rows:
        enrichment.enrich_artifact(
            job,
            artifact_name,
            updates=candidate_rows,
            key_columns=key_columns,
            target_columns=target_columns or None,
            overwrite_non_empty=bool(config["overwrite_non_empty"]),
        )
        schema, existing_rows = artifacts.read_artifact(job, artifact_name)
        enrichment_meta = enrichment._read_meta(job, artifact_name)

    new_rows = _candidate_rows_to_append(existing_rows, candidate_rows, key_columns)
    if not new_rows:
        return

    merged = [*existing_rows, *new_rows]
    artifacts.write_table_artifact(
        job,
        artifact_name,
        schema=_schema_with_row_columns(schema, merged),
        rows=merged,
        source_coverage=f"{len(merged)} candidate rows from connector results",
    )
    preserved_meta = _enrichment_only_meta(enrichment_meta)
    if preserved_meta:
        enrichment._write_meta(job, artifact_name, preserved_meta)


def _is_zero_result_search(kind: str, result: dict[str, Any] | None) -> bool | None:
    if not kind.endswith("_search") or not isinstance(result, dict):
        return None
    results = result.get("results")
    if not isinstance(results, list):
        return None
    return len(results) == 0


def _suggested_unblocker_for(kind: str, query_stem: str) -> str | None:
    from research_agent.prompts.loader import load_data_source_followups

    recipes = load_data_source_followups().get(kind) or []
    if not recipes:
        return None
    query_words = set(query_stem.split())
    best: dict[str, Any] | None = None
    best_score = -1
    for recipe in recipes:
        family = recipe.get("if_zero_for_query_family")
        family_words = set(re.findall(r"[a-z0-9]+", str(family or "").lower()))
        score = len(query_words & family_words)
        if score > best_score:
            best = recipe
            best_score = score
    suggestion = best.get("suggest") if best is not None else recipes[0].get("suggest")
    return suggestion if isinstance(suggestion, str) and suggestion.strip() else None


def _append_low_yield_unblocker(job: Job, payload: dict[str, Any]) -> None:
    """Persist low-yield hints for the later Confirmed Gaps synthesis pass."""
    path = job.root / "synthesis" / "low_yield.json"
    items: list[dict[str, Any]] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                items = [item for item in existing if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            items = []
    items.append(dict(payload))
    _atomic_write_text(path, json.dumps(items, indent=2, sort_keys=True) + "\n")


def _track_low_yield_connector(
    job: Job,
    task: dict[str, Any],
    result: dict[str, Any] | None,
    zero_result_streaks: dict[tuple[str, str], int],
) -> None:
    """Emit a low-yield event after three 0-result searches for a query family."""
    kind = str(task.get("kind") or "")
    zero = _is_zero_result_search(kind, result)
    if zero is None:
        return
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    query_stem = _query_stem(payload, max_words=3)
    key = (kind, query_stem)
    if not zero:
        zero_result_streaks.pop(key, None)
        return
    count = zero_result_streaks.get(key, 0) + 1
    zero_result_streaks[key] = count
    if count < 3:
        return

    suggested = _suggested_unblocker_for(kind, query_stem)
    event_payload = {
        "kind": kind,
        "query_stem": query_stem,
        "count": count,
        "suggested_unblocker": suggested,
    }
    emit(job, "WARN", "loop", "low_yield_connector", event_payload)
    _append_low_yield_unblocker(job, event_payload)
    zero_result_streaks.pop(key, None)


def _load_pending_follow_up_questions(
    recent_results: list[dict[str, Any]],
) -> list[str]:
    """Pull cornerstone-emitted ``follow_up_questions`` from recent task results.

    Issue #206: cornerstone-section extraction surfaces questions the
    document raises. The drain-replan calls this to feed them to
    :func:`tactical_replan` so the planner sees them as candidate
    sub-questions on the next iteration. Deduped by case-insensitive
    text so two sections asking the same question don't double-count.
    """
    seen: set[str] = set()
    out: list[str] = []
    for r in recent_results:
        result = r.get("result")
        if not isinstance(result, dict):
            continue
        questions = result.get("follow_up_questions")
        if not isinstance(questions, list):
            continue
        for q in questions:
            if not isinstance(q, str):
                continue
            cleaned = q.strip()
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(cleaned)
    return out


def _resolve_drain_cap(plan: Plan | None) -> int:
    """Return the drain-replan cap for ``plan``'s ``scope_class`` (issue #209).

    When no scope is set we read the live ``MAX_DRAIN_REPLANS`` symbol from
    this module so legacy callers and tests that monkeypatch the constant
    continue to drive the cap.
    """
    scope = plan.scope_class if plan is not None else None
    if scope:
        return _MAX_DRAIN_REPLANS_BY_SCOPE.get(scope, _MAX_DRAIN_REPLANS_FALLBACK)
    return MAX_DRAIN_REPLANS


def _resolve_finding_floor(plan: Plan | None) -> int:
    """Return the per-subgoal findings-floor for ``plan``'s ``scope_class``."""
    scope = plan.scope_class if plan is not None else None
    return _FINDING_FLOOR_BY_SCOPE.get(scope or "", _FINDING_FLOOR_FALLBACK)


def _under_served_subgoals(
    plan: Plan, total_findings: int, floor: int
) -> list[dict[str, Any]]:
    """Identify open subgoals that may justify extending the drain-replan cap.

    Findings aren't tagged by subgoal in the schema, so we estimate
    coverage as ``total_findings // open_subgoals`` — coarse but enough to
    distinguish "barely investigated" from "thoroughly answered". Every
    open subgoal is reported (an unclosed subgoal is by definition not
    materially answered) along with its estimated finding count and the
    floor threshold, so the cap-diagnostic event is self-explanatory.
    """
    open_subgoals = [sg for sg in plan.subgoals if not sg.done]
    if not open_subgoals:
        return []
    estimate = total_findings // max(len(open_subgoals), 1)
    return [
        {
            "id": sg.id,
            "description": sg.description,
            "finding_count": estimate,
            "floor": floor,
        }
        for sg in open_subgoals
    ]


def _unexercised_connectors(job: Job, plan: Plan, *, limit: int = 5) -> list[str]:
    """Return connector kinds the planner expected but never actually ran.

    Combines kinds named in ``plan.task_template`` with a small core set
    and diffs against the distinct ``kind`` of completed tasks for the
    job. Surfaces "we never tried EDGAR / FedRegister" before the cap hit.
    """
    template_kinds = {spec.kind for spec in plan.task_template}
    candidates = template_kinds | set(_CORE_CONNECTOR_KINDS)
    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT kind FROM tasks WHERE job_id = ? AND status = 'done'",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    exercised = {r["kind"] for r in rows}
    return sorted(k for k in candidates if k not in exercised)[:limit]


def _next_plausible_searches(plan: Plan, limit: int = 3) -> list[str]:
    """Pull up to ``limit`` next-search/fetch payload strings from the plan.

    Looks at ``plan.task_template`` (i.e. the planner's most recent output)
    and returns the first ``query``/``q``/``url`` it can find for each
    ``*_search``/``*_fetch`` task — what the planner was *about* to do
    when the cap hit, surfaced for the operator.
    """
    out: list[str] = []
    for spec in plan.task_template:
        kind = spec.kind
        if not (kind.endswith("_search") or kind.endswith("_fetch")):
            continue
        payload = spec.payload or {}
        for key in ("query", "q", "url"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                out.append(value.strip())
                break
        if len(out) >= limit:
            break
    return out


def _emit_cap_diagnostic(
    job: Job,
    plan: Plan,
    findings: list[dict[str, Any]],
    floor: int,
    *,
    cap: int,
) -> None:
    """Emit ``loop/cap_diagnostic`` so operators can see what wasn't done.

    Issue #209: when the drain-replan cap fires, a sparse final report
    leaves the operator guessing which subgoals were under-served and
    which connectors never ran. This event makes that explicit.
    """
    payload = {
        "scope_class": plan.scope_class,
        "cap": cap,
        "floor": floor,
        "total_findings": len(findings),
        "under_served_subgoals": _under_served_subgoals(plan, len(findings), floor),
        "unexercised_connectors": _unexercised_connectors(job, plan),
        "next_plausible_searches": _next_plausible_searches(plan),
    }
    emit(job, "WARN", "loop", "cap_diagnostic", payload)


async def _drain_replan(
    job: Job,
    plan: Plan,
    *,
    router: Any,
    drain_count: int,
) -> _DrainReplanOutcome:
    """Fire a tactical replan when the queue drains mid-run.

    Emits ``loop/drain_replan`` before calling the planner so operators can
    see when this fires. Loads all findings + the latest synthesis as
    context so the local-tier planner can pivot intelligently. Returns the
    outcome. Planner failures return no productive count; empty task templates
    return ``productive_task_count=0`` so the caller can distinguish clean
    exhaustion from a transient planner error.
    """
    from research_agent.orchestrator.plan import (
        _compute_prior_attempts_for_subgoal,
        tactical_replan,
    )

    emit(
        job,
        "INFO",
        "loop",
        "drain_replan",
        {"drain_count": drain_count, "from_plan_version": plan.version},
    )
    try:
        recent_results = _load_recent_task_results(job)
        findings = _load_all_findings(job)
        synth_md = _load_latest_synthesis_md(job)
        followups = _load_pending_follow_up_questions(recent_results)
        failed_signatures = _load_failed_task_signatures(job)
        prior_attempts = _compute_prior_attempts_for_subgoal(plan, _load_all_task_attempts(job))
        inconclusive_ids = _latest_inconclusive_subgoal_ids(job, plan)
        inconclusive_context = [
            item
            for sid, item in prior_attempts.items()
            if sid in inconclusive_ids or item.get("prior_task_kinds")
        ]
        replan_kwargs: dict[str, Any] = {}
        if inconclusive_context:
            replan_kwargs["inconclusive_subgoals"] = inconclusive_context
        new_plan = await tactical_replan(
            job,
            plan,
            recent_results,
            router=router,
            findings=findings,
            synthesis_md=synth_md,
            follow_up_questions=followups,
            **replan_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — drain replan failure must not kill the loop
        emit(
            job,
            "WARN",
            "loop",
            "warning",
            {"drain_replan_failed": True, "error": str(exc)},
        )
        return _DrainReplanOutcome(plan=None, productive_task_count=None, should_continue=False)

    productive_count = _productive_task_count(new_plan, failed_signatures=failed_signatures)
    if not new_plan.task_template:
        return _DrainReplanOutcome(
            plan=new_plan,
            productive_task_count=productive_count,
            should_continue=False,
        )
    return _DrainReplanOutcome(
        plan=new_plan,
        productive_task_count=productive_count,
        should_continue=True,
    )


def _consume_inbox_replan_request(job: Job) -> dict[str, Any] | None:
    path = job.root / INBOX_REPLAN_FILE
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        payload = {}
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return payload if isinstance(payload, dict) else {}


async def _inbox_replan(
    job: Job,
    plan: Plan,
    *,
    router: Any,
    request: dict[str, Any],
) -> Plan | None:
    from research_agent.orchestrator.plan import (
        _compute_prior_attempts_for_subgoal,
        tactical_replan,
    )

    recent_results = _load_recent_task_results(job)
    prior_attempts = _compute_prior_attempts_for_subgoal(plan, _load_all_task_attempts(job))
    inconclusive_context = [
        item for item in prior_attempts.values() if item.get("prior_task_kinds")
    ]
    kwargs: dict[str, Any] = {}
    if inconclusive_context:
        kwargs["inconclusive_subgoals"] = inconclusive_context
    note = request.get("note")
    if isinstance(note, str) and note.strip():
        kwargs["user_note"] = note.strip()
    emit(
        job,
        "INFO",
        "loop",
        "replan_triggered",
        {
            "stage": "inbox",
            "filename": request.get("filename"),
            "sha": request.get("sha"),
            "has_note": "user_note" in kwargs,
        },
    )
    try:
        return await tactical_replan(
            job,
            plan,
            recent_results,
            router=router,
            findings=_load_all_findings(job),
            synthesis_md=_load_latest_synthesis_md(job),
            follow_up_questions=_load_pending_follow_up_questions(recent_results),
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — inbox replan failure must not kill loop
        emit(
            job,
            "WARN",
            "loop",
            "warning",
            {"inbox_replan_failed": True, "error": str(exc)},
        )
        return None


def _make_wait_fn(wait_seq: tuple[int, ...]) -> Callable[[Any], int]:
    """Build a tenacity wait callable that walks ``wait_seq`` then clamps to its tail."""

    def _wait(retry_state: Any) -> int:
        idx = min(max(retry_state.attempt_number - 1, 0), len(wait_seq) - 1)
        return int(wait_seq[idx])

    return _wait


async def _run_with_retry(
    handler: Handler,
    job: Job,
    task: dict[str, Any],
    *,
    wait_seq: tuple[int, ...] = RETRY_WAITS,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
) -> dict[str, Any] | None:
    """Run ``handler`` with tenacity backoff on :class:`RetriableError`.

    On final exhaustion the underlying :class:`RetriableError` is re-raised
    (``reraise=True``), which the caller catches to mark the task ``failed``.
    """
    result: dict[str, Any] | None = None
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=_make_wait_fn(wait_seq),
        retry=retry_if_exception_type(RetriableError),
        reraise=True,
    ):
        with attempt:
            result = await handler(job, task)
    return result


def _enqueue_follow_ups(
    job: Job,
    follow_ups: list[Any],
    plan_version: int,
) -> list[int]:
    """Coerce a handler's ``follow_up_tasks`` into :class:`TaskSpec` rows + enqueue.

    Accepts either fully-formed ``TaskSpec`` instances or plain dicts that
    validate against the model — handlers can return whichever is more
    natural at their boundary.
    """
    coerced: list[TaskSpec] = []
    for item in follow_ups:
        if isinstance(item, TaskSpec):
            coerced.append(item)
        else:
            coerced.append(TaskSpec.model_validate(item))
    if not coerced:
        return []
    return enqueue(job, coerced, plan_version)


# ---------------------------------------------------------------------------
# Source persistence + finding extraction (replace _not_implemented stubs)
# ---------------------------------------------------------------------------


def _expand_search_to_fetches(
    job: Job,
    payload: dict[str, Any],
    results: list[Any],
) -> dict[str, Any]:
    """Turn a search-handler return into a result + ``follow_up_tasks`` dict.

    Bridges the static plan graph to live URLs. Used by web_search,
    reddit_search, and news_search — anything that returns
    :class:`SearchResult` rows. Each top-K hit becomes a ``web_fetch``
    follow-up carrying ``sub_question`` so the fetch handler's own
    ``extract_findings`` follow-up inherits context.

    ``payload`` knobs:
      * ``expand_top_k`` — cap follow-ups per search to keep the queue
        from exploding. If the planner sets it explicitly, that wins.
        Otherwise the default scales with the plan's ``scope_class``
        (narrow→3, medium→5, broad→7, comprehensive→10), falling back
        to 3 when no plan / no scope is available.
      * ``sub_question`` — overrides the auto-derived question.
      * ``query`` — used as the sub_question fallback if neither
        ``sub_question`` nor ``job.goal`` are set.
    """
    from research_agent.orchestrator.plan import TaskSpec

    if "expand_top_k" in payload:
        top_k = int(payload["expand_top_k"])
    else:
        plan = _load_latest_plan(job)
        scope = plan.scope_class if plan is not None else None
        top_k = _DEFAULT_TOP_K_BY_SCOPE.get(scope or "", _DEFAULT_TOP_K_FALLBACK)
    sub_question = payload.get("sub_question") or payload.get("query") or job.goal

    follow_ups: list[TaskSpec] = []
    for hit in results[:top_k]:
        url = getattr(hit, "url", None)
        if not url:
            continue
        hit_payload = {
            "url": url,
            "sub_question": sub_question,
            **_task_passthrough_payload(payload),
        }
        hit_payload.update(_language_hints_from_mapping(getattr(hit, "extras", None)))
        follow_ups.append(TaskSpec(kind="web_fetch", payload=hit_payload))

    return {
        "results": [r.model_dump(mode="json") for r in results],
        "follow_up_tasks": [t.model_dump() for t in follow_ups],
    }


def _persist_fetched_source(
    job: Job,
    source: Any,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the fetched ``Source`` to disk + the sources table.

    Returns a result dict the loop persists into ``tasks.result_json``:
    ``source_id`` is the rowid the new ``extract_findings`` handler uses
    to look the content back up. ``source`` is the serialized model so
    downstream consumers (UI, debugging, follow-up planners) don't have
    to re-query.

    Raises :class:`FatalError` when ``source is None`` (placeholder URL,
    blocked fetch, parse failure) so the loop marks the task ``failed``
    rather than silently storing ``source_id=None`` that downstream
    ``extract_findings`` tasks then trip on.

    Issue #193: when the persisted source's metadata carries a
    ``bill_text_url`` (set by ``congress._fetch_bill``), fan out a
    ``web_fetch`` follow-up for the actual bill body so the agent reads
    the substance of the legislation, not just the metadata index card.
    The follow-up is suppressed if the bill text URL has already been
    fetched for this job (deduped via the cross-job ``sources.url`` row).
    """
    if source is None:
        raise FatalError("web_fetch returned None — URL unreachable, blocked, or empty body")
    from research_agent.storage.sources import write_source

    # mode='json' so the dict we hand back to the loop has ISO-string
    # datetimes — mark_done's json.dumps would otherwise need default=str
    # to swallow them.
    source_dict = source.model_dump(mode="json")
    raw_content = source_dict.get("cleaned_text") or ""
    if not raw_content.strip():
        # 404 / empty body / boilerplate-only page — treat as a fetch miss
        # rather than letting write_source's ValueError leak past the loop's
        # FatalError handler and kill the daemon.
        raise FatalError(
            f"web_fetch produced empty content for {source_dict.get('url')!r}"
        )
    fetched_epoch: int | None
    if hasattr(source, "fetched_at") and source.fetched_at is not None:
        try:
            fetched_epoch = int(source.fetched_at.timestamp())
        except Exception:
            fetched_epoch = None
    else:
        fetched_epoch = None

    source_metadata = source_dict.get("metadata")
    if not isinstance(source_metadata, dict):
        source_metadata = {}
    else:
        source_metadata = dict(source_metadata)
    for key, value in _language_hints_from_mapping(payload).items():
        source_metadata.setdefault(key, value)

    source_id = write_source(
        job,
        url=source_dict.get("url"),
        title=source_dict.get("title"),
        raw_content=raw_content,
        kind=source_dict.get("source_kind"),
        archive_url=source_dict.get("archive_url"),
        fetched_at=fetched_epoch,
        metadata=source_metadata,
    )
    result: dict[str, Any] = {"source": source_dict, "source_id": source_id}

    bill_text_followup = _build_bill_text_followup(job, source, payload)
    if bill_text_followup is not None:
        result["follow_up_tasks"] = [bill_text_followup.model_dump()]

    return result


def _build_bill_text_followup(
    job: Job,
    source: Any,
    payload: dict[str, Any] | None,
) -> TaskSpec | None:
    """Emit a ``web_fetch`` follow-up for the bill text URL if appropriate.

    Returns ``None`` when the source has no ``bill_text_url`` metadata, when
    the URL has already been fetched for this job (anti-runaway guard against
    tactical_replan re-fetching the same bill), or when the metadata is
    malformed. The follow-up's ``sub_question`` inherits from the originating
    task payload so downstream ``extract_findings`` keeps research context.
    """
    metadata = getattr(source, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    bill_text_url = metadata.get("bill_text_url")
    if not isinstance(bill_text_url, str) or not bill_text_url.strip():
        return None
    bill_text_url = bill_text_url.strip()

    # Dedup against any prior fetch of this same URL within the job. Checking
    # ``sources`` (not ``job_sources``) is fine because cross-job dedup means
    # any prior write surfaces the existing row. We still scope by job through
    # the join so a sibling job's fetch doesn't suppress this one.
    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM sources s
            JOIN job_sources js ON js.source_id = s.id
            WHERE s.url = ? AND js.job_id = ?
            LIMIT 1
            """,
            (bill_text_url, job.id),
        ).fetchone()
    finally:
        conn.close()
    if row is not None:
        return None

    sub_question: str
    if payload and isinstance(payload.get("sub_question"), str) and payload["sub_question"].strip():
        sub_question = payload["sub_question"].strip()
    else:
        title = getattr(source, "title", None) or metadata.get("bill_text_format") or "this bill"
        sub_question = f"What does the bill text say about: {title}"

    return TaskSpec(
        kind="web_fetch",
        payload={"url": bill_text_url, "sub_question": sub_question},
    )


# ---------------------------------------------------------------------------
# Issue #194: second-order URL fan-out from extracted findings
# ---------------------------------------------------------------------------


_BLOCKLIST_PATH_REL = "config/url_blocklist.yaml"
_BLOCKLIST_CACHE: set[str] | None = None


def _load_url_blocklist() -> set[str]:
    """Read ``config/url_blocklist.yaml`` once; return host substrings.

    The blocklist filters second-order ``web_fetch`` follow-ups so the
    fan-out doesn't burn budget on social media surfaces, paywalled hosts,
    or archive.org (which has its own connector). Match is host-substring,
    case-insensitive, applied in :func:`_is_url_blocked`. Missing or
    malformed file → empty set; we'd rather lose blocklist enforcement
    than crash extraction.
    """
    global _BLOCKLIST_CACHE
    if _BLOCKLIST_CACHE is not None:
        return _BLOCKLIST_CACHE

    from pathlib import Path

    import yaml

    # Walk upward from this module until we find a directory carrying both
    # the blocklist and a ``pyproject.toml`` (the worktree root). The package
    # is installed editable, so ``__file__`` always lives inside the project.
    blocklist: set[str] = set()
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / _BLOCKLIST_PATH_REL
        if (parent / "pyproject.toml").exists():
            if candidate.exists():
                try:
                    data = yaml.safe_load(candidate.read_text(encoding="utf-8"))
                except (OSError, yaml.YAMLError):
                    data = None
                if isinstance(data, list):
                    for entry in data:
                        if isinstance(entry, str) and entry.strip():
                            blocklist.add(entry.strip().lower())
            break

    _BLOCKLIST_CACHE = blocklist
    return _BLOCKLIST_CACHE


def _normalize_url_for_fanout(url: str) -> str | None:
    """Strip trailing punctuation + fragment, lowercase host. Return canonical or None.

    Returns ``None`` for URLs that don't have an http/https scheme or that
    parse to nonsense — those should never be treated as fan-out candidates.
    Path/query are preserved as-is (no aggressive normalization that might
    alias two genuinely different resources).
    """
    if not isinstance(url, str) or not url:
        return None
    trimmed = url.strip().rstrip(".,;:!?)\"'>]}")
    if not trimmed:
        return None
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(trimmed)
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    host = parts.hostname or ""
    if not host:
        return None
    netloc = host.lower()
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def _is_url_blocked(url: str, blocklist: set[str]) -> bool:
    """Substring match against ``blocklist`` on the URL's hostname.

    ``blocklist`` entries are lowercase host fragments (``twitter.com``,
    ``web.archive.org``); we extract the URL's host once and check for
    substring containment so ``mobile.twitter.com`` and ``t.co`` redirects
    that resolve to the same host are both caught.
    """
    if not blocklist:
        return False
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    return any(entry in host for entry in blocklist)


def _count_second_order_emitted(job: Job) -> int:
    """Count tasks for ``job`` whose payload carries the second-order marker.

    Used to enforce the job-wide cap (:data:`_SECOND_ORDER_JOB_CAP`). We
    count tasks in any status — pending/running/done/failed all count
    against the budget so a long run that has already burned 200 fetches
    on dead URLs doesn't get to keep adding more.
    """
    json_path = f"$.{_SECOND_ORDER_PARENT_FINDING_ID_KEY}"
    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks"
            " WHERE job_id = ?"
            " AND json_extract(payload_json, ?) IS NOT NULL",
            (job.id, json_path),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return 0
    return int(row["n"])


def _existing_job_source_urls(job: Job) -> set[str]:
    """Return the normalized URLs of every source already linked to ``job``.

    Drives same-job dedup: if extraction emits a finding citing a URL that
    the agent has already fetched in this run, skip it. Cross-job dedup
    is intentionally out of scope (issue #194 § "Out of scope").
    """
    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT s.url AS url FROM sources s"
            " JOIN job_sources js ON js.source_id = s.id"
            " WHERE js.job_id = ? AND s.url IS NOT NULL",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    out: set[str] = set()
    for row in rows:
        norm = _normalize_url_for_fanout(row["url"])
        if norm is not None:
            out.add(norm)
    return out


def _extract_followup_urls(
    findings: list[dict[str, Any]],
    *,
    max_per_extract: int,
    confidence_threshold: float,
    exclude_urls: set[str],
    blocklist: set[str],
) -> list[tuple[dict[str, Any], str]]:
    """Pick high-confidence findings' cited URLs; return ``(finding, url)`` pairs.

    Sorted by parent confidence descending so when ``max_per_extract`` clips
    the list, the strongest signals survive. URLs are deduped within the
    call, normalized via :func:`_normalize_url_for_fanout`, blocklist-
    filtered via :func:`_is_url_blocked`, and any URL in ``exclude_urls``
    (typically the job's already-fetched source URLs) is dropped before
    the cap fires so we don't waste cap slots on dedups.
    """
    seen: set[str] = set()
    candidates: list[tuple[float, dict[str, Any], str]] = []
    for f in findings:
        try:
            confidence = float(f.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if confidence < confidence_threshold:
            continue
        text_parts = [str(f.get("claim") or ""), str(f.get("quote") or "")]
        text = " ".join(text_parts)
        for raw_url in _URL_REGEX.findall(text):
            normalized = _normalize_url_for_fanout(raw_url)
            if normalized is None:
                continue
            if normalized in seen:
                continue
            if normalized in exclude_urls:
                continue
            if _is_url_blocked(normalized, blocklist):
                continue
            seen.add(normalized)
            candidates.append((confidence, f, normalized))
    # Stable sort by descending confidence so per-extract cap retains the
    # strongest parent signals.
    candidates.sort(key=lambda c: -c[0])
    return [(f, u) for _, f, u in candidates[:max_per_extract]]


def _build_second_order_fanout(
    job: Job,
    findings: list[dict[str, Any]],
    payload: dict[str, Any] | None,
) -> list[TaskSpec]:
    """Build ``web_fetch`` follow-ups for URLs cited inside ``findings``.

    Precedence for the per-extract cap (mirrors :func:`_expand_search_to_fetches`):
    explicit ``payload['second_order_max']`` > plan ``scope_class`` > fallback.
    Returns an empty list when the cap is 0 (e.g. narrow scope), when the
    job-wide cap is already exceeded, or when nothing in ``findings`` cites
    a fan-out-eligible URL. Emits one ``second_order_fanout`` event per
    surfaced URL so operators can audit the fan-out volume.
    """
    payload = payload or {}
    if "second_order_max" in payload:
        try:
            max_per_extract = int(payload["second_order_max"])
        except (TypeError, ValueError):
            max_per_extract = _SECOND_ORDER_MAX_FALLBACK
    else:
        plan = _load_latest_plan(job)
        scope = plan.scope_class if plan is not None else None
        max_per_extract = _SECOND_ORDER_MAX_BY_SCOPE.get(
            scope or "", _SECOND_ORDER_MAX_FALLBACK
        )
    if max_per_extract <= 0 or not findings:
        return []

    threshold_raw = payload.get(
        "second_order_confidence_threshold", _SECOND_ORDER_CONFIDENCE_THRESHOLD
    )
    try:
        threshold = float(threshold_raw)
    except (TypeError, ValueError):
        threshold = _SECOND_ORDER_CONFIDENCE_THRESHOLD

    already_emitted = _count_second_order_emitted(job)
    remaining_job_budget = _SECOND_ORDER_JOB_CAP - already_emitted
    if remaining_job_budget <= 0:
        return []
    effective_cap = min(max_per_extract, remaining_job_budget)

    blocklist = _load_url_blocklist()
    exclude_urls = _existing_job_source_urls(job)

    selected = _extract_followup_urls(
        findings,
        max_per_extract=effective_cap,
        confidence_threshold=threshold,
        exclude_urls=exclude_urls,
        blocklist=blocklist,
    )
    if not selected:
        return []

    follow_ups: list[TaskSpec] = []
    for finding, url in selected:
        parent_finding_id = finding.get("finding_id")
        sub_question = (finding.get("claim") or "").strip() or job.goal
        emit(
            job,
            "INFO",
            "loop",
            "second_order_fanout",
            {
                "parent_finding_id": parent_finding_id,
                "parent_confidence": float(finding.get("confidence", 0.0)),
                "url": url,
                "sub_question": sub_question,
            },
        )
        follow_ups.append(
            TaskSpec(
                kind="web_fetch",
                payload={
                    "url": url,
                    "sub_question": sub_question,
                    _SECOND_ORDER_PARENT_FINDING_ID_KEY: parent_finding_id,
                },
            )
        )
    return follow_ups


def _load_source_text(job: Job, source_id: int) -> tuple[str, dict[str, Any]] | None:
    """Read ``sources/<sha>.md`` for ``source_id``; return ``(text, meta)`` or None.

    ``meta`` carries ``url``/``title``/``archive_url`` so the LLM prompt can
    cite without an extra query. Returns ``None`` if the row is missing or
    its on-disk file was pruned by the disk-cap watcher.
    """
    conn = db.connect(job.db_path)
    try:
        row = conn.execute(
            "SELECT id, url, title, md_path, archive_url, kind FROM sources WHERE id = ?",
            (source_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row["md_path"]:
        return None
    md_path = job.root / row["md_path"]
    if not md_path.exists():
        return None
    text = md_path.read_text(encoding="utf-8")
    sidecar_metadata: dict[str, Any] = {}
    json_path = md_path.with_suffix(".json")
    if json_path.exists():
        import json as _json

        try:
            sidecar = _json.loads(json_path.read_text(encoding="utf-8"))
        except (_json.JSONDecodeError, OSError):
            sidecar = {}
        if isinstance(sidecar, dict) and isinstance(sidecar.get("metadata"), dict):
            sidecar_metadata = dict(sidecar["metadata"])
    meta = {
        "id": int(row["id"]),
        "url": row["url"],
        "title": row["title"],
        "archive_url": row["archive_url"],
        "source_kind": row["kind"],
        "metadata": sidecar_metadata,
    }
    for key, value in sidecar_metadata.items():
        meta.setdefault(key, value)
    return text, meta


_EXTRACT_TEXT_LIMIT = 20000  # ~5k tokens; well under any tier's window
_FINDINGS_PER_SOURCE_LIMIT = 8

# Issue #177: cornerstone documents (the document the goal is anchored on —
# the Mandate for Leadership PDF, a 10-K, a court opinion) carry the spine
# of the investigation. Article-sized caps starve the rest of the run, so
# the cornerstone path uses a much larger text window and a much higher
# findings ceiling. The ceiling exists only to bound truly pathological
# model output, not to constrain a real indexing pass.
_CORNERSTONE_EXTRACT_TEXT_LIMIT = 80000  # ~20k tokens; still inside frontier windows
_CORNERSTONE_FINDINGS_PER_SOURCE_LIMIT = 500
# Documents that nobody marked as cornerstone but whose body is bigger than
# this still get the cornerstone treatment — the planner's marker is the
# primary signal, this is a fallback. Issue #189 gates the fallback to
# ``source_kind == "pdf"`` so long-form HTML (Wikipedia, archive transcripts)
# stays on the regular extraction path and the #177 report-padding
# regression doesn't come back. See ``_is_cornerstone_source``.
_CORNERSTONE_FALLBACK_MIN_CHARS = 200_000
_TRANSLATION_TIER = "frontier_speed"
_TRANSLATION_TARGET_LANG = "en"
_LANGUAGE_ALIASES: dict[str, str] = {
    "ar": "ar",
    "ara": "ar",
    "arabic": "ar",
    "de": "de",
    "deu": "de",
    "ger": "de",
    "german": "de",
    "en": "en",
    "eng": "en",
    "english": "en",
    "es": "es",
    "spa": "es",
    "esp": "es",
    "spanish": "es",
    "espanol": "es",
    "castellano": "es",
    "fr": "fr",
    "fra": "fr",
    "fre": "fr",
    "french": "fr",
    "francais": "fr",
    "it": "it",
    "ita": "it",
    "italian": "it",
    "ja": "ja",
    "jpn": "ja",
    "japanese": "ja",
    "nl": "nl",
    "nld": "nl",
    "dut": "nl",
    "dutch": "nl",
    "pt": "pt",
    "por": "pt",
    "portuguese": "pt",
    "ru": "ru",
    "rus": "ru",
    "russian": "ru",
    "zh": "zh",
    "chi": "zh",
    "zho": "zh",
    "chinese": "zh",
}
_SOURCE_KIND_LANGUAGE_DEFAULTS: dict[str, str] = {
    "bne_search": "es",
    "gallica_search": "fr",
    "persee_search": "fr",
}


def _truncate_for_prompt(text: str, limit: int = _EXTRACT_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[…truncated]"


def _coerce_optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
    return None


def _translation_enabled(job: Job, payload: dict[str, Any]) -> bool:
    """Return True when this extract opted into non-English translation."""
    if _TRANSLATE_NON_ENGLISH_KEY in payload:
        explicit = _coerce_optional_bool(payload.get(_TRANSLATE_NON_ENGLISH_KEY))
        if explicit is not None:
            return explicit
    intake = job.intake or {}
    configured = _coerce_optional_bool(intake.get(_TRANSLATE_NON_ENGLISH_KEY))
    if configured is not None:
        return configured
    nested = intake.get("config")
    if isinstance(nested, dict):
        configured = _coerce_optional_bool(nested.get(_TRANSLATE_NON_ENGLISH_KEY))
        if configured is not None:
            return configured
    return False


def _normalize_language(value: Any) -> str | None:
    """Normalize common ISO-639 and human language labels to ISO-639-1."""
    if isinstance(value, (list, tuple)):
        for item in value:
            normalized = _normalize_language(item)
            if normalized:
                return normalized
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for raw_part in re.split(r"[;,|/]+", text):
        part = raw_part.strip().casefold()
        if not part:
            continue
        part = part.split("_", 1)[0].split("-", 1)[0].strip()
        alias = _LANGUAGE_ALIASES.get(part)
        if alias:
            return alias
        if re.fullmatch(r"[a-z]{2}", part):
            return part
        if re.fullmatch(r"[a-z]{3}", part):
            return _LANGUAGE_ALIASES.get(part)
    return None


def _source_language(meta: dict[str, Any]) -> str | None:
    for mapping in (meta, meta.get("metadata")):
        if not isinstance(mapping, dict):
            continue
        for key in _LANGUAGE_HINT_KEYS:
            normalized = _normalize_language(mapping.get(key))
            if normalized:
                return normalized
    kind = meta.get("source_kind")
    if isinstance(kind, str):
        return _SOURCE_KIND_LANGUAGE_DEFAULTS.get(kind)
    return None


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 4) + 1)


def _translation_usage(system_prompt: str, body: str) -> Any:
    from research_agent.llm.budgets import TokenUsage

    return TokenUsage(
        input_tokens=_estimate_tokens(system_prompt) + _estimate_tokens(body),
        output_tokens=_estimate_tokens(body),
    )


def _translation_would_exceed_budget(router: Any, system_prompt: str, body: str) -> bool:
    budget = getattr(router, "budget", None)
    would_exceed = getattr(budget, "would_exceed", None)
    if not callable(would_exceed):
        return False
    return bool(would_exceed(_TRANSLATION_TIER, _translation_usage(system_prompt, body)))


def _emit_translation_skipped_budget(
    job: Job,
    *,
    finding_id: int,
    source_lang: str,
    reason: str,
) -> None:
    emit(
        job,
        "INFO",
        "loop",
        "translation_skipped_budget",
        {
            "finding_id": finding_id,
            "source_lang": source_lang,
            "target_lang": _TRANSLATION_TARGET_LANG,
            "tier": _TRANSLATION_TIER,
            "reason": reason,
        },
    )


async def _translate_finding(
    job: Job,
    *,
    router: Any,
    finding: dict[str, Any],
    source_lang: str,
) -> tuple[str | None, bool]:
    from pydantic_ai import Agent

    from research_agent.llm.budgets import BudgetExceeded
    from research_agent.prompts.loader import load_prompt
    from research_agent.storage.markdown import write_finding_translation

    finding_id = finding.get("finding_id")
    claim = finding.get("claim")
    if not isinstance(finding_id, int) or not isinstance(claim, str) or not claim.strip():
        return None, False

    rendered = load_prompt(
        "translator",
        job=job,
        source_lang=source_lang,
        target_lang=_TRANSLATION_TARGET_LANG,
    )
    body = claim.strip()
    if _translation_would_exceed_budget(router, rendered, body):
        _emit_translation_skipped_budget(
            job,
            finding_id=finding_id,
            source_lang=source_lang,
            reason="estimate_would_exceed_cap",
        )
        return None, True

    agent = Agent(
        router.model_for(_TRANSLATION_TIER),
        output_type=str,
        system_prompt=rendered,
    )
    try:
        result = await router.call(
            _TRANSLATION_TIER,
            agent,
            f"Finding {finding_id:06d}:\n{body}",
        )
    except BudgetExceeded:
        _emit_translation_skipped_budget(
            job,
            finding_id=finding_id,
            source_lang=source_lang,
            reason="precheck_exceeded_cap",
        )
        return None, True

    translated = result.output if isinstance(result.output, str) else str(result.output)
    translated = translated.strip()
    if not translated:
        return None, False
    path = write_finding_translation(
        job,
        finding_id=finding_id,
        translated_body=translated,
        source_lang=source_lang,
        target_lang=_TRANSLATION_TARGET_LANG,
    )
    return str(path.relative_to(job.root)), False


async def _maybe_translate_findings(
    job: Job,
    *,
    router: Any,
    meta: dict[str, Any],
    payload: dict[str, Any],
    written_findings: list[dict[str, Any]],
) -> tuple[list[str], int]:
    if not written_findings or not _translation_enabled(job, payload):
        return [], 0
    source_lang = _source_language(meta)
    if not source_lang or source_lang == _TRANSLATION_TARGET_LANG:
        return [], 0

    paths: list[str] = []
    skipped_budget = 0
    for finding in written_findings:
        path, skipped = await _translate_finding(
            job,
            router=router,
            finding=finding,
            source_lang=source_lang,
        )
        if path is not None:
            paths.append(path)
        if skipped:
            skipped_budget += 1
    return paths, skipped_budget


def _normalize_url_for_compare(url: str | None) -> str | None:
    """Lowercase host + strip trailing slash so URL equality is forgiving.

    The planner emits ``cornerstone_url`` as a literal string; the source
    row stores whatever URL the fetch resolved. Casing of the host and a
    trailing slash on the path are the only differences worth tolerating —
    anything more involved (query reordering, scheme upgrade) would risk
    aliasing two genuinely different resources.
    """
    if not isinstance(url, str) or not url:
        return None
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = parts.hostname or ""
    netloc = host.lower()
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def _is_cornerstone_source(
    job: Job, meta: dict[str, Any], text: str
) -> tuple[bool, bool]:
    """Return ``(is_cornerstone, via_size_fallback)`` for this source.

    Primary signal: the latest persisted plan's ``cornerstone_url`` matches
    the source's URL (after light normalization).

    Fallback (issue #189): a source whose ``source_kind == "pdf"`` and whose
    body exceeds :data:`_CORNERSTONE_FALLBACK_MIN_CHARS` is treated as the
    cornerstone even when no planner marker matches. The PDF gate exists
    because long-form HTML (Wikipedia full-text articles, archive.org
    transcripts, scraped book chapters) can also exceed 200k chars without
    being investigation cornerstones, and routing them through the
    structured-index prompt with the 500-finding ceiling causes the exact
    report-padding regression #177 was meant to prevent. PDFs of this size
    are nearly always the kind of monograph/filing the cornerstone path is
    calibrated for, so the fallback only fires for them.

    The second return value is ``True`` only when the size-fallback path
    fires (i.e. the planner did not mark the source); the call site uses
    this to emit a ``cornerstone_fallback_triggered`` event so operators
    can see the fallback was responsible for the lifted cap.
    """
    plan = _load_latest_plan(job)
    if plan is not None and plan.cornerstone_url:
        norm_plan = _normalize_url_for_compare(plan.cornerstone_url)
        norm_src = _normalize_url_for_compare(meta.get("url"))
        if norm_plan and norm_src and norm_plan == norm_src:
            return True, False
    if (
        meta.get("source_kind") == "pdf"
        and isinstance(text, str)
        and len(text) >= _CORNERSTONE_FALLBACK_MIN_CHARS
    ):
        return True, True
    return False, False


_YAML_FENCE_RE = re.compile(r"```(?:yaml|yml)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


def _extract_yaml_block(raw: str) -> str:
    """Return the first fenced YAML block, or the whole string if none.

    Local models occasionally forget the fence, so we tolerate "the whole
    response is YAML" as a fallback rather than failing immediately.
    """
    match = _YAML_FENCE_RE.search(raw)
    if match:
        return match.group(1).strip()
    return raw.strip()


def _persist_raw_findings_yaml(
    job: Job,
    source_id: int,
    raw: str,
    *,
    suffix: str | None = None,
) -> str:
    """Write the researcher's raw YAML to ``findings/raw/<source_id>.yaml``.

    Saved before parse so a malformed extraction still leaves an artifact
    on disk for forensics + future learnings. ``suffix`` (issue #206)
    distinguishes per-section raw outputs of a cornerstone walk: e.g.
    ``findings/raw/000123-section-001.yaml`` so each LLM call's raw
    output stays separately auditable.
    """
    if suffix:
        rel = f"findings/raw/{source_id:06d}-{suffix}.yaml"
    else:
        rel = f"findings/raw/{source_id:06d}.yaml"
    from research_agent.storage.jobs import _atomic_write_text

    _atomic_write_text(job.root / rel, raw if raw.endswith("\n") else raw + "\n")
    return rel


async def _run_extract_findings(
    job: Job,
    task: dict[str, Any],
    *,
    router: Any,
) -> dict[str, Any]:
    """Read a source and ask the ``general`` tier for findings as YAML.

    Payload contract: ``{"source_id": int, "sub_question": str (optional)}``.
    The model emits a YAML list of ``{claim, confidence, quote, tags}``
    findings (or, for cornerstones, a mapping with ``findings`` and
    ``follow_up_questions``). We parse + validate + write each via
    :func:`write_finding`. Raw output is also persisted under
    ``findings/raw/`` for forensics.

    Cornerstone documents (issue #206) take a structural section-walk
    path: ``pdf.extract_sections`` slices the PDF by outline / heading
    regex, and the cornerstone-extract prompt fires once per section
    with breadcrumb context prepended. Findings are tagged with the
    section breadcrumb, sliding-window fallback sections are deduped
    by claim Jaccard ≥ 0.85, and the document is also chunk-and-
    embedded into a per-job vector index so the ``cornerstone_query``
    task can retrieve top-K chunks for a sub-question without
    re-fetching the PDF.
    """
    payload = task.get("payload") or {}
    source_id = payload.get("source_id")
    if not isinstance(source_id, int):
        raise FatalError("extract_findings: payload.source_id (int) is required")

    loaded = _load_source_text(job, source_id)
    if loaded is None:
        raise FatalError(f"extract_findings: source {source_id} not found or pruned")
    text, meta = loaded

    sub_question = payload.get("sub_question") or job.goal

    is_cornerstone, via_size_fallback = _is_cornerstone_source(job, meta, text)
    if is_cornerstone:
        if via_size_fallback:
            emit(
                job,
                "WARN",
                "loop",
                "cornerstone_fallback_triggered",
                {
                    "source_id": source_id,
                    "url": meta.get("url"),
                    "source_kind": meta.get("source_kind"),
                    "cleaned_chars": len(text),
                },
            )
        emit(
            job,
            "INFO",
            "loop",
            "cornerstone_extract",
            {
                "source_id": source_id,
                "url": meta.get("url"),
                "prompt": "researcher_cornerstone",
                "text_chars": len(text),
            },
        )
        return await _run_cornerstone_section_walk(
            job,
            source_id=source_id,
            meta=meta,
            text=text,
            sub_question=sub_question,
            payload=payload,
            router=router,
        )

    regular_result = await _run_single_extract(
        job,
        source_id=source_id,
        meta=meta,
        text=text,
        sub_question=sub_question,
        payload=payload,
        router=router,
        prompt_name="researcher",
        text_limit=_EXTRACT_TEXT_LIMIT,
        findings_limit=_FINDINGS_PER_SOURCE_LIMIT,
    )
    # ``_written_findings`` is an internal handoff for the cornerstone walk
    # and ``cornerstone_query`` aggregators; never persist it into the regular
    # extract task's ``result_json``.
    regular_result.pop("_written_findings", None)
    return regular_result


async def _run_single_extract(
    job: Job,
    *,
    source_id: int,
    meta: dict[str, Any],
    text: str,
    sub_question: str,
    payload: dict[str, Any],
    router: Any,
    prompt_name: str,
    text_limit: int,
    findings_limit: int,
    extra_tags: list[str] | None = None,
    breadcrumb: str | None = None,
    raw_path_suffix: str | None = None,
) -> dict[str, Any]:
    """Single-pass extract used by the regular path and per-section calls."""
    import yaml
    from pydantic_ai import Agent

    from research_agent.prompts.loader import load_prompt

    rendered = load_prompt(prompt_name, job=job, goal=job.goal)
    agent = Agent(router.model_for("general"), output_type=str, system_prompt=rendered)
    breadcrumb_line = (
        f"This section is from {breadcrumb}.\n\n" if breadcrumb else ""
    )
    context = (
        f"Sub-question: {sub_question}\n"
        f"Source URL: {meta.get('url')}\n"
        f"Source title: {meta.get('title')}\n\n"
        f"{breadcrumb_line}"
        f"Source content:\n{_truncate_for_prompt(text, text_limit)}"
    )
    result = await router.call("general", agent, context)
    raw = result.output if isinstance(result.output, str) else str(result.output)

    raw_path = _persist_raw_findings_yaml(
        job, source_id, raw, suffix=raw_path_suffix
    )

    yaml_text = _extract_yaml_block(raw)
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise FatalError(
            f"extract_findings: YAML parse failed for source {source_id} "
            f"({raw_path}): {exc}"
        ) from exc

    items, follow_up_questions = _normalize_findings_yaml(
        parsed, source_id=source_id, raw_path=raw_path
    )

    written, written_findings, skipped = _write_findings_batch(
        job,
        source_id=source_id,
        items=items,
        findings_limit=findings_limit,
        extra_tags=extra_tags,
    )
    translation_paths, translations_skipped_budget = await _maybe_translate_findings(
        job,
        router=router,
        meta=meta,
        payload=payload,
        written_findings=written_findings,
    )

    result_dict: dict[str, Any] = {
        "source_id": source_id,
        "findings_written": len(written),
        "finding_ids": written,
        "skipped": skipped,
        "raw_path": raw_path,
        "follow_up_questions": follow_up_questions,
    }
    if translation_paths:
        result_dict["translations_written"] = len(translation_paths)
        result_dict["translation_paths"] = translation_paths
    if translations_skipped_budget:
        result_dict["translations_skipped_budget"] = translations_skipped_budget

    follow_ups = _build_second_order_fanout(job, written_findings, payload)
    if follow_ups:
        result_dict["follow_up_tasks"] = [t.model_dump() for t in follow_ups]
    result_dict["_written_findings"] = written_findings
    return result_dict


_CORNERSTONE_DEDUP_THRESHOLD = 0.85


async def _run_cornerstone_section_walk(
    job: Job,
    *,
    source_id: int,
    meta: dict[str, Any],
    text: str,
    sub_question: str,
    payload: dict[str, Any],
    router: Any,
) -> dict[str, Any]:
    """Section-walk + per-job index for cornerstone documents (issue #206).

    1. Try ``pdf.extract_sections`` to slice the document by outline /
       heading regex / sliding window.
    2. Run the cornerstone-extract prompt once per section with a
       breadcrumb-prefixed context line. Per-section findings carry the
       breadcrumb as a tag.
    3. When sections are unstructured (sliding-window fallback), dedupe
       findings by Jaccard ≥ :data:`_CORNERSTONE_DEDUP_THRESHOLD`.
    4. After extraction, build a per-job vector index of the document so
       a future ``cornerstone_query`` can retrieve top-K chunks without
       re-fetching the PDF.
    """
    sections = await _try_extract_sections(job, meta, text)

    aggregated_written: list[int] = []
    aggregated_written_findings: list[dict[str, Any]] = []
    aggregated_skipped = 0
    aggregated_follow_ups: list[str] = []
    raw_paths: list[str] = []
    seen_claim_token_sets: list[set[str]] = []
    any_unstructured = False

    if not sections:
        # No section walk available — fall back to one whole-document call,
        # preserving the legacy single-pass cornerstone behavior.
        whole_result = await _run_single_extract(
            job,
            source_id=source_id,
            meta=meta,
            text=text,
            sub_question=sub_question,
            payload=payload,
            router=router,
            prompt_name="researcher_cornerstone",
            text_limit=_CORNERSTONE_EXTRACT_TEXT_LIMIT,
            findings_limit=_CORNERSTONE_FINDINGS_PER_SOURCE_LIMIT,
        )
        whole_result.pop("_written_findings", None)
        return whole_result

    for idx, section in enumerate(sections):
        breadcrumb = str(section.get("breadcrumb") or f"section {idx + 1}")
        section_text = str(section.get("text") or "")
        if not section_text.strip():
            continue
        structured = bool(section.get("structured", True))
        if not structured:
            any_unstructured = True
        section_result = await _run_single_extract(
            job,
            source_id=source_id,
            meta=meta,
            text=section_text,
            sub_question=sub_question,
            payload=payload,
            router=router,
            prompt_name="researcher_cornerstone",
            text_limit=_CORNERSTONE_EXTRACT_TEXT_LIMIT,
            findings_limit=_CORNERSTONE_FINDINGS_PER_SOURCE_LIMIT,
            extra_tags=[breadcrumb],
            breadcrumb=breadcrumb,
            raw_path_suffix=f"section-{idx + 1:03d}",
        )

        section_written = section_result.pop("_written_findings", [])
        emit(
            job,
            "INFO",
            "loop",
            "cornerstone_section_extract",
            {
                "source_id": source_id,
                "section_idx": idx,
                "breadcrumb": breadcrumb,
                "text_chars": len(section_text),
                "findings_written": int(section_result.get("findings_written") or 0),
                "structured": structured,
            },
        )
        aggregated_written.extend(section_result.get("finding_ids") or [])
        aggregated_written_findings.extend(section_written)
        aggregated_skipped += int(section_result.get("skipped") or 0)
        aggregated_follow_ups.extend(section_result.get("follow_up_questions") or [])
        raw_paths.append(str(section_result.get("raw_path") or ""))
        for f in section_written:
            seen_claim_token_sets.append(_tokenize_for_jaccard(f.get("claim", "")))

    if any_unstructured and aggregated_written_findings:
        before = len(aggregated_written_findings)
        kept_findings, kept_ids = _dedupe_findings_by_jaccard(
            aggregated_written_findings,
            threshold=_CORNERSTONE_DEDUP_THRESHOLD,
        )
        if len(kept_findings) != before:
            emit(
                job,
                "INFO",
                "loop",
                "cornerstone_dedup",
                {
                    "source_id": source_id,
                    "before": before,
                    "after": len(kept_findings),
                    "threshold": _CORNERSTONE_DEDUP_THRESHOLD,
                },
            )
        aggregated_written_findings = kept_findings
        aggregated_written = kept_ids

    if aggregated_follow_ups:
        emit(
            job,
            "INFO",
            "loop",
            "cornerstone_followups_emitted",
            {
                "source_id": source_id,
                "count": len(aggregated_follow_ups),
            },
        )

    result_dict: dict[str, Any] = {
        "source_id": source_id,
        "findings_written": len(aggregated_written),
        "finding_ids": aggregated_written,
        "skipped": aggregated_skipped,
        "raw_path": raw_paths[0] if raw_paths else None,
        "section_raw_paths": raw_paths,
        "follow_up_questions": aggregated_follow_ups,
    }

    follow_ups = _build_second_order_fanout(
        job, aggregated_written_findings, payload
    )
    if follow_ups:
        result_dict["follow_up_tasks"] = [t.model_dump() for t in follow_ups]

    # Build the per-job vector index. Failure here must not fail the
    # extraction task — a missing index just disables Stage-3 retrieval
    # for this document.
    try:
        await _build_cornerstone_index(job, source_id, sections, meta)
    except Exception as exc:  # noqa: BLE001
        emit(
            job,
            "WARN",
            "loop",
            "cornerstone_index_failed",
            {"source_id": source_id, "error": str(exc)},
        )

    return result_dict


async def _try_extract_sections(
    job: Job,
    meta: dict[str, Any],
    text: str,
) -> list[dict[str, object]]:
    """Try ``pdf.extract_sections`` for a cornerstone PDF; return [] otherwise.

    Section-walk only fires when the source was actually fetched as a
    PDF (``source_kind == 'pdf'``) and we have a URL we can re-fetch.
    HTML / markdown cornerstones (older test fixtures, web-fetched
    cornerstones) keep the legacy single-pass behavior so existing
    callers don't break — issue #206 explicitly scopes the section-walk
    to PDF inputs.
    """
    if meta.get("source_kind") != "pdf":
        return []
    url = meta.get("url")
    if not isinstance(url, str) or not url:
        return []
    from research_agent.tools import pdf

    try:
        sections = await pdf.extract_sections(
            url,
            doc_title=meta.get("title") or url,
            job=job,
        )
    except Exception as exc:  # noqa: BLE001 — section walk is best-effort
        emit(
            job,
            "WARN",
            "loop",
            "cornerstone_section_walk_failed",
            {"url": url, "error": str(exc)},
        )
        return []
    return sections


async def _build_cornerstone_index(
    job: Job,
    parent_source_id: int,
    sections: list[dict[str, object]],
    meta: dict[str, Any],
) -> None:
    """Run :func:`local_corpus.index_cornerstone_source` in an executor.

    Embedding calls are blocking HTTP requests against LM Studio; running
    them inline would stall the loop. The orchestrator already ships a
    sync indexer for the corpus path, so we mirror it here.
    """
    if not sections:
        return
    from research_agent.tools import local_corpus

    def _index() -> dict[str, int]:
        return local_corpus.index_cornerstone_source(
            job,
            parent_source_id,
            sections,
            parent_url=meta.get("url"),
            parent_title=meta.get("title"),
        )

    summary = await asyncio.get_running_loop().run_in_executor(None, _index)
    emit(
        job,
        "INFO",
        "loop",
        "cornerstone_index_built",
        {
            "source_id": parent_source_id,
            "chunks_indexed": summary.get("chunks_indexed", 0),
            "chunks_skipped": summary.get("chunks_skipped", 0),
            "embed_dim": summary.get("embed_dim", 0),
            "section_count": len(sections),
        },
    )


def _normalize_findings_yaml(
    parsed: Any,
    *,
    source_id: int,
    raw_path: str,
) -> tuple[list[Any], list[str]]:
    """Coerce parser output to ``(items, follow_up_questions)``.

    Accepts either the legacy list-root form (just findings) or the new
    mapping form ``{findings: [...], follow_up_questions: [...]}``
    introduced for the cornerstone-section pass (issue #206).
    """
    if parsed is None:
        return [], []
    if isinstance(parsed, list):
        return parsed, []
    if isinstance(parsed, dict):
        items = parsed.get("findings") or []
        if not isinstance(items, list):
            raise FatalError(
                f"extract_findings: 'findings' must be a list (source {source_id}, "
                f"{raw_path}); got {type(items).__name__}"
            )
        questions_raw = parsed.get("follow_up_questions") or []
        questions: list[str] = []
        if isinstance(questions_raw, list):
            for q in questions_raw:
                if isinstance(q, str) and q.strip():
                    questions.append(q.strip())
        return items, questions
    raise FatalError(
        f"extract_findings: YAML root must be a list or mapping (source {source_id}, "
        f"{raw_path}); got {type(parsed).__name__}"
    )


def _write_findings_batch(
    job: Job,
    *,
    source_id: int,
    items: list[Any],
    findings_limit: int,
    extra_tags: list[str] | None = None,
) -> tuple[list[int], list[dict[str, Any]], int]:
    """Write up to ``findings_limit`` findings; return ``(ids, mirrors, skipped)``.

    ``extra_tags`` are prepended to each finding's ``tags`` list so the
    section breadcrumb is preserved on the persisted row — downstream
    ``tactical_replan`` reads tags to build per-proposal sub-questions.
    """
    from research_agent.orchestrator.synth import normalize_fragment_tags
    from research_agent.storage.markdown import write_finding

    written: list[int] = []
    written_findings: list[dict[str, Any]] = []
    skipped = 0
    for item in items[:findings_limit]:
        if not isinstance(item, dict):
            skipped += 1
            continue
        claim_raw = item.get("claim")
        conf_raw = item.get("confidence")
        if not isinstance(claim_raw, str) or not claim_raw.strip():
            skipped += 1
            continue
        try:
            conf = float(conf_raw)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if conf < 0.0 or conf > 1.0:
            skipped += 1
            continue
        tags_raw = item.get("tags") or []
        tags_list = [str(t) for t in tags_raw if isinstance(t, (str, int))]
        if extra_tags:
            tags_list = [*extra_tags, *tags_list]
        tags_final = tags_list or None
        target_fragments = normalize_fragment_tags(item.get("fragments") or [], job=job)
        quote_raw = item.get("quote")
        quote_str = quote_raw.strip() if isinstance(quote_raw, str) else ""
        try:
            fid = write_finding(
                job,
                claim=claim_raw.strip(),
                confidence=conf,
                source_ids=[source_id],
                tags=tags_final,
                target_fragments=target_fragments,
            )
            written.append(fid)
            written_findings.append(
                {
                    "finding_id": fid,
                    "claim": claim_raw.strip(),
                    "confidence": conf,
                    "quote": quote_str,
                    "target_fragments": target_fragments,
                }
            )
        except (ValueError, TypeError):
            skipped += 1
            continue
    return written, written_findings, skipped


def _tokenize_for_jaccard(text: str) -> set[str]:
    """Lowercase + whitespace-split tokens for Jaccard dedupe."""
    if not isinstance(text, str):
        return set()
    return {t for t in text.lower().split() if t}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return inter / union


def _dedupe_findings_by_jaccard(
    findings: list[dict[str, Any]],
    *,
    threshold: float,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Drop near-duplicates from ``findings`` using whitespace-token Jaccard.

    Earlier findings beat later ones when they're near-duplicates — the
    sliding-window fallback emits sections in document order, so the
    first occurrence of a claim is the one we keep.
    """
    kept: list[dict[str, Any]] = []
    kept_ids: list[int] = []
    seen_token_sets: list[set[str]] = []
    for f in findings:
        tokens = _tokenize_for_jaccard(f.get("claim") or "")
        if not tokens:
            kept.append(f)
            fid = f.get("finding_id")
            if isinstance(fid, int):
                kept_ids.append(fid)
            seen_token_sets.append(tokens)
            continue
        is_dup = any(
            _jaccard(tokens, prior) >= threshold for prior in seen_token_sets
        )
        if is_dup:
            continue
        kept.append(f)
        fid = f.get("finding_id")
        if isinstance(fid, int):
            kept_ids.append(fid)
        seen_token_sets.append(tokens)
    return kept, kept_ids


async def _run_cornerstone_query(
    job: Job,
    task: dict[str, Any],
    *,
    router: Any,
) -> dict[str, Any]:
    """Retrieve cornerstone chunks for a sub-question + extract findings (issue #206).

    Payload contract: ``{"sub_question": str, "cornerstone_url": str (optional),
    "parent_source_id": int (optional), "top_k": int (default 8)}``.

    Resolves the parent source by ``parent_source_id`` directly when given,
    otherwise by URL match against the latest plan's ``cornerstone_url`` /
    payload's ``cornerstone_url``. Embeds the sub-question, ranks chunks
    by cosine, and feeds the top-K (with breadcrumb-tagged context) to
    the standard ``researcher`` prompt — *not* ``researcher_cornerstone``,
    because retrieval already focuses the prompt on a sub-question.
    """
    payload = task.get("payload") or {}
    sub_question = payload.get("sub_question") or job.goal
    if not isinstance(sub_question, str) or not sub_question.strip():
        raise FatalError("cornerstone_query: payload.sub_question (str) is required")

    parent_source_id = payload.get("parent_source_id")
    if not isinstance(parent_source_id, int):
        target_url = payload.get("cornerstone_url")
        if not isinstance(target_url, str) or not target_url:
            plan = _load_latest_plan(job)
            target_url = plan.cornerstone_url if plan is not None else None
        if not isinstance(target_url, str) or not target_url:
            raise FatalError(
                "cornerstone_query: parent_source_id or cornerstone_url is required"
            )
        parent_source_id = _resolve_cornerstone_parent_id(job, target_url)
        if parent_source_id is None:
            raise FatalError(
                f"cornerstone_query: no parent source found for url {target_url!r}"
            )

    top_k = int(payload.get("top_k") or 8)

    from research_agent.tools import local_corpus

    def _retrieve() -> list[dict[str, Any]]:
        return local_corpus.cornerstone_query(
            sub_question, job, parent_source_id, top_k=top_k
        )

    hits = await asyncio.get_running_loop().run_in_executor(None, _retrieve)
    emit(
        job,
        "INFO",
        "loop",
        "cornerstone_query_run",
        {
            "parent_source_id": parent_source_id,
            "sub_question": sub_question,
            "top_k": top_k,
            "hits": len(hits),
        },
    )
    if not hits:
        return {
            "parent_source_id": parent_source_id,
            "sub_question": sub_question,
            "hits": 0,
            "findings_written": 0,
            "finding_ids": [],
        }

    # Concatenate the retrieved chunks (each already breadcrumb-prefixed
    # by the indexer's contextual-retrieval format) and run a focused
    # researcher pass.
    parts: list[str] = []
    breadcrumbs: list[str] = []
    for hit in hits:
        md_path = hit.get("md_path")
        if not isinstance(md_path, str):
            continue
        chunk_path = job.root / md_path
        if not chunk_path.exists():
            continue
        body = chunk_path.read_text(encoding="utf-8")
        parts.append(body)
        title = hit.get("title")
        if isinstance(title, str):
            breadcrumbs.append(title)
    if not parts:
        return {
            "parent_source_id": parent_source_id,
            "sub_question": sub_question,
            "hits": len(hits),
            "findings_written": 0,
            "finding_ids": [],
        }
    combined = "\n\n---\n\n".join(parts)
    parent_loaded = _load_source_text(job, parent_source_id)
    parent_meta: dict[str, Any]
    if parent_loaded is None:
        parent_meta = {"id": parent_source_id, "url": None, "title": None}
    else:
        _, parent_meta = parent_loaded

    result = await _run_single_extract(
        job,
        source_id=parent_source_id,
        meta=parent_meta,
        text=combined,
        sub_question=sub_question,
        payload=payload,
        router=router,
        prompt_name="researcher",
        text_limit=_CORNERSTONE_EXTRACT_TEXT_LIMIT,
        findings_limit=_FINDINGS_PER_SOURCE_LIMIT,
        extra_tags=["cornerstone_query"],
        breadcrumb=" / ".join(breadcrumbs[:3]) if breadcrumbs else None,
        raw_path_suffix="cornerstone-query",
    )
    result.pop("_written_findings", None)
    result["parent_source_id"] = parent_source_id
    result["sub_question"] = sub_question
    result["hits"] = len(hits)
    return result


def _resolve_cornerstone_parent_id(job: Job, url: str) -> int | None:
    """Resolve a cornerstone URL to its parent source rowid for ``job``.

    Uses :func:`_normalize_url_for_compare` so the same forgiving match
    that drives ``_is_cornerstone_source`` resolves the retrieval target
    too. Excludes ``cornerstone_chunk`` rows, since the indexer copies
    the parent URL onto every chunk — without this filter, the resolver
    can return a chunk id and ``cornerstone_query`` would then filter
    chunks by that chunk id and find nothing.
    """
    target_norm = _normalize_url_for_compare(url)
    if target_norm is None:
        return None
    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT s.id, s.url FROM sources s"
            " JOIN job_sources js ON js.source_id = s.id"
            " WHERE js.job_id = ? AND s.url IS NOT NULL"
            " AND (s.kind IS NULL OR s.kind != 'cornerstone_chunk')",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        if _normalize_url_for_compare(row["url"]) == target_norm:
            return int(row["id"])
    return None


async def _run_summarize_source(
    job: Job,
    task: dict[str, Any],
    *,
    router: Any,
) -> dict[str, Any]:
    """Compress a long source into a paragraph using the ``general`` tier.

    Payload contract: ``{"source_id": int, "max_words": int (optional)}``.
    The summary is returned in ``result_json`` and not written as a finding —
    summaries are scaffolding for the next ``extract_findings`` pass, not
    the citable output the synthesizer reads.
    """
    from pydantic_ai import Agent

    payload = task.get("payload") or {}
    source_id = payload.get("source_id")
    if not isinstance(source_id, int):
        raise FatalError("summarize_source: payload.source_id (int) is required")

    loaded = _load_source_text(job, source_id)
    if loaded is None:
        raise FatalError(f"summarize_source: source {source_id} not found or pruned")
    text, meta = loaded

    max_words = int(payload.get("max_words") or 250)
    system = (
        "You compress a single source into a tight paragraph for downstream "
        "extraction. Stay factual; no editorializing."
    )
    agent = Agent(router.model_for("general"), output_type=str, system_prompt=system)
    context = (
        f"Goal: {job.goal}\n"
        f"Source URL: {meta.get('url')}\n"
        f"Source title: {meta.get('title')}\n"
        f"Compress the source below to at most {max_words} words.\n\n"
        f"{_truncate_for_prompt(text)}"
    )
    result = await router.call("general", agent, context)
    summary = str(result.output)
    return {"source_id": source_id, "summary": summary}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_loop(
    job: Job,
    router: Any,
    *,
    plan: Plan | None = None,
    handlers: dict[str, Handler] | None = None,
    max_tasks: int = MAX_TASKS_PER_JOB,
    time_cap_hours: float | None = None,
    retry_waits: tuple[int, ...] = RETRY_WAITS,
    retry_max_attempts: int = RETRY_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Drain the task queue for ``job`` until the plan is complete or a cap fires.

    Parameters mirror §6.1's pseudocode plus a few testing hooks: an explicit
    ``plan`` (otherwise the latest persisted plan is loaded), a ``handlers``
    registry override, and ``retry_waits``/``retry_max_attempts`` so tests
    can collapse the backoff to zero. Returns a small status dict so callers
    (CLI, future UI) can render the run's outcome without re-querying the DB.
    """
    handlers = handlers if handlers is not None else default_handlers(router)
    if plan is None:
        plan = _load_latest_plan(job)
    if plan is None:
        raise RuntimeError(
            f"run_loop: no plan persisted for job {job.id!r}; "
            "run initial_plan() before entering the loop"
        )

    start_ts = time.monotonic()
    time_cap_seconds = (
        float(time_cap_hours) * 3600 if time_cap_hours is not None and time_cap_hours > 0 else None
    )
    deadline_ts = start_ts + time_cap_seconds if time_cap_seconds is not None else None

    def _within_time_cap() -> bool:
        return deadline_ts is None or deadline_ts > time.monotonic()

    tasks_done = 0
    stopped = False
    cap_hit = False
    time_cap_hit = False
    drain_replans = 0
    last_drain_productive_task_count: int | None = None
    zero_result_streaks: dict[tuple[str, str], int] = {}

    checkpoint(
        job,
        "job_started",
        {"plan_version": plan.version, "objective": plan.objective},
    )

    while (
        not _should_stop(job)
        and not _is_goal_complete(job, plan)
        and not _is_complete_with_confirmed_gaps(job, plan)
        and tasks_done < max_tasks
        and _within_time_cap()
    ):
        inbox_request = _consume_inbox_replan_request(job)
        if inbox_request is not None:
            inbox_plan = await _inbox_replan(job, plan, router=router, request=inbox_request)
            if inbox_plan is not None:
                plan = inbox_plan
            continue

        task = next_pending(job)
        if task is None:
            cap = _resolve_drain_cap(plan)
            floor = _resolve_finding_floor(plan)
            if drain_replans >= cap:
                # Issue #209: before bailing, check whether subgoals are
                # materially answered. If open subgoals remain we allow up
                # to ``cap + floor`` more replans before giving up — a
                # broad/comprehensive run that names a 30-department
                # tracker shouldn't terminate just because the planner
                # hit a defensive cap, when half the goal is still open.
                findings = _load_all_findings(job)
                under_served = _under_served_subgoals(plan, len(findings), floor)
                if under_served and drain_replans < cap + floor:
                    emit(
                        job,
                        "INFO",
                        "loop",
                        "drain_replan_floor_extension",
                        {
                            "cap": cap,
                            "floor": floor,
                            "scope_class": plan.scope_class,
                            "drain_replans": drain_replans,
                            "extension": drain_replans - cap + 1,
                            "under_served_count": len(under_served),
                        },
                    )
                    # fall through into the replan call below
                else:
                    emit(
                        job,
                        "WARN",
                        "loop",
                        "warning",
                        {
                            "drain_replan_cap_hit": True,
                            "cap": cap,
                            "scope_class": plan.scope_class,
                            "drain_replans": drain_replans,
                        },
                    )
                    _emit_cap_diagnostic(job, plan, findings, floor, cap=cap)
                    break
            replan_outcome = await _drain_replan(
                job, plan, router=router, drain_count=drain_replans + 1
            )
            drain_replans += 1
            if replan_outcome.productive_task_count is not None:
                last_drain_productive_task_count = replan_outcome.productive_task_count
            if replan_outcome.plan is not None:
                plan = replan_outcome.plan
            if not replan_outcome.should_continue:
                break
            continue

        mark_running(task["id"], db_path=job.db_path)
        emit(
            job,
            "INFO",
            "loop",
            "task_pulled",
            {"task_id": task["id"], "kind": task["kind"]},
        )
        checkpoint(
            job,
            "task_pulled",
            {
                "task_id": task["id"],
                "kind": task["kind"],
                "plan_version": plan.version,
            },
        )

        handler = handlers.get(task["kind"])
        if handler is None:
            err = f"no handler registered for kind={task['kind']!r}"
            mark_failed(task["id"], err, db_path=job.db_path)
            _mark_coverage_task_failed(job, task, err)
            emit(
                job,
                "ERROR",
                "loop",
                "error",
                {"task_id": task["id"], "kind": task["kind"], "error": err},
            )
            tasks_done += 1
            continue

        try:
            result = await _run_with_retry(
                handler,
                job,
                task,
                wait_seq=retry_waits,
                max_attempts=retry_max_attempts,
            )
        except FatalError as exc:
            mark_failed(task["id"], str(exc), db_path=job.db_path)
            _mark_coverage_task_failed(job, task, str(exc))
            emit(
                job,
                "ERROR",
                "loop",
                "task_failed",
                {
                    "task_id": task["id"],
                    "kind": task["kind"],
                    "error": str(exc),
                    "fatal": True,
                },
            )
            tasks_done += 1
            continue
        except RetriableError as exc:
            mark_failed(task["id"], str(exc), db_path=job.db_path)
            _mark_coverage_task_failed(job, task, str(exc))
            emit(
                job,
                "ERROR",
                "loop",
                "task_failed",
                {
                    "task_id": task["id"],
                    "kind": task["kind"],
                    "error": str(exc),
                    "retries_exhausted": True,
                },
            )
            tasks_done += 1
            continue
        except Exception as exc:  # noqa: BLE001 — catch-all guard
            # Defensive: a handler raising an unexpected exception type (e.g.
            # ValueError from a downstream library) must NOT bubble up and
            # kill the daemon. Mark the single task failed and keep draining.
            mark_failed(task["id"], str(exc), db_path=job.db_path)
            _mark_coverage_task_failed(job, task, str(exc))
            emit(
                job,
                "ERROR",
                "loop",
                "task_failed",
                {
                    "task_id": task["id"],
                    "kind": task["kind"],
                    "error": str(exc),
                    "exception_type": type(exc).__name__,
                    "uncaught": True,
                },
            )
            tasks_done += 1
            continue

        follow_ups = (result or {}).get("follow_up_tasks") if isinstance(result, dict) else None
        # ``result`` is what gets persisted; strip the meta key so it isn't
        # double-stored (the queue rows for the follow-ups are the canonical
        # record of what was enqueued).
        persistable: dict[str, Any] | None
        if isinstance(result, dict) and "follow_up_tasks" in result:
            persistable = {k: v for k, v in result.items() if k != "follow_up_tasks"}
        else:
            persistable = result

        mark_done(task["id"], persistable, db_path=job.db_path)
        _update_coverage_from_result(job, task, persistable)
        try:
            _update_candidate_artifact_from_result(job, persistable)
        except Exception as exc:  # noqa: BLE001 — artifact updates must not break task draining
            emit(
                job,
                "WARN",
                "loop",
                "warning",
                {
                    "stage": "candidate_artifact_update",
                    "task_id": task.get("id"),
                    "error": str(exc),
                },
            )
        _track_low_yield_connector(job, task, persistable, zero_result_streaks)
        emit(
            job,
            "INFO",
            "loop",
            "task_done",
            {"task_id": task["id"], "kind": task["kind"]},
        )
        checkpoint(
            job,
            "task_done",
            {"task_id": task["id"], "kind": task["kind"]},
        )

        if follow_ups:
            _enqueue_follow_ups(job, list(follow_ups), task["plan_version"])

        tasks_done += 1

        if tasks_done % HEURISTIC_CHECK_EVERY_N == 0:
            await _maybe_run_heuristic(job, plan, handlers, tasks_done)
            # The synth/critique heuristics may have closed (or reopened)
            # subgoals on the persisted plan. Reload so plan.is_complete()
            # in the next loop guard sees the latest state and we exit
            # cleanly with completion_reason='goal_complete' instead of
            # running until task_cap or queue drain.
            refreshed = _load_latest_plan(job)
            if refreshed is not None:
                plan = refreshed

    if (
        deadline_ts is not None
        and not _should_stop(job)
        and not _is_goal_complete(job, plan)
        and not _within_time_cap()
    ):
        time_cap_hit = True
        emit(
            job,
            "WARN",
            "loop",
            "warning",
            {
                "time_cap_hit": True,
                "time_cap_hours": time_cap_hours,
                "tasks_done": tasks_done,
                "elapsed_seconds": time.monotonic() - start_ts,
            },
        )
        await _final_synthesis_best_effort(job, handlers)

    if tasks_done >= max_tasks and not _should_stop(job) and not time_cap_hit:
        cap_hit = True
        emit(
            job,
            "WARN",
            "loop",
            "warning",
            {"cap_hit": True, "max_tasks": max_tasks, "tasks_done": tasks_done},
        )
        await _final_synthesis_best_effort(job, handlers)

    if _should_stop(job):
        stopped = True
        checkpoint(job, "stop_requested", {"tasks_done": tasks_done})

    exhausted = _is_exhausted_termination(
        job,
        plan,
        tasks_done=tasks_done,
        max_tasks=max_tasks,
        stopped=stopped,
        cap_hit=cap_hit,
        time_cap_hit=time_cap_hit,
        last_drain_productive_task_count=last_drain_productive_task_count,
    )
    goal_complete = _is_goal_complete(job, plan)
    complete_with_confirmed_gaps = _is_complete_with_confirmed_gaps(job, plan)

    return {
        "tasks_done": tasks_done,
        "stopped": stopped,
        "completed": goal_complete,
        "cap_hit": cap_hit,
        "time_cap_hit": time_cap_hit,
        "completion_reason": (
            "time_cap"
            if time_cap_hit
            else "task_cap"
            if cap_hit
            else "confirmed_gap"
            if complete_with_confirmed_gaps
            else "exhausted"
            if exhausted
            else None
        ),
        "elapsed_seconds": time.monotonic() - start_ts,
        "drain_replans": drain_replans,
        "last_drain_productive_task_count": last_drain_productive_task_count,
        "exhausted": exhausted,
    }


async def _maybe_run_heuristic(
    job: Job,
    plan: Plan,
    handlers: dict[str, Handler],
    tasks_done: int,
) -> None:
    """Fire synthesize/critique handlers if the cadence heuristics agree.

    Failures here surface as ``warning`` events but never abort the loop —
    a flaky synth pass shouldn't kill a week-long run.
    """
    if _should_synthesize(plan, tasks_done):
        synth = handlers.get("synthesize")
        if synth is not None:
            try:
                await synth(job, {"kind": "synthesize", "id": -1, "payload": {}})
            except Exception as exc:  # noqa: BLE001 — heuristic is best-effort
                emit(
                    job,
                    "WARN",
                    "loop",
                    "warning",
                    _heuristic_failure_payload("synthesize", exc, synth),
                )
            else:
                checkpoint(
                    job,
                    "synthesis_done",
                    {"tasks_done": tasks_done, "plan_version": plan.version},
                )
    if _should_critique(plan, tasks_done):
        crit = handlers.get("critique")
        if crit is not None:
            try:
                await crit(job, {"kind": "critique", "id": -1, "payload": {}})
            except Exception as exc:  # noqa: BLE001 — heuristic is best-effort
                emit(
                    job,
                    "WARN",
                    "loop",
                    "warning",
                    _heuristic_failure_payload("critique", exc, crit),
                )
            else:
                checkpoint(
                    job,
                    "critique_done",
                    {"tasks_done": tasks_done, "plan_version": plan.version},
                )


async def _final_synthesis_best_effort(
    job: Job,
    handlers: dict[str, Handler],
) -> None:
    """On cap-hit, try a final synthesize pass so the user gets *something*."""
    synth = handlers.get("synthesize")
    if synth is None:
        return
    try:
        await synth(job, {"kind": "synthesize", "id": -1, "payload": {"final": True}})
    except Exception as exc:  # noqa: BLE001 — final synth is best-effort
        emit(
            job,
            "WARN",
            "loop",
            "warning",
            {"final_synthesis": "failed", "error": str(exc)},
        )


__all__ = [
    "HEURISTIC_CHECK_EVERY_N",
    "Handler",
    "MAX_DRAIN_REPLANS",
    "MAX_TASKS_PER_JOB",
    "RETRY_MAX_ATTEMPTS",
    "RETRY_WAITS",
    "TaskKind",
    "default_handlers",
    "run_loop",
]
