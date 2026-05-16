"""Unit tests for the research-mcp lifecycle surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp import types
from mcp.shared.exceptions import McpError

from research_agent.mcp import server as mcp_server


@pytest.mark.asyncio
async def test_lifecycle_tools_are_listed() -> None:
    tools = mcp_server.list_tool_definitions()
    names = {tool.name for tool in tools}

    assert set(mcp_server.LIFECYCLE_TOOLS) <= names
    assert "start_research_job" in names
    assert all(tool.inputSchema for tool in tools)
    assert all(tool.outputSchema for tool in tools)


@pytest.mark.asyncio
async def test_list_jobs_returns_structured_shape_and_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = await mcp_server.call_tool("list_jobs", {})

    assert result == {"jobs": []}
    event_files = list((tmp_path / "data" / "mcp_events").glob("*.jsonl"))
    assert event_files
    event = json.loads(event_files[0].read_text(encoding="utf-8").splitlines()[0])
    assert event["tool"] == "list_jobs"
    assert event["ok"] is True


@pytest.mark.asyncio
async def test_bad_lifecycle_input_raises_mcp_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(McpError) as excinfo:
        await mcp_server.call_tool("get_report", {})

    assert excinfo.value.error.code == types.INVALID_PARAMS
