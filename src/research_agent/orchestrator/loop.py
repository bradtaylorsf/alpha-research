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
from collections.abc import Awaitable, Callable
from typing import Any

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
)

from research_agent.observability.events import emit
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
            engine=payload.get("engine", "ddg"),
        )
        return {"results": [r.model_dump() for r in results]}

    async def _web_fetch(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        from research_agent.tools import web_fetch

        payload = task["payload"]
        source = await web_fetch.fetch(payload["url"])
        return {"source": source.model_dump() if source is not None else None}

    async def _arxiv_search(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        from research_agent.tools import arxiv_tool

        payload = task["payload"]
        results = await arxiv_tool.search(
            payload.get("query", ""),
            max_results=payload.get("max_results", 10),
        )
        return {"results": [r.model_dump() for r in results]}

    async def _arxiv_fetch(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        from research_agent.tools import arxiv_tool

        payload = task["payload"]
        source = await arxiv_tool.fetch(payload["arxiv_id"])
        return {"source": source.model_dump() if source is not None else None}

    async def _news_search(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        from research_agent.tools import news

        payload = task["payload"]
        results = await news.search(payload.get("query", ""))
        return {"results": [r.model_dump() for r in results]}

    async def _reddit_search(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        from research_agent.tools import reddit

        payload = task["payload"]
        results = await reddit.search(payload.get("query", ""))
        return {"results": [r.model_dump() for r in results]}

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

    return {
        "web_search": _web_search,
        "web_fetch": _web_fetch,
        "arxiv_search": _arxiv_search,
        "arxiv_fetch": _arxiv_fetch,
        "news_search": _news_search,
        "reddit_search": _reddit_search,
        "local_corpus_query": _local_corpus_query,
        "github_search": _not_implemented_handler,
        "github_fetch": _not_implemented_handler,
        "extract_findings": _not_implemented_handler,
        "summarize_source": _not_implemented_handler,
        "synthesize": _synthesize,
        "critique": _not_implemented_handler,
    }


# ---------------------------------------------------------------------------
# Loop helpers
# ---------------------------------------------------------------------------


def _should_stop(job: Job) -> bool:
    """True when the operator dropped a ``STOP`` flag in the job folder."""
    return (job.root / "STOP").exists()


def _checkpoint_hook(job: Job, plan: Plan, task: dict[str, Any]) -> None:
    """Best-effort call into the (future) ``orchestrator.checkpoint`` module.

    The checkpoint module ships in issue #29; until then the import simply
    fails and the hook is a no-op so this loop can ship first.
    """
    try:
        from research_agent.orchestrator.checkpoint import (
            checkpoint,  # type: ignore[import-not-found]
        )
    except ImportError:
        return
    checkpoint(job, plan, task)


def _should_synthesize(plan: Plan, tasks_done: int) -> bool:
    """v1 synthesis heuristic: every ``HEURISTIC_CHECK_EVERY_N`` tasks.

    The "real" heuristic — only synthesize if there are unsummarized
    findings — lives with the synthesis module that lands later. The loop
    just provides the cadence.
    """
    return tasks_done > 0 and tasks_done % HEURISTIC_CHECK_EVERY_N == 0


def _should_critique(plan: Plan, tasks_done: int) -> bool:
    """v1 critique heuristic: every 4× the synthesis cadence (default: 100)."""
    return tasks_done > 0 and tasks_done % (HEURISTIC_CHECK_EVERY_N * 4) == 0


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

    while not _should_stop(job) and not plan.is_complete() and tasks_done < max_tasks:
        task = next_pending(job)
        if task is None:
            break

        mark_running(task["id"], db_path=job.db_path)
        emit(
            job,
            "INFO",
            "loop",
            "task_pulled",
            {"task_id": task["id"], "kind": task["kind"]},
        )
        _checkpoint_hook(job, plan, task)

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

        if follow_ups:
            _enqueue_follow_ups(job, list(follow_ups), task["plan_version"])

        tasks_done += 1

        if tasks_done % HEURISTIC_CHECK_EVERY_N == 0:
            await _maybe_run_heuristic(job, plan, handlers, tasks_done)

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

    return {
        "tasks_done": tasks_done,
        "stopped": stopped,
        "completed": plan.is_complete(),
        "cap_hit": cap_hit,
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
    "MAX_TASKS_PER_JOB",
    "RETRY_MAX_ATTEMPTS",
    "RETRY_WAITS",
    "TaskKind",
    "default_handlers",
    "run_loop",
]
