"""Lowlevel MCP stdio server for research-agent lifecycle tools."""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_agent import __version__, config
from research_agent import api as public_api
from research_agent.errors import InvalidGoal, JobAlreadyRunning, JobNotFound
from research_agent.skills.loader import load_skill
from research_agent.tools._errors import MissingCredentialError
from research_agent.tools.models import SearchResult

SERVER_NAME = "research-mcp"
MCP_EVENTS_DIR = Path("data/mcp_events")


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartResearchJobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    budget_usd: float | None = None
    time_cap: int | None = None
    local: bool = False
    fresh_reset: bool = False


class JobIdInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str


class StopJobInput(JobIdInput):
    graceful: bool = True


class ResumeJobInput(JobIdInput):
    force: bool = False
    replan: bool = False
    note: str | None = None


class SearchFindingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    job_id: str | None = None
    kind: str = "both"
    fts_only: bool = True


class ExportJobInput(JobIdInput):
    zip: bool = False
    md_bundle: bool = True
    out: str | None = None
    include_history: bool = False


class StartResearchJobOutput(BaseModel):
    job_id: str
    daemon_pid: int


class JobStatusOutput(BaseModel):
    status: str
    spent_usd: float
    time_elapsed: int | None
    current_iteration: int
    last_event_summary: str | None = None


class ListJobsOutput(BaseModel):
    jobs: list[dict[str, Any]] = Field(default_factory=list)


class StopJobOutput(BaseModel):
    stopped: bool


class ResumeJobOutput(BaseModel):
    resumed: bool
    daemon_pid: int


class ReportOutput(BaseModel):
    report_md: str
    sources: list[dict[str, Any]] = Field(default_factory=list)


class FindingsOutput(BaseModel):
    findings: list[dict[str, Any]] = Field(default_factory=list)


class SearchOutput(BaseModel):
    results: list[dict[str, Any]] = Field(default_factory=list)


class ExportOutput(BaseModel):
    path: str
    bytes: int


class SearchResultsOutput(BaseModel):
    results: list[dict[str, Any]] = Field(default_factory=list)


class ToolSpec(BaseModel):
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]

    model_config = ConfigDict(arbitrary_types_allowed=True)


LIFECYCLE_TOOLS: dict[str, ToolSpec] = {
    "start_research_job": ToolSpec(
        name="start_research_job",
        description="Start a full research job and return its job id and daemon pid.",
        input_model=StartResearchJobInput,
        output_model=StartResearchJobOutput,
    ),
    "get_job_status": ToolSpec(
        name="get_job_status",
        description="Return lifecycle status, spend, elapsed time, and latest event summary.",
        input_model=JobIdInput,
        output_model=JobStatusOutput,
    ),
    "list_jobs": ToolSpec(
        name="list_jobs",
        description="List known research jobs newest first.",
        input_model=EmptyInput,
        output_model=ListJobsOutput,
    ),
    "stop_job": ToolSpec(
        name="stop_job",
        description="Request a graceful stop or kill a job daemon.",
        input_model=StopJobInput,
        output_model=StopJobOutput,
    ),
    "resume_job": ToolSpec(
        name="resume_job",
        description="Restart a stranded job daemon.",
        input_model=ResumeJobInput,
        output_model=ResumeJobOutput,
    ),
    "get_report": ToolSpec(
        name="get_report",
        description="Read the current report.md and materialized sources for a job.",
        input_model=JobIdInput,
        output_model=ReportOutput,
    ),
    "get_findings": ToolSpec(
        name="get_findings",
        description="Read finding files from a job folder.",
        input_model=JobIdInput,
        output_model=FindingsOutput,
    ),
    "search_findings": ToolSpec(
        name="search_findings",
        description="Search findings and sources across job folders or the SQLite index.",
        input_model=SearchFindingsInput,
        output_model=SearchOutput,
    ),
    "export_job": ToolSpec(
        name="export_job",
        description="Export a job as a markdown bundle or zip archive.",
        input_model=ExportJobInput,
        output_model=ExportOutput,
    ),
}


def _mcp_error(code: int, message: str) -> McpError:
    return McpError(types.ErrorData(code=code, message=message))


def _emit_mcp_event(tool: str, *, ok: bool, error: str | None = None) -> None:
    MCP_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    line = {
        "ts": int(time.time()),
        "pid": os.getpid(),
        "tool": tool,
        "ok": ok,
        "error": error,
        "schema_version": 1,
    }
    path = MCP_EVENTS_DIR / f"{os.getpid()}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


def list_tool_definitions() -> list[types.Tool]:
    tools = [
        types.Tool(
            name=spec.name,
            description=spec.description,
            inputSchema=_schema(spec.input_model),
            outputSchema=_schema(spec.output_model),
        )
        for spec in LIFECYCLE_TOOLS.values()
    ]
    tools.extend(_connector_tool_definitions())
    return tools


def _connector_tool_definitions() -> list[types.Tool]:
    import research_agent.tools  # noqa: F401 - populate registry
    from research_agent.tools._registry import iter_kinds

    _maybe_register_fake_connector_for_tests()
    tools: list[types.Tool] = []
    for entry in iter_kinds():
        description = ""
        if entry.skill_name:
            with contextlib.redirect_stdout(sys.stderr):
                description = load_skill("connectors", entry.skill_name).strip()
        if not description:
            description = entry.description
        tools.append(
            types.Tool(
                name=entry.name,
                description=description,
                inputSchema=entry.payload_schema.model_json_schema(),
                outputSchema=SearchResultsOutput.model_json_schema(),
            )
        )
    return tools


def _dump_model(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


async def call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    spec = LIFECYCLE_TOOLS.get(name)
    if spec is None:
        return await _call_connector_tool(name, arguments or {})
    try:
        payload = spec.input_model.model_validate(arguments or {})
        result = _dispatch_lifecycle_tool(name, payload)
        _emit_mcp_event(name, ok=True)
        return result
    except ValidationError as exc:
        _emit_mcp_event(name, ok=False, error=str(exc))
        raise _mcp_error(types.INVALID_PARAMS, str(exc)) from exc
    except (InvalidGoal, JobAlreadyRunning, JobNotFound) as exc:
        _emit_mcp_event(name, ok=False, error=str(exc))
        raise _mcp_error(types.INVALID_PARAMS, str(exc)) from exc
    except McpError:
        _emit_mcp_event(name, ok=False, error="mcp_error")
        raise
    except Exception as exc:
        _emit_mcp_event(name, ok=False, error=str(exc))
        raise _mcp_error(types.INTERNAL_ERROR, str(exc)) from exc


async def _call_connector_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    import research_agent.tools  # noqa: F401 - populate registry
    from research_agent.tools._registry import get_kind

    _maybe_register_fake_connector_for_tests()
    entry = get_kind(name)
    if entry is None:
        raise _mcp_error(types.INVALID_PARAMS, f"unknown tool: {name}")
    try:
        _precheck_cost_connector(name)
        if "sub_question" not in arguments and isinstance(arguments.get("query"), str):
            arguments = {**arguments, "sub_question": arguments["query"]}
        payload = entry.payload_schema.model_validate(arguments)
        data = payload.model_dump(exclude_none=True)
        query = str(data.pop("query"))
        data.pop("sub_question", None)
        kwargs = _filter_search_kwargs(entry.search_fn, data)
        raw_results = await entry.search_fn(query, **kwargs)
        results = [
            SearchResult.model_validate(item).model_dump(mode="json")
            for item in (raw_results or [])
        ]
        _emit_mcp_event(name, ok=True)
        return _dump_model(SearchResultsOutput(results=results))
    except ValidationError as exc:
        _emit_mcp_event(name, ok=False, error=str(exc))
        raise _mcp_error(types.INVALID_PARAMS, str(exc)) from exc
    except MissingCredentialError as exc:
        _emit_mcp_event(name, ok=False, error=str(exc))
        raise _mcp_error(types.INVALID_PARAMS, str(exc)) from exc
    except McpError:
        _emit_mcp_event(name, ok=False, error="mcp_error")
        raise
    except Exception as exc:
        _emit_mcp_event(name, ok=False, error=str(exc))
        raise _mcp_error(types.INTERNAL_ERROR, str(exc)) from exc


def _precheck_cost_connector(name: str) -> None:
    if name == "scholar_search" and not (config.get("SERPAPI_KEY") or "").strip():
        raise MissingCredentialError("scholar_search requires SERPAPI_KEY; not configured")
    if name == "linkedin_search":
        broker = (config.get("LINKEDIN_BROKER") or "proxycurl").strip().lower()
        env_var = "LIX_API_KEY" if broker == "lix" else "LINKEDIN_DATA_API_KEY"
        if not (config.get(env_var) or "").strip():
            raise MissingCredentialError(
                f"linkedin_search requires {env_var}; not configured"
            )


def _filter_search_kwargs(fn: Any, payload: dict[str, Any]) -> dict[str, Any]:
    signature = inspect.signature(fn)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
        return payload
    allowed = {
        name
        for name, param in signature.parameters.items()
        if name != "query"
        and param.kind
        in {inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    }
    return {key: value for key, value in payload.items() if key in allowed}


def _maybe_register_fake_connector_for_tests() -> None:
    if os.environ.get("MUCKWIRE_MCP_TEST_FAKE_CONNECTOR") != "1":
        return
    from research_agent.tools._registry import BaseSearchPayload, is_registered, register_kind

    if is_registered("fake_search"):
        return

    class _FakePayload(BaseSearchPayload):
        max_results: int | None = None

    async def _fake_search(query: str, *, max_results: int | None = None) -> list[SearchResult]:
        count = max(1, min(int(max_results or 1), 3))
        return [
            SearchResult(
                url=f"https://example.com/mcp-fake/{idx}",
                title=f"Fake result {idx}: {query}",
                snippet=f"Fixture result for {query}",
                source_kind="web",
                score=1.0,
            )
            for idx in range(1, count + 1)
        ]

    register_kind(
        "fake_search",
        payload_schema=_FakePayload,
        search_fn=_fake_search,
        skill_name=None,
        description="Test-only fake connector for MCP integration smoke tests.",
        optional_payload_knobs="max_results",
        example_query="fixture",
        module_name="fake",
    )


def _dispatch_lifecycle_tool(name: str, payload: BaseModel) -> dict[str, Any]:
    if name == "start_research_job":
        args = payload.model_dump()
        result = public_api.start_job(**args)
        return _dump_model(
            StartResearchJobOutput(job_id=result.job_id, daemon_pid=result.daemon_pid)
        )
    if name == "get_job_status":
        args = JobIdInput.model_validate(payload).model_dump()
        status = public_api.get_job_status(**args)
        return _dump_model(
            JobStatusOutput(
                status=status.status,
                spent_usd=status.spent_usd,
                time_elapsed=status.time_elapsed,
                current_iteration=status.current_iteration,
                last_event_summary=status.last_event_summary,
            )
        )
    if name == "list_jobs":
        jobs = [
            {
                "job_id": item.job_id,
                "goal": item.goal,
                "status": item.status,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in public_api.list_jobs()
        ]
        return _dump_model(ListJobsOutput(jobs=jobs))
    if name == "stop_job":
        args = StopJobInput.model_validate(payload).model_dump()
        result = public_api.stop_job(**args)
        return _dump_model(StopJobOutput(stopped=result.stopped))
    if name == "resume_job":
        args = ResumeJobInput.model_validate(payload).model_dump()
        result = public_api.resume_job(**args)
        return _dump_model(ResumeJobOutput(resumed=result.resumed, daemon_pid=result.daemon_pid))
    if name == "get_report":
        args = JobIdInput.model_validate(payload).model_dump()
        result = public_api.get_report(**args)
        return _dump_model(
            ReportOutput(
                report_md=result.report_md,
                sources=[source.model_dump(mode="json") for source in result.sources],
            )
        )
    if name == "get_findings":
        args = JobIdInput.model_validate(payload).model_dump()
        findings = [item.model_dump(mode="json") for item in public_api.get_findings(**args)]
        return _dump_model(FindingsOutput(findings=findings))
    if name == "search_findings":
        args = SearchFindingsInput.model_validate(payload).model_dump()
        results = [item.model_dump(mode="json") for item in public_api.search_findings(**args)]
        return _dump_model(SearchOutput(results=results))
    if name == "export_job":
        args = ExportJobInput.model_validate(payload).model_dump()
        if args.get("out") is not None:
            args["out"] = Path(str(args["out"]))
        result = public_api.export_job(**args)
        return _dump_model(ExportOutput(path=result.path, bytes=result.bytes))
    raise _mcp_error(types.INVALID_PARAMS, f"unknown tool: {name}")


def create_server() -> Server:
    server = Server(SERVER_NAME, version=__version__)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return list_tool_definitions()

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await call_tool(name, arguments)

    return server


server = create_server()


async def async_main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    anyio.run(async_main)


__all__ = [
    "LIFECYCLE_TOOLS",
    "SERVER_NAME",
    "call_tool",
    "create_server",
    "list_tool_definitions",
    "main",
    "server",
]


if __name__ == "__main__":
    main()
