"""Tests for ``research_agent.orchestrator.loop``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from research_agent.orchestrator import plan as plan_module
from research_agent.orchestrator.errors import FatalError, RetriableError
from research_agent.orchestrator.loop import (
    HEURISTIC_CHECK_EVERY_N,
    MAX_DRAIN_REPLANS,
    MAX_TASKS_PER_JOB,
    RETRY_MAX_ATTEMPTS,
    Handler,
    _expand_search_to_fetches,
    _run_extract_findings,
    default_handlers,
    run_loop,
)
from research_agent.orchestrator.plan import Plan, ScopeClass, Subgoal, TaskSpec
from research_agent.tools.models import SearchResult
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


CONNECTOR_KIND_PREFIXES: tuple[str, ...] = (
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

# Connector module name → ``source_kind`` Literal value. Most connectors
# match by name; ``edgar`` is the SEC connector so its source_kind is "sec".
_CONNECTOR_SOURCE_KIND: dict[str, str] = {p: p for p in CONNECTOR_KIND_PREFIXES}
_CONNECTOR_SOURCE_KIND["edgar"] = "sec"


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
    for prefix in CONNECTOR_KIND_PREFIXES:
        expected.add(f"{prefix}_search")
        expected.add(f"{prefix}_fetch")
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


# ---------------------------------------------------------------------------
# Issue #175: connector-specific kinds dispatch to tools.<name>.search()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", CONNECTOR_KIND_PREFIXES)
async def test_connector_search_handler_dispatches_to_module(
    job: Job, monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    """Each ``<prefix>_search`` handler must call ``tools.<prefix>.search`` and
    expand top hits into ``web_fetch`` follow-ups via the standard helper.
    """
    import importlib

    mod = importlib.import_module(f"research_agent.tools.{prefix}")
    captured: dict[str, Any] = {}

    sk = _CONNECTOR_SOURCE_KIND[prefix]

    async def fake_search(query: str, **kwargs: Any) -> list[SearchResult]:
        captured["query"] = query
        captured["kwargs"] = kwargs
        return [
            SearchResult(
                url=f"https://{prefix}.example/1",
                title="Hit 1",
                snippet="…",
                source_kind=sk,  # type: ignore[arg-type]
            ),
            SearchResult(
                url=f"https://{prefix}.example/2",
                title="Hit 2",
                snippet="…",
                source_kind=sk,  # type: ignore[arg-type]
            ),
        ]

    monkeypatch.setattr(mod, "search", fake_search)

    handlers = default_handlers(router=None)
    handler = handlers[f"{prefix}_search"]
    out = await handler(
        job,
        {"kind": f"{prefix}_search", "payload": {"query": "needle", "kind": "x"}},
    )

    assert captured["query"] == "needle"
    # ``kind`` is in the passthrough allowlist so it reaches the connector.
    assert captured["kwargs"].get("kind") == "x"
    assert isinstance(out, dict)
    assert "results" in out
    assert "follow_up_tasks" in out
    assert len(out["results"]) == 2
    follow_kinds = {f["kind"] for f in out["follow_up_tasks"]}
    assert follow_kinds == {"web_fetch"}


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", CONNECTOR_KIND_PREFIXES)
async def test_connector_fetch_handler_dispatches_to_module(
    job: Job, monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    """Each ``<prefix>_fetch`` handler must call ``tools.<prefix>.fetch`` and
    persist the returned :class:`Source` via the shared helper.
    """
    import importlib
    from datetime import datetime, timezone

    from research_agent.tools.models import Source

    mod = importlib.import_module(f"research_agent.tools.{prefix}")
    captured: dict[str, Any] = {}

    sk = _CONNECTOR_SOURCE_KIND[prefix]

    async def fake_fetch(url: str) -> Source:
        captured["url"] = url
        return Source(
            url=url,
            title="t",
            cleaned_text="hello world",
            fetched_at=datetime.now(tz=timezone.utc),
            source_kind=sk,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(mod, "fetch", fake_fetch)

    handlers = default_handlers(router=None)
    handler = handlers[f"{prefix}_fetch"]
    out = await handler(
        job,
        {
            "kind": f"{prefix}_fetch",
            "payload": {"url": f"https://{prefix}.example/abc"},
        },
    )

    assert captured["url"] == f"https://{prefix}.example/abc"
    assert isinstance(out, dict)
    assert "source_id" in out
    assert isinstance(out["source_id"], int)


@pytest.mark.asyncio
async def test_connector_search_handler_wraps_runtime_error_as_fatal(
    job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a connector raises ``RuntimeError`` (its missing-credential path),
    the handler must convert it to :class:`FatalError` so the loop marks the
    task failed cleanly down the documented path rather than relying on the
    daemon's catch-all guard.
    """
    from research_agent.tools import linkedin

    async def fake_search(query: str, **kwargs: Any) -> list[SearchResult]:
        raise RuntimeError(
            "linkedin requires LINKEDIN_DATA_API_KEY (or LIX_API_KEY for lix broker)"
        )

    monkeypatch.setattr(linkedin, "search", fake_search)

    handler = default_handlers(router=None)["linkedin_search"]
    with pytest.raises(FatalError, match="LINKEDIN_DATA_API_KEY"):
        await handler(
            job, {"kind": "linkedin_search", "payload": {"query": "Sundar Pichai"}}
        )


@pytest.mark.asyncio
async def test_connector_fetch_handler_wraps_runtime_error_as_fatal(
    job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``<prefix>_fetch`` mirrors the search-side FatalError wrapping."""
    from research_agent.tools import courtlistener

    async def fake_fetch(url: str) -> Any:
        raise RuntimeError("courtlistener requires COURTLISTENER_API_TOKEN")

    monkeypatch.setattr(courtlistener, "fetch", fake_fetch)

    handler = default_handlers(router=None)["courtlistener_fetch"]
    with pytest.raises(FatalError, match="COURTLISTENER_API_TOKEN"):
        await handler(
            job,
            {
                "kind": "courtlistener_fetch",
                "payload": {"url": "https://www.courtlistener.com/opinion/123/"},
            },
        )


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


# ---------------------------------------------------------------------------
# Drain-replan (issue #117) — keep the loop running when the queue empties
# but the plan is not yet complete and the cap hasn't fired.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_replan_chains_when_queue_empties(
    job: Job,
    db_path: Path,
    plan: Plan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the queue drains, fire ``tactical_replan`` and keep going."""
    _seed_tasks(job, ["web_search"] * 4)

    drain_calls = {"n": 0}

    async def fake_tactical_replan(
        job_arg: Job,
        prior_plan: Plan,
        recent_results: list[dict[str, Any]],
        *,
        router: Any,
        findings: list[dict[str, Any]] | None = None,
        synthesis_md: str | None = None,
    ) -> Plan:
        drain_calls["n"] += 1
        next_version = prior_plan.version + 1
        if drain_calls["n"] == 1:
            template = [TaskSpec(kind="web_search", payload={"q": f"r{i}"}) for i in range(4)]
        else:
            template = []
        new = Plan(
            version=next_version,
            objective=prior_plan.objective,
            subgoals=prior_plan.subgoals,
            task_template=template,
            expected_iterations=prior_plan.expected_iterations,
        )
        write_plan(job_arg, new.model_dump())
        if template:
            enqueue(job_arg, list(template), next_version)
        return new

    monkeypatch.setattr(plan_module, "tactical_replan", fake_tactical_replan)

    result = await run_loop(
        job,
        router=None,
        plan=plan,
        handlers={"web_search": _ok_handler()},
        retry_waits=(0,),
    )

    assert drain_calls["n"] == 2
    assert result["drain_replans"] == 2
    assert result["tasks_done"] >= 8

    events = _read_event_kinds(db_path, job.id)
    assert events.count("drain_replan") == 2


@pytest.mark.asyncio
async def test_drain_replan_respects_cap(
    job: Job,
    db_path: Path,
    plan: Plan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past ``MAX_DRAIN_REPLANS`` the loop must bail with a cap-hit warning."""
    _seed_tasks(job, ["web_search"])

    monkeypatch.setattr("research_agent.orchestrator.loop.MAX_DRAIN_REPLANS", 3)

    async def fake_tactical_replan(
        job_arg: Job,
        prior_plan: Plan,
        recent_results: list[dict[str, Any]],
        *,
        router: Any,
        findings: list[dict[str, Any]] | None = None,
        synthesis_md: str | None = None,
    ) -> Plan:
        next_version = prior_plan.version + 1
        template = [TaskSpec(kind="web_search", payload={"q": "more"})]
        new = Plan(
            version=next_version,
            objective=prior_plan.objective,
            subgoals=prior_plan.subgoals,
            task_template=template,
            expected_iterations=prior_plan.expected_iterations,
        )
        write_plan(job_arg, new.model_dump())
        enqueue(job_arg, list(template), next_version)
        return new

    monkeypatch.setattr(plan_module, "tactical_replan", fake_tactical_replan)

    result = await run_loop(
        job,
        router=None,
        plan=plan,
        handlers={"web_search": _ok_handler()},
        retry_waits=(0,),
    )

    assert result["drain_replans"] == 3

    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM events WHERE job_id = ? AND kind = 'warning'",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    import json as _json

    assert any(
        _json.loads(r["payload_json"]).get("drain_replan_cap_hit") is True for r in rows
    )


@pytest.mark.asyncio
async def test_drain_replan_break_on_empty_template(
    job: Job,
    db_path: Path,
    plan: Plan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty task_template means the planner thinks the goal is exhausted."""
    _seed_tasks(job, ["web_search"])

    async def fake_tactical_replan(
        job_arg: Job,
        prior_plan: Plan,
        recent_results: list[dict[str, Any]],
        *,
        router: Any,
        findings: list[dict[str, Any]] | None = None,
        synthesis_md: str | None = None,
    ) -> Plan:
        next_version = prior_plan.version + 1
        new = Plan(
            version=next_version,
            objective=prior_plan.objective,
            subgoals=prior_plan.subgoals,
            task_template=[],
            expected_iterations=prior_plan.expected_iterations,
        )
        write_plan(job_arg, new.model_dump())
        return new

    monkeypatch.setattr(plan_module, "tactical_replan", fake_tactical_replan)

    result = await run_loop(
        job,
        router=None,
        plan=plan,
        handlers={"web_search": _ok_handler()},
        retry_waits=(0,),
    )

    assert result["drain_replans"] == 1
    assert result["tasks_done"] == 1
    events = _read_event_kinds(db_path, job.id)
    assert events.count("drain_replan") == 1


def test_max_drain_replans_default_is_ten() -> None:
    assert MAX_DRAIN_REPLANS == 10


# ---------------------------------------------------------------------------
# _expand_search_to_fetches scope-aware top_K (issue #178)
# ---------------------------------------------------------------------------


def _make_results(n: int) -> list[SearchResult]:
    return [
        SearchResult(
            url=f"https://example.com/{i}",
            title=f"Hit {i}",
            snippet="…",
            source_kind="web",
        )
        for i in range(n)
    ]


def _persist_plan_with_scope(job: Job, scope: ScopeClass | None) -> None:
    p = Plan(
        version=1,
        objective="Investigate the target",
        subgoals=[Subgoal(id=1, description="Gather", done=False)],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=3,
        scope_class=scope,
    )
    write_plan(job, p.model_dump())


def test_expand_search_to_fetches_broad_scope_emits_seven(job: Job) -> None:
    _persist_plan_with_scope(job, "broad")
    results = _make_results(10)

    out = _expand_search_to_fetches(job, {"query": "q"}, results)

    assert len(out["follow_up_tasks"]) == 7
    assert len(out["results"]) == 10


@pytest.mark.parametrize(
    "scope,expected",
    [
        ("narrow", 3),
        ("medium", 5),
        ("broad", 7),
        ("comprehensive", 10),
    ],
)
def test_expand_search_to_fetches_scope_mapping(
    job: Job, scope: ScopeClass, expected: int
) -> None:
    _persist_plan_with_scope(job, scope)
    results = _make_results(10)

    out = _expand_search_to_fetches(job, {"query": "q"}, results)

    assert len(out["follow_up_tasks"]) == expected


def test_expand_search_to_fetches_payload_override_still_wins(job: Job) -> None:
    _persist_plan_with_scope(job, "broad")
    results = _make_results(10)

    out = _expand_search_to_fetches(job, {"query": "q", "expand_top_k": 2}, results)

    assert len(out["follow_up_tasks"]) == 2


def test_expand_search_to_fetches_unknown_scope_falls_back_to_three(job: Job) -> None:
    _persist_plan_with_scope(job, None)
    results = _make_results(10)

    out = _expand_search_to_fetches(job, {"query": "q"}, results)

    assert len(out["follow_up_tasks"]) == 3


def test_expand_search_to_fetches_no_plan_falls_back_to_three(job: Job) -> None:
    # No plan persisted at all — handler should still default to 3.
    results = _make_results(10)

    out = _expand_search_to_fetches(job, {"query": "q"}, results)

    assert len(out["follow_up_tasks"]) == 3


# ---------------------------------------------------------------------------
# Cornerstone-document extraction (issue #177)
# ---------------------------------------------------------------------------


class _StubRouter:
    """Minimal router stand-in for ``_run_extract_findings`` tests.

    Captures every ``call(tier, agent, ...)`` invocation so tests can
    assert tier + agent.system_prompt without going through Pydantic AI.
    The configured ``yaml_output`` is what the model "emits".

    ``model_for`` returns a Pydantic AI :class:`TestModel` because the
    handler constructs an ``Agent`` before our :meth:`call` ever runs,
    and the Agent constructor refuses raw objects.
    """

    def __init__(self, yaml_output: str) -> None:
        self.yaml_output = yaml_output
        self.calls: list[tuple[str, Any, tuple[Any, ...], dict[str, Any]]] = []

    def model_for(self, tier: str) -> Any:
        from pydantic_ai.models.test import TestModel

        return TestModel()

    async def call(self, tier: str, agent: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((tier, agent, args, kwargs))

        class _Result:
            output = self.yaml_output

        return _Result()


def _seed_source(job: Job, *, url: str, body: str) -> int:
    from research_agent.storage.sources import write_source

    return write_source(
        job,
        url=url,
        title="Cornerstone Document",
        raw_content=body,
        kind="web",
    )


def _persist_plan_with_cornerstone(job: Job, url: str | None) -> None:
    p = Plan(
        version=1,
        objective="Index the cornerstone",
        subgoals=[Subgoal(id=1, description="Index", done=False)],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=1,
        cornerstone_url=url,
    )
    write_plan(job, p.model_dump())


_CORNERSTONE_BODY = (
    "DOJ: Proposal 1: Restore Schedule F across the executive branch.\n"
    "DOJ: Proposal 2: Relocate FBI domestic-intelligence operations.\n"
    "DOJ: Proposal 3: Reorganize the Antitrust Division.\n"
    "DOJ: Proposal 4: Clarify the AG's removal authority.\n"
    "State: Proposal 5: Withdraw from the Paris Agreement.\n"
    "State: Proposal 6: Restructure USAID.\n"
    "State: Proposal 7: Re-list the Houthis as a foreign terrorist organization.\n"
    "State: Proposal 8: Limit refugee admissions.\n"
    "EPA: Proposal 9: Halt Clean Air Act greenhouse-gas enforcement.\n"
    "EPA: Proposal 10: Rescind the endangerment finding.\n"
    "EPA: Proposal 11: Devolve Superfund authority to the states.\n"
    "EPA: Proposal 12: End ESG-style cost-benefit calculations.\n"
)

_CORNERSTONE_YAML = """\
```yaml
- claim: "Restore Schedule F across the executive branch (DOJ)."
  confidence: 0.9
  quote: "Restore Schedule F across the executive branch."
  tags: [doj, schedule-f]
- claim: "Relocate FBI domestic-intelligence operations (DOJ)."
  confidence: 0.85
  quote: "Relocate FBI domestic-intelligence operations."
  tags: [doj, fbi]
- claim: "Reorganize the Antitrust Division (DOJ)."
  confidence: 0.8
  quote: "Reorganize the Antitrust Division."
  tags: [doj, antitrust]
- claim: "Clarify the AG's removal authority (DOJ)."
  confidence: 0.8
  quote: "Clarify the AG's removal authority."
  tags: [doj, removal]
- claim: "Withdraw from the Paris Agreement (State)."
  confidence: 0.95
  quote: "Withdraw from the Paris Agreement."
  tags: [state, climate]
- claim: "Restructure USAID (State)."
  confidence: 0.8
  quote: "Restructure USAID."
  tags: [state, usaid]
- claim: "Re-list the Houthis as a foreign terrorist organization (State)."
  confidence: 0.8
  quote: "Re-list the Houthis as a foreign terrorist organization."
  tags: [state, terrorism]
- claim: "Limit refugee admissions (State)."
  confidence: 0.8
  quote: "Limit refugee admissions."
  tags: [state, refugees]
- claim: "Halt Clean Air Act greenhouse-gas enforcement (EPA)."
  confidence: 0.9
  quote: "Halt Clean Air Act greenhouse-gas enforcement."
  tags: [epa, climate]
- claim: "Rescind the endangerment finding (EPA)."
  confidence: 0.85
  quote: "Rescind the endangerment finding."
  tags: [epa, endangerment]
- claim: "Devolve Superfund authority to the states (EPA)."
  confidence: 0.8
  quote: "Devolve Superfund authority to the states."
  tags: [epa, superfund]
- claim: "End ESG-style cost-benefit calculations (EPA)."
  confidence: 0.8
  quote: "End ESG-style cost-benefit calculations."
  tags: [epa, esg]
```
"""


@pytest.mark.asyncio
async def test_extract_findings_cornerstone_path(
    job: Job,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cornerstone source: structured-index prompt + uncapped findings.

    A planner-marked cornerstone URL must (a) route extraction through
    ``researcher_cornerstone.md`` (not ``researcher.md``), (b) write all
    12 findings — past the 8-finding default cap, (c) preserve the
    department tag on each finding so downstream tactical_replan can
    convert them into per-proposal sub-questions.
    """
    url = "https://example.test/policy.md"
    _persist_plan_with_cornerstone(job, url)
    source_id = _seed_source(job, url=url, body=_CORNERSTONE_BODY)

    # _run_extract_findings does ``from research_agent.prompts.loader import
    # load_prompt`` inside the function body, so each call re-resolves the
    # name against the loader module. Patching the module attribute is
    # enough — no need to also patch any orchestrator-side symbol.
    import research_agent.prompts.loader as _loader_mod

    loaded_prompts: list[str] = []
    real_load = _loader_mod.load_prompt

    def _spy_load(name: str, *args: Any, **kwargs: Any) -> str:
        loaded_prompts.append(name)
        return real_load(name, *args, **kwargs)

    monkeypatch.setattr(_loader_mod, "load_prompt", _spy_load)

    router = _StubRouter(_CORNERSTONE_YAML)
    task = {"payload": {"source_id": source_id, "sub_question": "List proposals."}}

    result = await _run_extract_findings(job, task, router=router)

    assert result["findings_written"] == 12
    assert result["skipped"] == 0
    assert "researcher_cornerstone" in loaded_prompts
    assert "researcher" not in loaded_prompts

    # Every finding should carry a department tag.
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT tags FROM findings WHERE job_id = ? ORDER BY id ASC",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 12
    import json as _json

    departments = {"doj", "state", "epa"}
    for row in rows:
        tags = _json.loads(row["tags"]) if row["tags"] else []
        assert any(t in departments for t in tags), tags

    # cornerstone_extract event must have been emitted.
    events = _read_event_kinds(db_path, job.id)
    assert "cornerstone_extract" in events


@pytest.mark.asyncio
async def test_extract_findings_non_cornerstone_uses_default_cap(
    job: Job,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-cornerstone source still caps at 8 findings via the default prompt."""
    url = "https://example.test/article.html"
    _persist_plan_with_cornerstone(job, "https://example.test/totally-different.pdf")
    source_id = _seed_source(job, url=url, body="A short news article body.")

    loaded_prompts: list[str] = []
    import research_agent.prompts.loader as _loader_mod

    real_load = _loader_mod.load_prompt

    def _spy_load(name: str, *args: Any, **kwargs: Any) -> str:
        loaded_prompts.append(name)
        return real_load(name, *args, **kwargs)

    monkeypatch.setattr(_loader_mod, "load_prompt", _spy_load)

    # Emit 12 findings — the default cap should drop the last 4.
    yaml_payload = "```yaml\n" + "".join(
        f'- claim: "Claim {i}"\n  confidence: 0.8\n  quote: ""\n  tags: [t{i}]\n'
        for i in range(12)
    ) + "```\n"
    router = _StubRouter(yaml_payload)
    task = {"payload": {"source_id": source_id, "sub_question": "What?"}}

    result = await _run_extract_findings(job, task, router=router)

    assert result["findings_written"] == 8
    assert "researcher" in loaded_prompts
    assert "researcher_cornerstone" not in loaded_prompts
