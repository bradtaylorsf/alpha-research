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
    _is_cornerstone_source,
    _load_source_text,
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
        "cornerstone_query",
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
    """When a connector raises :class:`MissingCredentialError`, the handler
    must convert it to :class:`FatalError` so the loop marks the task failed
    cleanly down the documented path rather than relying on the daemon's
    catch-all guard. Preserves the smoke-skip contract.
    """
    from research_agent.tools import linkedin
    from research_agent.tools._errors import MissingCredentialError

    async def fake_search(query: str, **kwargs: Any) -> list[SearchResult]:
        raise MissingCredentialError(
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
    """``<prefix>_fetch`` mirrors the search-side FatalError wrapping for
    :class:`MissingCredentialError`."""
    from research_agent.tools import courtlistener
    from research_agent.tools._errors import MissingCredentialError

    async def fake_fetch(url: str) -> Any:
        raise MissingCredentialError(
            "courtlistener requires COURTLISTENER_API_TOKEN"
        )

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
async def test_connector_search_handler_does_not_wrap_unrelated_runtime_error(
    job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-credential ``RuntimeError`` from inside a connector must NOT get
    masked as a credential FatalError. It propagates to the loop's catch-all
    so the original bug message surfaces in ``daemon/error`` events with
    traceback. Issue #190.
    """
    from research_agent.tools import congress

    bug_message = "unrecoverable: response.json() returned None"

    async def fake_search(query: str, **kwargs: Any) -> list[SearchResult]:
        raise RuntimeError(bug_message)

    monkeypatch.setattr(congress, "search", fake_search)

    handler = default_handlers(router=None)["congress_search"]
    with pytest.raises(RuntimeError) as excinfo:
        await handler(
            job, {"kind": "congress_search", "payload": {"query": "needle"}}
        )
    assert not isinstance(excinfo.value, FatalError)
    assert bug_message in str(excinfo.value)


@pytest.mark.asyncio
async def test_connector_fetch_handler_does_not_wrap_unrelated_runtime_error(
    job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror of the search variant for the fetch handler. Issue #190."""
    from research_agent.tools import congress

    bug_message = "boom: state-machine violation in fetch"

    async def fake_fetch(url: str) -> Any:
        raise RuntimeError(bug_message)

    monkeypatch.setattr(congress, "fetch", fake_fetch)

    handler = default_handlers(router=None)["congress_fetch"]
    with pytest.raises(RuntimeError) as excinfo:
        await handler(
            job,
            {
                "kind": "congress_fetch",
                "payload": {"url": "https://www.congress.gov/bill/118/hr/1"},
            },
        )
    assert not isinstance(excinfo.value, FatalError)
    assert bug_message in str(excinfo.value)


@pytest.mark.asyncio
async def test_connector_fetch_handler_missing_url_raises_fatal(
    job: Job,
) -> None:
    """A connector-fetch task without a ``url`` payload field surfaces as a
    clean :class:`FatalError` rather than a ``KeyError`` into the daemon
    catch-all. Issue #190.
    """
    handler = default_handlers(router=None)["congress_fetch"]
    with pytest.raises(FatalError, match="missing url field"):
        await handler(job, {"kind": "congress_fetch", "payload": {}})


# ---------------------------------------------------------------------------
# Issue #193: bill-text fan-out via _persist_fetched_source
# ---------------------------------------------------------------------------


def _make_congress_source(
    *,
    url: str = "https://www.congress.gov/bill/117th-congress/house-bill/5376",
    title: str = "Inflation Reduction Act of 2022",
    bill_text_url: str | None = (
        "https://www.congress.gov/117/bills/hr5376/BILLS-117hr5376enr.pdf"
    ),
    bill_text_format: str | None = "PDF",
) -> Any:
    from datetime import UTC, datetime

    from research_agent.tools.models import Source

    metadata: dict[str, Any] = {
        "congress": 117,
        "bill_type": "hr",
        "bill_number": "5376",
    }
    if bill_text_url is not None:
        metadata["bill_text_url"] = bill_text_url
    if bill_text_format is not None:
        metadata["bill_text_format"] = bill_text_format

    return Source(
        url=url,
        title=title,
        cleaned_text=f"# {title}\n\nMetadata roll-up.",
        fetched_at=datetime.now(tz=UTC),
        source_kind="congress",
        metadata=metadata,
    )


def test_persist_fetched_source_emits_bill_text_followup(job: Job) -> None:
    """A congress source with ``bill_text_url`` in metadata fans out a
    ``web_fetch`` follow-up pointing at the bill body — not at the metadata
    URL — and inherits ``sub_question`` from the originating task payload.
    """
    from research_agent.orchestrator.loop import _persist_fetched_source

    bill_text_url = "https://www.congress.gov/117/bills/hr5376/BILLS-117hr5376enr.pdf"
    source = _make_congress_source(bill_text_url=bill_text_url)

    payload = {
        "url": "https://www.congress.gov/bill/117th-congress/house-bill/5376",
        "sub_question": "How does HR 5376 fund climate provisions?",
    }
    result = _persist_fetched_source(job, source, payload=payload)

    assert isinstance(result, dict)
    assert "follow_up_tasks" in result
    follow_ups = result["follow_up_tasks"]
    assert len(follow_ups) == 1
    fu = follow_ups[0]
    assert fu["kind"] == "web_fetch"
    assert fu["payload"]["url"] == bill_text_url
    # Critically: the follow-up's URL must be the bill text, not the metadata
    # source URL — that's the bug #193 fixes.
    assert fu["payload"]["url"] != source.url
    assert fu["payload"]["sub_question"] == "How does HR 5376 fund climate provisions?"


def test_persist_fetched_source_no_followup_when_bill_text_url_absent(
    job: Job,
) -> None:
    """When the metadata has no ``bill_text_url`` (e.g. newly-introduced bill
    with no published text yet), no follow-up is emitted and no log noise.
    """
    from research_agent.orchestrator.loop import _persist_fetched_source

    source = _make_congress_source(bill_text_url=None, bill_text_format=None)
    result = _persist_fetched_source(job, source, payload={"sub_question": "anything"})

    assert "follow_up_tasks" not in result


def test_persist_fetched_source_dedups_already_fetched_bill_text(
    job: Job,
) -> None:
    """Anti-runaway: if the bill text URL has already been fetched for this
    job, ``_persist_fetched_source`` must not re-emit a follow-up. Otherwise
    tactical_replan can cycle the same bill repeatedly.
    """
    from research_agent.orchestrator.loop import _persist_fetched_source
    from research_agent.storage.sources import write_source

    bill_text_url = "https://www.congress.gov/117/bills/hr5376/BILLS-117hr5376enr.pdf"

    # Pre-seed: the bill text has already been fetched for this job.
    write_source(
        job,
        url=bill_text_url,
        title="HR 5376 — Bill Text",
        raw_content="Section 1. Findings. Section 2. ...",
        kind="pdf",
    )

    source = _make_congress_source(bill_text_url=bill_text_url)
    result = _persist_fetched_source(job, source, payload={"sub_question": "again?"})

    assert "follow_up_tasks" not in result


def test_persist_fetched_source_falls_back_sub_question_from_title(
    job: Job,
) -> None:
    """When no payload sub_question is supplied, the follow-up's sub_question
    is derived from the bill title so downstream extraction still has context.
    """
    from research_agent.orchestrator.loop import _persist_fetched_source

    source = _make_congress_source(title="Inflation Reduction Act of 2022")
    # Pass an empty payload (no sub_question key).
    result = _persist_fetched_source(job, source, payload={})

    follow_ups = result.get("follow_up_tasks") or []
    assert len(follow_ups) == 1
    sub_q = follow_ups[0]["payload"]["sub_question"]
    assert "Inflation Reduction Act of 2022" in sub_q


@pytest.mark.asyncio
async def test_congress_fetch_handler_emits_bill_text_followup(
    job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the handler: the ``congress_fetch`` handler returns
    a ``follow_up_tasks`` list whose only entry is a ``web_fetch`` for the
    bill text URL, with the originating task's ``sub_question`` propagated.
    """
    from research_agent.tools import congress as congress_mod

    bill_text_url = "https://www.congress.gov/117/bills/hr5376/BILLS-117hr5376enr.pdf"
    metadata_url = "https://www.congress.gov/bill/117th-congress/house-bill/5376"
    expected_source = _make_congress_source(
        url=metadata_url, bill_text_url=bill_text_url
    )

    async def fake_fetch(url: str) -> Any:
        assert url == metadata_url
        return expected_source

    monkeypatch.setattr(congress_mod, "fetch", fake_fetch)

    handler = default_handlers(router=None)["congress_fetch"]
    out = await handler(
        job,
        {
            "kind": "congress_fetch",
            "payload": {
                "url": metadata_url,
                "sub_question": "What does HR 5376 do for energy?",
            },
        },
    )

    assert isinstance(out, dict)
    assert "source_id" in out
    follow_ups = out.get("follow_up_tasks") or []
    assert len(follow_ups) == 1
    fu = follow_ups[0]
    assert fu["kind"] == "web_fetch"
    assert fu["payload"]["url"] == bill_text_url
    assert fu["payload"]["sub_question"] == "What does HR 5376 do for energy?"


@pytest.mark.asyncio
async def test_web_fetch_handler_merges_bill_text_and_extract_followups(
    job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #193 + the existing extract-findings fan-out interact correctly:
    when ``web_fetch`` host-dispatches to ``congress.fetch`` and the source has
    ``bill_text_url``, the resulting follow_up_tasks must contain BOTH the
    bill-text web_fetch and the extract_findings task on the metadata source.
    """
    from research_agent.tools import web_fetch as web_fetch_mod

    bill_text_url = "https://www.congress.gov/117/bills/hr5376/BILLS-117hr5376enr.pdf"
    metadata_url = "https://www.congress.gov/bill/117th-congress/house-bill/5376"

    async def fake_fetch(url: str) -> Any:
        return _make_congress_source(url=url, bill_text_url=bill_text_url)

    monkeypatch.setattr(web_fetch_mod, "fetch", fake_fetch)

    handler = default_handlers(router=None)["web_fetch"]
    out = await handler(
        job,
        {
            "kind": "web_fetch",
            "payload": {
                "url": metadata_url,
                "sub_question": "energy provisions?",
            },
        },
    )

    follow_ups = out.get("follow_up_tasks") or []
    kinds = sorted(f["kind"] for f in follow_ups)
    assert kinds == ["extract_findings", "web_fetch"]
    bill_followup = next(f for f in follow_ups if f["kind"] == "web_fetch")
    assert bill_followup["payload"]["url"] == bill_text_url


@pytest.mark.asyncio
async def test_connector_search_handler_drops_kwargs_connector_does_not_accept(
    job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planner drift: a passthrough kwarg the target connector doesn't accept
    must be dropped, not raised as a TypeError that bypasses the documented
    FatalError path. ``edgar.search`` takes ``form_type`` (not ``kind``); a
    plan that emits ``kind`` for ``edgar_search`` must still produce a clean
    call rather than crashing.
    """
    from research_agent.tools import edgar

    captured: dict[str, Any] = {}

    async def fake_search(
        query: str,
        *,
        form_type: Any = None,
        max_results: int = 20,
        timeout: float = 15.0,
    ) -> list[SearchResult]:
        captured["query"] = query
        captured["form_type"] = form_type
        captured["max_results"] = max_results
        return []

    monkeypatch.setattr(edgar, "search", fake_search)

    handler = default_handlers(router=None)["edgar_search"]
    out = await handler(
        job,
        {
            "kind": "edgar_search",
            "payload": {
                "query": "cybersecurity",
                "kind": "should-be-dropped",  # edgar takes form_type, not kind
                "form_type": "8-K",
                "max_results": 5,
            },
        },
    )
    assert captured == {
        "query": "cybersecurity",
        "form_type": "8-K",
        "max_results": 5,
    }
    assert isinstance(out, dict)
    assert out["results"] == []


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
        follow_up_questions: list[str] | None = None,
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
        follow_up_questions: list[str] | None = None,
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
        follow_up_questions: list[str] | None = None,
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


def _seed_source(job: Job, *, url: str, body: str, kind: str = "web") -> int:
    from research_agent.storage.sources import write_source

    return write_source(
        job,
        url=url,
        title="Cornerstone Document",
        raw_content=body,
        kind=kind,
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


def test_is_cornerstone_source_size_fallback_gated_to_pdf(
    job: Job,
) -> None:
    """Issue #189: size-fallback only fires for ``source_kind == "pdf"``.

    Three cases — all share the same 250k-char body, only ``source_kind``
    and the planner marker differ:

    * HTML, no planner match → ``(False, False)`` — long-form HTML must
      not be promoted to cornerstone, or #177's report-padding regression
      comes back.
    * PDF, no planner match  → ``(True, True)`` — the second flag tells
      the call site to emit ``cornerstone_fallback_triggered``.
    * Planner-marked URL     → ``(True, False)`` — primary signal wins,
      no fallback event regardless of kind/size.
    """
    # Bodies must differ — sources dedupe by content sha256 across kinds.
    html_id = _seed_source(
        job,
        url="https://example.test/long-article.html",
        body="H" * 250_000,
        kind="html",
    )
    pdf_id = _seed_source(
        job,
        url="https://example.test/big-doc.pdf",
        body="P" * 250_000,
        kind="pdf",
    )
    marked_id = _seed_source(
        job,
        url="https://example.test/marked.html",
        body="short body",
        kind="html",
    )

    _persist_plan_with_cornerstone(job, "https://example.test/marked.html")

    html_loaded = _load_source_text(job, html_id)
    pdf_loaded = _load_source_text(job, pdf_id)
    marked_loaded = _load_source_text(job, marked_id)
    assert html_loaded is not None
    assert pdf_loaded is not None
    assert marked_loaded is not None

    html_text, html_meta = html_loaded
    pdf_text, pdf_meta = pdf_loaded
    marked_text, marked_meta = marked_loaded

    assert _is_cornerstone_source(job, html_meta, html_text) == (False, False)
    assert _is_cornerstone_source(job, pdf_meta, pdf_text) == (True, True)
    assert _is_cornerstone_source(job, marked_meta, marked_text) == (True, False)


@pytest.mark.asyncio
async def test_extract_findings_pdf_fallback_emits_event(
    job: Job,
    db_path: Path,
) -> None:
    """PDF size-fallback path emits ``cornerstone_fallback_triggered`` (WARN).

    The planner did not mark this URL; only the PDF size-fallback path
    routes the source through the cornerstone prompt, so the operator-
    visible WARN event must fire alongside the existing INFO
    ``cornerstone_extract`` event.
    """
    url = "https://example.test/forgot-to-mark.pdf"
    _persist_plan_with_cornerstone(job, "https://example.test/something-else.pdf")
    source_id = _seed_source(job, url=url, body="P" * 250_000, kind="pdf")

    router = _StubRouter("```yaml\n[]\n```\n")
    task = {"payload": {"source_id": source_id, "sub_question": "What?"}}

    await _run_extract_findings(job, task, router=router)

    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT level, kind, payload_json FROM events"
            " WHERE job_id = ? AND kind = 'cornerstone_fallback_triggered'",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["level"] == "WARN"
    import json as _json

    payload = _json.loads(rows[0]["payload_json"])
    assert payload["source_id"] == source_id
    assert payload["url"] == url
    assert payload["source_kind"] == "pdf"
    # md_path is written as ``cleaned + "\n"`` so the read-back text adds
    # one byte to the body length; tolerate that without fixing the value.
    assert payload["cleaned_chars"] >= 250_000

    # Both the fallback WARN and the standard cornerstone_extract INFO must fire.
    kinds = _read_event_kinds(db_path, job.id)
    assert "cornerstone_fallback_triggered" in kinds
    assert "cornerstone_extract" in kinds


@pytest.mark.asyncio
async def test_extract_findings_html_size_does_not_emit_fallback(
    job: Job,
    db_path: Path,
) -> None:
    """A 250k-char HTML source must NOT trigger the size fallback.

    Long-form HTML (Wikipedia, archive.org transcripts) frequently
    crosses the 200k-char threshold without being investigation
    cornerstones — the gate must keep them on the regular extraction
    path with the 8-finding cap intact.
    """
    url = "https://example.test/very-long.html"
    _persist_plan_with_cornerstone(job, "https://example.test/something-else.pdf")
    source_id = _seed_source(job, url=url, body="H" * 250_000, kind="html")

    router = _StubRouter("```yaml\n[]\n```\n")
    task = {"payload": {"source_id": source_id, "sub_question": "What?"}}

    await _run_extract_findings(job, task, router=router)

    kinds = _read_event_kinds(db_path, job.id)
    assert "cornerstone_fallback_triggered" not in kinds
    assert "cornerstone_extract" not in kinds


@pytest.mark.asyncio
async def test_extract_findings_planner_marker_does_not_emit_fallback(
    job: Job,
    db_path: Path,
) -> None:
    """Planner-marked cornerstone never emits the fallback WARN event."""
    url = "https://example.test/policy.md"
    _persist_plan_with_cornerstone(job, url)
    source_id = _seed_source(job, url=url, body=_CORNERSTONE_BODY)

    router = _StubRouter("```yaml\n[]\n```\n")
    task = {"payload": {"source_id": source_id, "sub_question": "What?"}}

    await _run_extract_findings(job, task, router=router)

    kinds = _read_event_kinds(db_path, job.id)
    assert "cornerstone_extract" in kinds
    assert "cornerstone_fallback_triggered" not in kinds


# ---------------------------------------------------------------------------
# Cornerstone PDF section-walk + vector index + cornerstone_query (issue #206)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cornerstone_pdf_section_walk_emits_per_section_events(
    job: Job,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PDF cornerstone with multi-section walk emits per-section telemetry.

    The handler:
    * Calls ``pdf.extract_sections`` for the PDF URL,
    * Runs the cornerstone-extract prompt once per section,
    * Tags each finding with the section breadcrumb,
    * Emits ``cornerstone_section_extract`` per section,
    * Stops the indexer from running by stubbing it (asserted separately),
    * Aggregates findings across sections.
    """
    url = "https://example.test/cornerstone.pdf"
    _persist_plan_with_cornerstone(job, url)
    source_id = _seed_source(
        job, url=url, body="full pdf body — extracted markdown", kind="pdf"
    )

    fake_sections = [
        {
            "breadcrumb": "Doc > Chapter 1 (pages 1-10)",
            "text": "DOJ chapter prose " * 200,
            "page_start": 1,
            "page_end": 10,
            "structured": True,
        },
        {
            "breadcrumb": "Doc > Chapter 2 (pages 11-20)",
            "text": "EPA chapter prose " * 200,
            "page_start": 11,
            "page_end": 20,
            "structured": True,
        },
    ]

    from research_agent.tools import pdf as pdf_mod

    async def _fake_extract_sections(*_args, **_kwargs):
        return fake_sections

    monkeypatch.setattr(pdf_mod, "extract_sections", _fake_extract_sections)

    # Don't actually run the embedder — assert the orchestrator at least
    # tries to build the index and fails-open on its absence.
    index_calls: list[int] = []

    def _fake_index(job_arg, parent_id, sections, **kwargs):
        index_calls.append(parent_id)
        return {"chunks_indexed": 4, "chunks_skipped": 0, "embed_dim": 1024}

    from research_agent.tools import local_corpus as lc_mod

    monkeypatch.setattr(lc_mod, "index_cornerstone_source", _fake_index)

    # Each section yields 2 findings — total 4 once aggregated.
    yaml_payload = (
        "```yaml\n"
        '- claim: "section finding A"\n  confidence: 0.8\n  quote: ""\n  tags: [t1]\n'
        '- claim: "section finding B"\n  confidence: 0.8\n  quote: ""\n  tags: [t2]\n'
        "```\n"
    )
    router = _StubRouter(yaml_payload)
    task = {"payload": {"source_id": source_id, "sub_question": "List proposals."}}

    result = await _run_extract_findings(job, task, router=router)

    assert result["findings_written"] == 4
    kinds = _read_event_kinds(db_path, job.id)
    assert kinds.count("cornerstone_section_extract") == 2
    assert "cornerstone_index_built" in kinds
    assert index_calls == [source_id]

    # Every finding row should carry the section breadcrumb in its tags.
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT tags FROM findings WHERE job_id = ? ORDER BY id ASC",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()
    import json as _json

    breadcrumbs_seen = set()
    for row in rows:
        tags = _json.loads(row["tags"]) if row["tags"] else []
        breadcrumbs_seen.update(t for t in tags if "Chapter" in t)
    assert {
        "Doc > Chapter 1 (pages 1-10)",
        "Doc > Chapter 2 (pages 11-20)",
    } <= breadcrumbs_seen


@pytest.mark.asyncio
async def test_cornerstone_pdf_index_failure_does_not_fail_extract(
    job: Job,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cornerstone_index_failed`` event fires; extract still returns findings."""
    url = "https://example.test/cornerstone-fail.pdf"
    _persist_plan_with_cornerstone(job, url)
    source_id = _seed_source(job, url=url, body="pdf body", kind="pdf")

    from research_agent.tools import pdf as pdf_mod

    async def _fake_extract_sections(*_args, **_kwargs):
        return [
            {
                "breadcrumb": "Doc > Only (pages 1-10)",
                "text": "section body",
                "page_start": 1,
                "page_end": 10,
                "structured": True,
            }
        ]

    monkeypatch.setattr(pdf_mod, "extract_sections", _fake_extract_sections)

    from research_agent.tools import local_corpus as lc_mod

    def _boom(*_a, **_k):
        raise RuntimeError("LM Studio is down")

    monkeypatch.setattr(lc_mod, "index_cornerstone_source", _boom)

    yaml_payload = (
        "```yaml\n"
        '- claim: "real finding"\n  confidence: 0.8\n  quote: ""\n  tags: [t1]\n'
        "```\n"
    )
    router = _StubRouter(yaml_payload)
    task = {"payload": {"source_id": source_id, "sub_question": "?"}}

    result = await _run_extract_findings(job, task, router=router)
    assert result["findings_written"] == 1

    kinds = _read_event_kinds(db_path, job.id)
    assert "cornerstone_index_failed" in kinds


@pytest.mark.asyncio
async def test_cornerstone_section_walk_dedupes_by_jaccard_when_unstructured(
    job: Job,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sliding-window sections must dedupe near-duplicate findings (Jaccard ≥ 0.85)."""
    url = "https://example.test/scan.pdf"
    _persist_plan_with_cornerstone(job, url)
    source_id = _seed_source(job, url=url, body="pdf body", kind="pdf")

    from research_agent.tools import pdf as pdf_mod

    async def _fake_extract_sections(*_args, **_kwargs):
        return [
            {
                "breadcrumb": "Doc > window 1 (chars 0-150000)",
                "text": "win 1 body " * 100,
                "page_start": 1,
                "page_end": 50,
                "structured": False,
            },
            {
                "breadcrumb": "Doc > window 2 (chars 140000-290000)",
                "text": "win 2 body " * 100,
                "page_start": 50,
                "page_end": 100,
                "structured": False,
            },
        ]

    monkeypatch.setattr(pdf_mod, "extract_sections", _fake_extract_sections)
    from research_agent.tools import local_corpus as lc_mod

    monkeypatch.setattr(
        lc_mod,
        "index_cornerstone_source",
        lambda *_a, **_k: {"chunks_indexed": 0, "chunks_skipped": 0, "embed_dim": 1024},
    )

    yaml_payload = (
        "```yaml\n"
        '- claim: "Schedule F should be reinstated across the executive branch"\n'
        "  confidence: 0.9\n  quote: \"\"\n  tags: [schedule-f]\n"
        "```\n"
    )
    router = _StubRouter(yaml_payload)
    task = {"payload": {"source_id": source_id, "sub_question": "?"}}

    result = await _run_extract_findings(job, task, router=router)
    # Both windows would produce the identical claim; dedupe must keep one.
    assert result["findings_written"] == 1

    kinds = _read_event_kinds(db_path, job.id)
    assert "cornerstone_dedup" in kinds


@pytest.mark.asyncio
async def test_cornerstone_section_walk_parses_followup_questions(
    job: Job,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mapping-form YAML with ``follow_up_questions`` is parsed + emitted."""
    url = "https://example.test/with-followups.pdf"
    _persist_plan_with_cornerstone(job, url)
    source_id = _seed_source(job, url=url, body="pdf body", kind="pdf")

    from research_agent.tools import pdf as pdf_mod

    async def _fake_extract_sections(*_args, **_kwargs):
        return [
            {
                "breadcrumb": "Doc > Section 1 (pages 1-5)",
                "text": "body",
                "page_start": 1,
                "page_end": 5,
                "structured": True,
            }
        ]

    monkeypatch.setattr(pdf_mod, "extract_sections", _fake_extract_sections)
    from research_agent.tools import local_corpus as lc_mod

    monkeypatch.setattr(
        lc_mod,
        "index_cornerstone_source",
        lambda *_a, **_k: {"chunks_indexed": 0, "chunks_skipped": 0, "embed_dim": 1024},
    )

    yaml_payload = (
        "```yaml\n"
        "findings:\n"
        '  - claim: "real claim"\n    confidence: 0.8\n    quote: ""\n    tags: [t1]\n'
        "follow_up_questions:\n"
        '  - "Is Schedule F implementation pending OPM guidance?"\n'
        '  - "Have any agencies issued draft regulations yet?"\n'
        "```\n"
    )
    router = _StubRouter(yaml_payload)
    task = {"payload": {"source_id": source_id, "sub_question": "?"}}

    result = await _run_extract_findings(job, task, router=router)
    assert result["findings_written"] == 1
    assert result["follow_up_questions"] == [
        "Is Schedule F implementation pending OPM guidance?",
        "Have any agencies issued draft regulations yet?",
    ]
    kinds = _read_event_kinds(db_path, job.id)
    assert "cornerstone_followups_emitted" in kinds


@pytest.mark.asyncio
async def test_cornerstone_query_handler_uses_index_and_writes_findings(
    job: Job,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``cornerstone_query`` handler retrieves chunks + writes findings."""
    from research_agent.orchestrator.loop import _run_cornerstone_query
    from research_agent.storage.sources import write_source

    parent_id = write_source(
        job,
        url="https://example.test/cornerstone.pdf",
        title="Cornerstone",
        raw_content="parent body",
        kind="pdf",
    )

    # Seed two cornerstone_chunk rows under the parent so the handler has
    # something to fetch back from disk.
    chunk_a_id = write_source(
        job,
        url="https://example.test/cornerstone.pdf",
        title="Cornerstone: Chapter 1",
        raw_content="This chunk is from Chapter 1. Schedule F proposal text.",
        kind="cornerstone_chunk",
        embedding=b"\x00" * (1024 * 4),
        parent_source_id=parent_id,
    )
    write_source(
        job,
        url="https://example.test/cornerstone.pdf",
        title="Cornerstone: Chapter 2",
        raw_content="This chunk is from Chapter 2. WOTUS rulemaking text.",
        kind="cornerstone_chunk",
        embedding=b"\x00" * (1024 * 4),
        parent_source_id=parent_id,
    )

    from research_agent.tools import local_corpus as lc_mod

    def _fake_query(query, job_arg, parent_arg, *, top_k=8, models_config=None):
        # Look up the chunk's md_path so the handler reads the right file.
        conn = db.connect(job_arg.db_path)
        try:
            row = conn.execute(
                "SELECT md_path, sha256, title FROM sources WHERE id = ?",
                (chunk_a_id,),
            ).fetchone()
        finally:
            conn.close()
        return [
            {
                "source_id": chunk_a_id,
                "sha256": row["sha256"],
                "md_path": row["md_path"],
                "title": row["title"],
                "score": 0.9,
            }
        ]

    monkeypatch.setattr(lc_mod, "cornerstone_query", _fake_query)

    yaml_payload = (
        "```yaml\n"
        '- claim: "Retrieved finding about Schedule F"\n'
        "  confidence: 0.8\n  quote: \"\"\n  tags: [schedule-f]\n"
        "```\n"
    )
    router = _StubRouter(yaml_payload)
    task = {
        "payload": {
            "sub_question": "What does the cornerstone say about Schedule F?",
            "cornerstone_url": "https://example.test/cornerstone.pdf",
            "top_k": 4,
        }
    }

    result = await _run_cornerstone_query(job, task, router=router)
    assert result["findings_written"] == 1
    assert result["parent_source_id"] == parent_id
    assert result["hits"] == 1

    kinds = _read_event_kinds(db_path, job.id)
    assert "cornerstone_query_run" in kinds


@pytest.mark.asyncio
async def test_cornerstone_query_handler_requires_target(
    job: Job,
) -> None:
    """Without ``parent_source_id`` or ``cornerstone_url`` the handler raises FatalError."""
    from research_agent.orchestrator.errors import FatalError
    from research_agent.orchestrator.loop import _run_cornerstone_query

    router = _StubRouter("```yaml\n[]\n```\n")
    task = {"payload": {"sub_question": "anything"}}

    with pytest.raises(FatalError):
        await _run_cornerstone_query(job, task, router=router)


@pytest.mark.asyncio
async def test_cornerstone_query_url_resolves_parent_not_chunk(
    job: Job,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URL → parent rowid lookup must skip ``cornerstone_chunk`` rows.

    The indexer copies the parent URL onto every chunk row, so a
    naive ``WHERE url = ?`` lookup can return a chunk id. The handler
    would then filter ``parent_source_id = <chunk_id>`` and find no
    chunks. This test seeds chunks first, then the parent, to make
    the bug deterministic without the resolver fix.
    """
    from research_agent.orchestrator.loop import _run_cornerstone_query
    from research_agent.storage.sources import write_source

    cornerstone_url = "https://example.test/cornerstone-resolver.pdf"

    # Pretend a previous job (or earlier in this run) wrote chunks
    # under the same URL. Insert chunks first so their rowids precede
    # the parent's — without the resolver filter the chunk would be
    # returned as the "parent".
    parent_id = write_source(
        job,
        url=cornerstone_url,
        title="Cornerstone parent",
        raw_content="parent body",
        kind="pdf",
    )
    chunk_id = write_source(
        job,
        url=cornerstone_url,
        title="Cornerstone: window 1",
        raw_content="This chunk is from window 1. body text.",
        kind="cornerstone_chunk",
        embedding=b"\x00" * (1024 * 4),
        parent_source_id=parent_id,
    )

    captured: dict[str, Any] = {}

    def _fake_query(query, job_arg, parent_arg, *, top_k=8, models_config=None):
        captured["parent"] = parent_arg
        return []

    from research_agent.tools import local_corpus as lc_mod

    monkeypatch.setattr(lc_mod, "cornerstone_query", _fake_query)

    router = _StubRouter("```yaml\n[]\n```\n")
    task = {
        "payload": {
            "sub_question": "anything",
            "cornerstone_url": cornerstone_url,
            "top_k": 4,
        }
    }

    result = await _run_cornerstone_query(job, task, router=router)
    assert captured["parent"] == parent_id
    assert captured["parent"] != chunk_id
    assert result["parent_source_id"] == parent_id


@pytest.mark.asyncio
async def test_cornerstone_section_walk_falls_back_for_non_pdf(
    job: Job,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-PDF cornerstones still take the legacy single-pass path.

    Existing planner-marked cornerstone tests pass through this branch
    (kind="web" markdown blob); the section-walk path must scope itself
    to ``source_kind == 'pdf'`` so existing callers don't regress.
    """
    url = "https://example.test/policy.md"
    _persist_plan_with_cornerstone(job, url)
    source_id = _seed_source(job, url=url, body=_CORNERSTONE_BODY, kind="web")

    from research_agent.tools import pdf as pdf_mod

    async def _explode(*_a, **_k):
        raise AssertionError("section walk must not fire for non-PDF cornerstones")

    monkeypatch.setattr(pdf_mod, "extract_sections", _explode)

    router = _StubRouter(_CORNERSTONE_YAML)
    task = {"payload": {"source_id": source_id, "sub_question": "?"}}

    result = await _run_extract_findings(job, task, router=router)
    assert result["findings_written"] == 12
    kinds = _read_event_kinds(db_path, job.id)
    assert "cornerstone_extract" in kinds
    # Single-pass: no per-section events.
    assert "cornerstone_section_extract" not in kinds


# ---------------------------------------------------------------------------
# Issue #194: second-order URL fan-out from extracted findings
# ---------------------------------------------------------------------------


def _persist_plan_with_full_scope(
    job: Job,
    scope: ScopeClass | None,
    *,
    cornerstone_url: str | None = None,
) -> None:
    p = Plan(
        version=1,
        objective="Investigate the target",
        subgoals=[Subgoal(id=1, description="Gather", done=False)],
        task_template=[TaskSpec(kind="web_search")],
        expected_iterations=3,
        scope_class=scope,
        cornerstone_url=cornerstone_url,
    )
    write_plan(job, p.model_dump())


def _reset_blocklist_cache() -> None:
    """Clear the module-level blocklist cache so tests can re-stub it."""
    import research_agent.orchestrator.loop as _loop_mod

    _loop_mod._BLOCKLIST_CACHE = None


@pytest.mark.asyncio
async def test_second_order_fanout_skips_url_already_in_job_sources(
    job: Job,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Citation that points at an already-fetched URL must NOT fan out.

    A finding's claim text contains 2 URLs: one already linked to this
    job via ``job_sources`` (the agent fetched it during a prior task)
    and one new. Only the new URL should become a ``web_fetch``
    follow-up — same-job dedup is the whole point of this guard.
    """
    import research_agent.orchestrator.loop as _loop_mod

    _reset_blocklist_cache()
    monkeypatch.setattr(_loop_mod, "_load_url_blocklist", lambda: set())

    _persist_plan_with_full_scope(job, "broad")

    # Pre-seed: the agent has already fetched ``already.example/doc.pdf`` for
    # this job, so a finding citing that URL must not re-fan-out.
    already_url = "https://already.example/doc.pdf"
    _seed_source(
        job,
        url=already_url,
        body="prior fetch contents",
        kind="pdf",
    )

    new_url = "https://new.example/citation.pdf"
    extract_url = "https://example.test/article.html"
    source_id = _seed_source(
        job,
        url=extract_url,
        body="An article body that triggers extraction.",
        kind="web",
    )

    yaml_payload = (
        "```yaml\n"
        f'- claim: "Important point — see {already_url} and also {new_url} for detail."\n'
        '  confidence: 0.9\n'
        '  quote: ""\n'
        '  tags: [policy]\n'
        "```\n"
    )
    router = _StubRouter(yaml_payload)
    task = {"payload": {"source_id": source_id, "sub_question": "Detail?"}}

    result = await _run_extract_findings(job, task, router=router)

    follow_ups = result.get("follow_up_tasks") or []
    urls = [f["payload"]["url"] for f in follow_ups]
    assert urls == [new_url], (
        f"expected only the new URL to fan out; got {urls}"
    )

    # The fanned-out follow-up must carry the marker key so the job-cap
    # query can count it.
    assert (
        follow_ups[0]["payload"].get("second_order_parent_finding_id") is not None
    )

    # And one ``second_order_fanout`` event must have fired.
    events = _read_event_kinds(db_path, job.id)
    assert events.count("second_order_fanout") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,expected_count",
    [("comprehensive", 3), ("broad", 2)],
)
async def test_second_order_fanout_per_extract_cap_by_scope(
    job: Job,
    monkeypatch: pytest.MonkeyPatch,
    scope: ScopeClass,
    expected_count: int,
) -> None:
    """Per-extract cap follows scope: comprehensive → 3, broad → 2.

    Three high-confidence findings, each citing one distinct URL. The
    same input must produce 3 follow-ups under comprehensive and only 2
    under broad — sorted by parent confidence so the strongest signals
    survive the clip.
    """
    import research_agent.orchestrator.loop as _loop_mod

    _reset_blocklist_cache()
    monkeypatch.setattr(_loop_mod, "_load_url_blocklist", lambda: set())

    _persist_plan_with_full_scope(job, scope)

    extract_url = "https://example.test/article.html"
    source_id = _seed_source(
        job,
        url=extract_url,
        body=f"Body for {scope}",
        kind="web",
    )

    yaml_payload = (
        "```yaml\n"
        '- claim: "Top finding cites https://a.example/one.pdf for evidence."\n'
        '  confidence: 0.95\n'
        '  quote: ""\n'
        '  tags: [a]\n'
        '- claim: "Second finding cites https://b.example/two.pdf for evidence."\n'
        '  confidence: 0.85\n'
        '  quote: ""\n'
        '  tags: [b]\n'
        '- claim: "Third finding cites https://c.example/three.pdf for evidence."\n'
        '  confidence: 0.75\n'
        '  quote: ""\n'
        '  tags: [c]\n'
        "```\n"
    )
    router = _StubRouter(yaml_payload)
    task = {"payload": {"source_id": source_id, "sub_question": "Refs?"}}

    result = await _run_extract_findings(job, task, router=router)

    follow_ups = result.get("follow_up_tasks") or []
    assert len(follow_ups) == expected_count

    urls = [f["payload"]["url"] for f in follow_ups]
    # Highest-confidence finding's URL must always be present (sorted-by-conf).
    assert "https://a.example/one.pdf" in urls
    if expected_count >= 2:
        assert "https://b.example/two.pdf" in urls


@pytest.mark.asyncio
async def test_second_order_fanout_skips_low_confidence_findings(
    job: Job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finding below the 0.5 confidence threshold must NOT fan out.

    The agent's least-reliable signals should not trigger more fetching.
    """
    import research_agent.orchestrator.loop as _loop_mod

    _reset_blocklist_cache()
    monkeypatch.setattr(_loop_mod, "_load_url_blocklist", lambda: set())

    _persist_plan_with_full_scope(job, "comprehensive")

    extract_url = "https://example.test/article.html"
    source_id = _seed_source(
        job,
        url=extract_url,
        body="A weakly-confident article.",
        kind="web",
    )

    yaml_payload = (
        "```yaml\n"
        '- claim: "Possibly relevant — see https://shaky.example/doc.pdf."\n'
        '  confidence: 0.3\n'
        '  quote: ""\n'
        '  tags: [shaky]\n'
        "```\n"
    )
    router = _StubRouter(yaml_payload)
    task = {"payload": {"source_id": source_id, "sub_question": "Maybe?"}}

    result = await _run_extract_findings(job, task, router=router)

    follow_ups = result.get("follow_up_tasks") or []
    assert follow_ups == []


@pytest.mark.asyncio
async def test_second_order_fanout_drops_blocklisted_urls(
    job: Job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A high-confidence finding citing a blocklisted host must NOT fan out.

    The blocklist filters social-media + archive hosts (issue #194)
    even when the parent finding is otherwise eligible.
    """
    import research_agent.orchestrator.loop as _loop_mod

    _reset_blocklist_cache()
    monkeypatch.setattr(
        _loop_mod, "_load_url_blocklist", lambda: {"twitter.com"}
    )

    _persist_plan_with_full_scope(job, "comprehensive")

    extract_url = "https://example.test/article.html"
    source_id = _seed_source(
        job,
        url=extract_url,
        body="An article with a Twitter citation.",
        kind="web",
    )

    yaml_payload = (
        "```yaml\n"
        '- claim: "Per https://twitter.com/foo/status/123 the source confirms it."\n'
        '  confidence: 0.9\n'
        '  quote: ""\n'
        '  tags: [social]\n'
        "```\n"
    )
    router = _StubRouter(yaml_payload)
    task = {"payload": {"source_id": source_id, "sub_question": "Confirms?"}}

    result = await _run_extract_findings(job, task, router=router)

    follow_ups = result.get("follow_up_tasks") or []
    assert follow_ups == []


def test_second_order_fanout_helpers_normalize_and_block() -> None:
    """Spot-check the URL normalizer + blocklist matcher for trailing punctuation."""
    from research_agent.orchestrator.loop import (
        _is_url_blocked,
        _normalize_url_for_fanout,
    )

    assert (
        _normalize_url_for_fanout("https://Example.com/path).")
        == "https://example.com/path"
    )
    assert _normalize_url_for_fanout("ftp://nope/x") is None
    assert _normalize_url_for_fanout("not-a-url") is None
    assert _is_url_blocked(
        "https://mobile.twitter.com/foo", {"twitter.com"}
    ) is True
    assert _is_url_blocked("https://example.com/x", {"twitter.com"}) is False


@pytest.mark.asyncio
async def test_second_order_fanout_narrow_scope_emits_zero(
    job: Job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``narrow`` scope leaves drill-downs to the planner — no auto fan-out."""
    import research_agent.orchestrator.loop as _loop_mod

    _reset_blocklist_cache()
    monkeypatch.setattr(_loop_mod, "_load_url_blocklist", lambda: set())

    _persist_plan_with_full_scope(job, "narrow")

    extract_url = "https://example.test/article.html"
    source_id = _seed_source(
        job,
        url=extract_url,
        body="A narrow-scope body.",
        kind="web",
    )

    yaml_payload = (
        "```yaml\n"
        '- claim: "See https://primary.example/doc.pdf for the underlying record."\n'
        '  confidence: 0.95\n'
        '  quote: ""\n'
        '  tags: [primary]\n'
        "```\n"
    )
    router = _StubRouter(yaml_payload)
    task = {"payload": {"source_id": source_id, "sub_question": "?"}}

    result = await _run_extract_findings(job, task, router=router)

    assert (result.get("follow_up_tasks") or []) == []
