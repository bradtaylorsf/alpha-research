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
import logging
import re
from collections.abc import Awaitable, Callable
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
from research_agent.storage import db
from research_agent.storage.jobs import Job
from research_agent.storage.tasks import (
    enqueue,
    mark_done,
    mark_failed,
    mark_running,
    next_pending,
)

logger = logging.getLogger(__name__)

MAX_TASKS_PER_JOB = 10000
HEURISTIC_CHECK_EVERY_N = 25
RETRY_WAITS: tuple[int, ...] = (1, 2, 4, 8, 16, 30, 60)
RETRY_MAX_ATTEMPTS = 5
MAX_DRAIN_REPLANS = 10

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

Handler = Callable[[Job, dict[str, Any]], Awaitable[dict[str, Any] | None]]


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
        "state",
        "form_type",
        "since",
        "agencies",
        "jurisdiction",
        "award_type",
        "language",
    }
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


def _make_connector_search_handler(module_name: str) -> Handler:
    """Build a thin search-handler that dispatches to ``tools.<module_name>.search``.

    Converts the connector's missing-credential ``RuntimeError`` (raised by
    edgar/courtlistener/scholar/linkedin when their API key/UA isn't
    configured) into :class:`FatalError` so the loop marks the task failed
    cleanly down the documented path. Returns the standard search-result
    + ``follow_up_tasks`` shape so each top hit becomes a connector-aware
    ``web_fetch`` follow-up.
    """

    async def _handler(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        from importlib import import_module

        mod = import_module(f"research_agent.tools.{module_name}")
        payload = task["payload"]
        kwargs = {
            k: v for k, v in payload.items() if k in _CONNECTOR_SEARCH_PASSTHROUGH
        }
        kwargs = _filter_kwargs_for(mod.search, kwargs)
        try:
            results = await mod.search(payload.get("query", ""), **kwargs)
        except RuntimeError as exc:
            raise FatalError(f"{module_name}_search: {exc}") from exc
        return _expand_search_to_fetches(job, payload, results)

    return _handler


def _make_connector_fetch_handler(module_name: str) -> Handler:
    """Build a thin fetch-handler that dispatches to ``tools.<module_name>.fetch``.

    Mirrors :func:`_make_connector_search_handler` but for the single-URL
    fetch path: persists the returned ``Source`` and converts the
    connector's missing-credential ``RuntimeError`` to :class:`FatalError`.
    """

    async def _handler(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        from importlib import import_module

        mod = import_module(f"research_agent.tools.{module_name}")
        payload = task["payload"]
        try:
            source = await mod.fetch(payload["url"])
        except RuntimeError as exc:
            raise FatalError(f"{module_name}_fetch: {exc}") from exc
        return _persist_fetched_source(job, source)

    return _handler


_CONNECTOR_KINDS: tuple[str, ...] = (
    "congress",
    "fec",
    "edgar",
    "courtlistener",
    "fedregister",
    "lda",
    "usaspending",
    "gdelt",
    "littlesis",
    "nonprofits",
    "opencorporates",
    "sanctions",
    "bbb",
    "licensing",
    "sos",
    "calaccess",
    "scholar",
    "linkedin",
)


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
        result = _persist_fetched_source(job, source)

        source_id = result.get("source_id")
        if isinstance(source_id, int):
            sub_question = payload.get("sub_question") or job.goal
            follow_up = TaskSpec(
                kind="extract_findings",
                payload={"source_id": source_id, "sub_question": sub_question},
            )
            result["follow_up_tasks"] = [follow_up.model_dump()]
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

    async def _synthesize(job: Job, task: dict[str, Any]) -> dict[str, Any] | None:
        from research_agent.orchestrator.synth import final_synthesis, synthesize

        plan = _load_latest_plan(job)
        if plan is None:
            raise FatalError("synthesize: no plan persisted for job")
        payload = task.get("payload") or {}
        if payload.get("final"):
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
        "github_search": _not_implemented_handler,
        "github_fetch": _not_implemented_handler,
        "extract_findings": _extract_findings,
        "summarize_source": _summarize_source,
        "synthesize": _synthesize,
        "critique": _critique,
    }
    for name in _CONNECTOR_KINDS:
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
            "SELECT id, claim, confidence, source_ids, tags FROM findings"
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
        out.append(
            {
                "id": int(row["id"]),
                "claim": row["claim"],
                "confidence": float(row["confidence"]),
                "source_ids": source_ids,
                "tags": tags,
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


async def _drain_replan(
    job: Job,
    plan: Plan,
    *,
    router: Any,
    drain_count: int,
) -> Plan | None:
    """Fire a tactical replan when the queue drains mid-run.

    Emits ``loop/drain_replan`` before calling the planner so operators can
    see when this fires. Loads all findings + the latest synthesis as
    context so the local-tier planner can pivot intelligently. Returns the
    new plan, or ``None`` if the planner emitted no fresh tasks (treat as
    "goal exhausted") or raised — the caller breaks the loop in either case.
    """
    from research_agent.orchestrator.plan import tactical_replan

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
        new_plan = await tactical_replan(
            job,
            plan,
            recent_results,
            router=router,
            findings=findings,
            synthesis_md=synth_md,
        )
    except Exception as exc:  # noqa: BLE001 — drain replan failure must not kill the loop
        emit(
            job,
            "WARN",
            "loop",
            "warning",
            {"drain_replan_failed": True, "error": str(exc)},
        )
        return None

    if not new_plan.task_template:
        return None
    return new_plan


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

    follow_ups: list[TaskSpec] = [
        TaskSpec(
            kind="web_fetch",
            payload={"url": hit.url, "sub_question": sub_question},
        )
        for hit in results[:top_k]
        if getattr(hit, "url", None)
    ]

    return {
        "results": [r.model_dump(mode="json") for r in results],
        "follow_up_tasks": [t.model_dump() for t in follow_ups],
    }


def _persist_fetched_source(job: Job, source: Any) -> dict[str, Any]:
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

    source_id = write_source(
        job,
        url=source_dict.get("url"),
        title=source_dict.get("title"),
        raw_content=raw_content,
        kind=source_dict.get("source_kind"),
        archive_url=source_dict.get("archive_url"),
        fetched_at=fetched_epoch,
    )
    return {"source": source_dict, "source_id": source_id}


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
    meta = {
        "id": int(row["id"]),
        "url": row["url"],
        "title": row["title"],
        "archive_url": row["archive_url"],
        "source_kind": row["kind"],
    }
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
# primary signal, this is a fallback for runs whose plans pre-date #177.
_CORNERSTONE_FALLBACK_MIN_CHARS = 200_000


def _truncate_for_prompt(text: str, limit: int = _EXTRACT_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[…truncated]"


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
    job: Job, source_id: int, raw: str
) -> str:
    """Write the researcher's raw YAML to ``findings/raw/<source_id>.yaml``.

    Saved before parse so a malformed extraction still leaves an artifact
    on disk for forensics + future learnings.
    """
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
    findings, which we parse + validate + write via :func:`write_finding`.
    Raw output is also persisted under ``findings/raw/`` for forensics.
    Empty extractions are valid and return ``findings_written = 0``.
    """
    import yaml
    from pydantic_ai import Agent

    from research_agent.prompts.loader import load_prompt
    from research_agent.storage.markdown import write_finding

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
        prompt_name = "researcher_cornerstone"
        text_limit = _CORNERSTONE_EXTRACT_TEXT_LIMIT
        findings_limit = _CORNERSTONE_FINDINGS_PER_SOURCE_LIMIT
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
                "prompt": prompt_name,
                "text_chars": len(text),
            },
        )
    else:
        prompt_name = "researcher"
        text_limit = _EXTRACT_TEXT_LIMIT
        findings_limit = _FINDINGS_PER_SOURCE_LIMIT

    rendered = load_prompt(prompt_name, job=job, goal=job.goal)
    agent = Agent(router.model_for("general"), output_type=str, system_prompt=rendered)
    context = (
        f"Sub-question: {sub_question}\n"
        f"Source URL: {meta.get('url')}\n"
        f"Source title: {meta.get('title')}\n\n"
        f"Source content:\n{_truncate_for_prompt(text, text_limit)}"
    )
    result = await router.call("general", agent, context)
    raw = result.output if isinstance(result.output, str) else str(result.output)

    raw_path = _persist_raw_findings_yaml(job, source_id, raw)

    yaml_text = _extract_yaml_block(raw)
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise FatalError(
            f"extract_findings: YAML parse failed for source {source_id} "
            f"({raw_path}): {exc}"
        ) from exc

    if parsed is None:
        parsed = []
    if not isinstance(parsed, list):
        raise FatalError(
            f"extract_findings: YAML root must be a list (source {source_id}, "
            f"{raw_path}); got {type(parsed).__name__}"
        )

    written: list[int] = []
    skipped = 0
    for item in parsed[:findings_limit]:
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
        tags_list = [str(t) for t in tags_raw if isinstance(t, (str, int))] or None
        try:
            fid = write_finding(
                job,
                claim=claim_raw.strip(),
                confidence=conf,
                source_ids=[source_id],
                tags=tags_list,
            )
            written.append(fid)
        except (ValueError, TypeError):
            skipped += 1
            continue

    return {
        "source_id": source_id,
        "findings_written": len(written),
        "finding_ids": written,
        "skipped": skipped,
        "raw_path": raw_path,
    }


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

    tasks_done = 0
    stopped = False
    cap_hit = False
    drain_replans = 0

    checkpoint(
        job,
        "job_started",
        {"plan_version": plan.version, "objective": plan.objective},
    )

    while not _should_stop(job) and not plan.is_complete() and tasks_done < max_tasks:
        task = next_pending(job)
        if task is None:
            if drain_replans >= MAX_DRAIN_REPLANS:
                emit(
                    job,
                    "WARN",
                    "loop",
                    "warning",
                    {"drain_replan_cap_hit": True, "cap": MAX_DRAIN_REPLANS},
                )
                break
            new_plan = await _drain_replan(
                job, plan, router=router, drain_count=drain_replans + 1
            )
            drain_replans += 1
            if new_plan is None:
                break
            plan = new_plan
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

    if tasks_done >= max_tasks and not _should_stop(job):
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

    return {
        "tasks_done": tasks_done,
        "stopped": stopped,
        "completed": plan.is_complete(),
        "cap_hit": cap_hit,
        "drain_replans": drain_replans,
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
                    {"heuristic": "synthesize", "error": str(exc)},
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
                    {"heuristic": "critique", "error": str(exc)},
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
