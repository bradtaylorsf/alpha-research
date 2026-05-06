"""Tests for ``research_agent.orchestrator.loop``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from research_agent.orchestrator.errors import FatalError, RetriableError
from research_agent.orchestrator.loop import (
    HEURISTIC_CHECK_EVERY_N,
    MAX_TASKS_PER_JOB,
    RETRY_MAX_ATTEMPTS,
    Handler,
    default_handlers,
    run_loop,
)
from research_agent.orchestrator.plan import Plan, Subgoal, TaskSpec
from research_agent.storage import db
from research_agent.storage.jobs import Job
from research_agent.storage.markdown import write_plan
from research_agent.storage.tasks import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    enqueue,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.sqlite"
    db.migrate(path=path).close()
    return path


@pytest.fixture
def jobs_root(tmp_path: Path) -> Path:
    return tmp_path / "jobs"


@pytest.fixture
def job(jobs_root: Path, db_path: Path) -> Job:
    return Job.create(
        {"goal": "Investigate Widget Co"},
        jobs_root=jobs_root,
        db_path=db_path,
    )


@pytest.fixture
def plan(job: Job) -> Plan:
    """Persist a plan whose subgoals are all open so the loop runs to drain."""
    p = Plan(
        version=1,
        objective="Investigate the target",
        subgoals=[Subgoal(id=1, description="Gather", done=False)],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=3,
    )
    write_plan(job, p.model_dump())
    return p


def _seed_tasks(job: Job, kinds: list[str], plan_version: int = 1) -> list[int]:
    specs = [TaskSpec(kind=k, payload={"q": f"q-{i}"}) for i, k in enumerate(kinds)]  # type: ignore[arg-type]
    return enqueue(job, specs, plan_version=plan_version)


def _read_task_rows(db_path: Path, job_id: str) -> list[dict[str, Any]]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, kind, status, error, retry_count, result_json"
            " FROM tasks WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _read_event_kinds(db_path: Path, job_id: str) -> list[str]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT kind FROM events WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    return [r["kind"] for r in rows]


# ---------------------------------------------------------------------------
# Stub handler factory
# ---------------------------------------------------------------------------


def _ok_handler(result: dict[str, Any] | None = None) -> Handler:
    async def _h(job: Job, task: dict[str, Any]) -> dict[str, Any] | None:
        return result if result is not None else {"hits": 1}

    return _h


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_drains_five_task_plan(job: Job, db_path: Path, plan: Plan) -> None:
    """A 5-task plan should fully drain with all rows transitioning to ``done``."""
    kinds = ["web_search", "web_fetch", "arxiv_search", "news_search", "reddit_search"]
    ids = _seed_tasks(job, kinds)

    handlers: dict[str, Handler] = {k: _ok_handler() for k in kinds}

    result = await run_loop(job, router=None, plan=plan, handlers=handlers, retry_waits=(0,))

    assert result["tasks_done"] == 5
    assert result["stopped"] is False
    assert result["cap_hit"] is False

    rows = _read_task_rows(db_path, job.id)
    assert [r["id"] for r in rows] == ids
    assert all(r["status"] == STATUS_DONE for r in rows)

    events = _read_event_kinds(db_path, job.id)
    assert events.count("task_pulled") == 5
    assert events.count("task_done") == 5


@pytest.mark.asyncio
async def test_retriable_error_retries_then_succeeds(job: Job, db_path: Path, plan: Plan) -> None:
    """A handler that raises RetriableError twice must succeed on the third try."""
    _seed_tasks(job, ["web_search"])
    attempts = {"n": 0}

    async def flaky(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RetriableError("transient")
        return {"ok": True}

    result = await run_loop(
        job,
        router=None,
        plan=plan,
        handlers={"web_search": flaky},
        retry_waits=(0,),
    )

    assert attempts["n"] == 3
    assert result["tasks_done"] == 1
    rows = _read_task_rows(db_path, job.id)
    assert rows[0]["status"] == STATUS_DONE


@pytest.mark.asyncio
async def test_retriable_error_exhausted_marks_failed(job: Job, db_path: Path, plan: Plan) -> None:
    """If every retry hits RetriableError, the task ends ``failed`` and the loop continues."""
    _seed_tasks(job, ["web_search", "web_fetch"])
    attempts = {"n": 0}

    async def always_retriable(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        raise RetriableError("never works")

    async def ok(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    result = await run_loop(
        job,
        router=None,
        plan=plan,
        handlers={"web_search": always_retriable, "web_fetch": ok},
        retry_waits=(0,),
    )

    assert attempts["n"] == RETRY_MAX_ATTEMPTS
    assert result["tasks_done"] == 2

    rows = _read_task_rows(db_path, job.id)
    assert rows[0]["status"] == STATUS_FAILED
    assert rows[0]["error"] == "never works"
    assert rows[1]["status"] == STATUS_DONE

    events = _read_event_kinds(db_path, job.id)
    assert "task_failed" in events


@pytest.mark.asyncio
async def test_fatal_error_marks_failed_and_continues(job: Job, db_path: Path, plan: Plan) -> None:
    """A FatalError marks the task failed once (no retry) and the next task still runs."""
    _seed_tasks(job, ["web_search", "web_fetch"])
    attempts = {"n": 0}

    async def boom(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        raise FatalError("structural")

    async def ok(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    result = await run_loop(
        job,
        router=None,
        plan=plan,
        handlers={"web_search": boom, "web_fetch": ok},
        retry_waits=(0,),
    )

    # FatalError must NOT be retried — exactly one call to the boom handler.
    assert attempts["n"] == 1
    assert result["tasks_done"] == 2

    rows = _read_task_rows(db_path, job.id)
    assert rows[0]["status"] == STATUS_FAILED
    assert rows[0]["error"] == "structural"
    assert rows[1]["status"] == STATUS_DONE


@pytest.mark.asyncio
async def test_stop_flag_short_circuits(job: Job, db_path: Path, plan: Plan) -> None:
    """A STOP flag dropped mid-run exits the loop and leaves remaining tasks pending."""
    _seed_tasks(job, ["web_search", "web_fetch", "arxiv_search"])

    async def stop_after_first(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        # Drop the STOP flag inside the handler to simulate an external request.
        (job.root / "STOP").write_text("")
        return {"ok": True}

    handlers: dict[str, Handler] = {
        "web_search": stop_after_first,
        "web_fetch": _ok_handler(),
        "arxiv_search": _ok_handler(),
    }

    result = await run_loop(job, router=None, plan=plan, handlers=handlers, retry_waits=(0,))

    assert result["stopped"] is True
    assert result["tasks_done"] == 1

    rows = _read_task_rows(db_path, job.id)
    assert rows[0]["status"] == STATUS_DONE
    # Tasks 2 and 3 must remain pending — STOP is not failure.
    assert rows[1]["status"] == STATUS_PENDING
    assert rows[2]["status"] == STATUS_PENDING


@pytest.mark.asyncio
async def test_max_tasks_cap_triggers_final_synthesis(job: Job, db_path: Path, plan: Plan) -> None:
    """When ``max_tasks`` cap fires, the loop best-effort calls the ``synthesize`` handler."""
    _seed_tasks(job, ["web_search"] * 5)
    synth_calls = {"n": 0}

    async def synth(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        synth_calls["n"] += 1
        return {"ok": True}

    result = await run_loop(
        job,
        router=None,
        plan=plan,
        handlers={"web_search": _ok_handler(), "synthesize": synth},
        max_tasks=3,
        retry_waits=(0,),
    )

    assert result["tasks_done"] == 3
    assert result["cap_hit"] is True
    assert synth_calls["n"] == 1

    events = _read_event_kinds(db_path, job.id)
    assert "warning" in events  # cap_hit warning event

    rows = _read_task_rows(db_path, job.id)
    done_count = sum(1 for r in rows if r["status"] == STATUS_DONE)
    pending_count = sum(1 for r in rows if r["status"] == STATUS_PENDING)
    assert done_count == 3
    assert pending_count == 2


@pytest.mark.asyncio
async def test_follow_up_tasks_get_enqueued(job: Job, db_path: Path, plan: Plan) -> None:
    """A handler returning ``follow_up_tasks`` must enqueue + process them."""
    _seed_tasks(job, ["web_search"])

    fetch_calls = {"n": 0}

    async def web_search(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "results": ["a", "b"],
            "follow_up_tasks": [
                TaskSpec(kind="web_fetch", payload={"url": "https://a"}),
                TaskSpec(kind="web_fetch", payload={"url": "https://b"}),
            ],
        }

    async def web_fetch(job: Job, task: dict[str, Any]) -> dict[str, Any]:
        fetch_calls["n"] += 1
        return {"ok": True}

    result = await run_loop(
        job,
        router=None,
        plan=plan,
        handlers={"web_search": web_search, "web_fetch": web_fetch},
        retry_waits=(0,),
    )

    assert fetch_calls["n"] == 2
    assert result["tasks_done"] == 3

    rows = _read_task_rows(db_path, job.id)
    assert len(rows) == 3
    assert all(r["status"] == STATUS_DONE for r in rows)

    # The result_json for the parent task must NOT include the follow_up_tasks key
    # (they're persisted as queue rows, not as a meta-blob).
    import json as _json

    parent_result = _json.loads(rows[0]["result_json"])
    assert "follow_up_tasks" not in parent_result
    assert parent_result["results"] == ["a", "b"]


@pytest.mark.asyncio
async def test_unknown_kind_marks_failed(job: Job, db_path: Path, plan: Plan) -> None:
    """A task whose kind has no registered handler ends ``failed``; the loop continues."""
    _seed_tasks(job, ["web_search", "web_fetch"])

    handlers: dict[str, Handler] = {"web_fetch": _ok_handler()}  # web_search missing

    result = await run_loop(job, router=None, plan=plan, handlers=handlers, retry_waits=(0,))

    assert result["tasks_done"] == 2

    rows = _read_task_rows(db_path, job.id)
    assert rows[0]["status"] == STATUS_FAILED
    assert "no handler" in (rows[0]["error"] or "")
    assert rows[1]["status"] == STATUS_DONE

    events = _read_event_kinds(db_path, job.id)
    assert "error" in events


# ---------------------------------------------------------------------------
# Constants and registry sanity
# ---------------------------------------------------------------------------


def test_module_constants_match_spec() -> None:
    assert MAX_TASKS_PER_JOB == 10000
    assert HEURISTIC_CHECK_EVERY_N == 25
    assert RETRY_MAX_ATTEMPTS == 5


def test_default_handlers_covers_every_task_kind() -> None:
    handlers = default_handlers(router=None)
    expected = {
        "web_search",
        "web_fetch",
        "arxiv_search",
        "arxiv_fetch",
        "github_search",
        "github_fetch",
        "news_search",
        "reddit_search",
        "local_corpus_query",
        "extract_findings",
        "summarize_source",
        "synthesize",
        "critique",
    }
    assert set(handlers.keys()) == expected


@pytest.mark.asyncio
async def test_default_not_implemented_handler_raises_fatal(
    job: Job, db_path: Path, plan: Plan
) -> None:
    """The placeholder handlers raise :class:`FatalError`, marking the task failed."""
    _seed_tasks(job, ["github_search"])

    result = await run_loop(
        job,
        router=None,
        plan=plan,
        handlers=default_handlers(router=None),
        retry_waits=(0,),
    )

    assert result["tasks_done"] == 1
    rows = _read_task_rows(db_path, job.id)
    assert rows[0]["status"] == STATUS_FAILED
    assert "not implemented" in (rows[0]["error"] or "")


@pytest.mark.asyncio
async def test_run_loop_loads_plan_from_db_when_not_provided(
    job: Job, db_path: Path, plan: Plan
) -> None:
    """If ``plan`` is omitted, the loop loads the latest persisted plan via DB."""
    _seed_tasks(job, ["web_search"])
    handlers: dict[str, Handler] = {"web_search": _ok_handler()}

    result = await run_loop(job, router=None, handlers=handlers, retry_waits=(0,))
    assert result["tasks_done"] == 1


@pytest.mark.asyncio
async def test_run_loop_raises_when_no_plan_persisted(jobs_root: Path, db_path: Path) -> None:
    """A job with no plan and no plan kwarg surfaces a clear RuntimeError."""
    j = Job.create(
        {"goal": "no plan yet"},
        jobs_root=jobs_root,
        db_path=db_path,
    )
    with pytest.raises(RuntimeError, match="no plan persisted"):
        await run_loop(j, router=None, retry_waits=(0,))


@pytest.mark.asyncio
async def test_loop_exits_when_subgoals_all_done(job: Job, db_path: Path) -> None:
    """Synthesis closing every subgoal must end the loop with ``completed=True``.

    When the heuristic-driven synthesize call mutates the persisted plan via
    ``plan.update_subgoal_done`` and every subgoal lands done, the next loop
    guard sees ``plan.is_complete()`` and exits cleanly. The daemon then
    maps that to ``completion_reason='goal_complete'`` (see daemon.py:796).
    """
    from research_agent.orchestrator import plan as plan_module

    seeded = Plan(
        version=1,
        objective="Investigate the target",
        subgoals=[
            Subgoal(id=1, description="background", done=False),
            Subgoal(id=2, description="finances", done=False),
        ],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=3,
    )
    write_plan(job, seeded.model_dump())

    # Need at least HEURISTIC_CHECK_EVERY_N tasks done so the synth heuristic
    # fires once. Seeding exactly N — the heuristic runs after task #25.
    _seed_tasks(job, ["web_search"] * HEURISTIC_CHECK_EVERY_N)

    synth_calls = {"n": 0}

    async def synth(job_arg: Job, task: dict[str, Any]) -> dict[str, Any]:
        synth_calls["n"] += 1
        plan_module.update_subgoal_done(
            job_arg, {1: "confirmed", 2: "refuted"}
        )
        return {"ok": True}

    handlers: dict[str, Handler] = {"web_search": _ok_handler(), "synthesize": synth}

    result = await run_loop(
        job,
        router=None,
        plan=seeded,
        handlers=handlers,
        max_tasks=HEURISTIC_CHECK_EVERY_N + 5,
        retry_waits=(0,),
    )

    assert synth_calls["n"] == 1
    assert result["completed"] is True
    assert result["cap_hit"] is False
    assert result["stopped"] is False
    assert result["tasks_done"] == HEURISTIC_CHECK_EVERY_N

    rows = _read_task_rows(db_path, job.id)
    done_count = sum(1 for r in rows if r["status"] == STATUS_DONE)
    pending_count = sum(1 for r in rows if r["status"] == STATUS_PENDING)
    assert done_count == HEURISTIC_CHECK_EVERY_N
    assert pending_count == 0
