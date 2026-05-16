"""Tests for registry-driven MCP connector tools."""

from __future__ import annotations

import pytest
from mcp import types
from mcp.shared.exceptions import McpError
from pydantic import ConfigDict

from research_agent.mcp import server as mcp_server
from research_agent.tools import _registry
from research_agent.tools._registry import BaseSearchPayload, KindEntry
from research_agent.tools.models import SearchResult


class FakePayload(BaseSearchPayload):
    model_config = ConfigDict(extra="ignore")

    max_results: int | None = None


async def fake_search(query: str, *, max_results: int | None = None) -> list[SearchResult]:
    return [
        SearchResult(
            url="https://example.com/fake",
            title=f"Fake: {query}",
            snippet="fake result",
            source_kind="web",
            score=1.0,
            extras={"max_results": max_results},
        )
    ]


@pytest.fixture
def fake_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = KindEntry(
        name="fake_search",
        payload_schema=FakePayload,
        search_fn=fake_search,
        fetch_fn=None,
        host_patterns=(),
        skill_name=None,
        description="Fake connector",
        optional_payload_knobs="max_results",
        example_query="fixture",
        module_name="fake",
    )
    monkeypatch.setitem(_registry._REGISTRY, "fake_search", entry)  # noqa: SLF001


def test_registered_connector_appears_without_server_edit(fake_kind: None) -> None:
    tools = mcp_server.list_tool_definitions()
    by_name = {tool.name: tool for tool in tools}

    assert "fake_search" in by_name
    assert by_name["fake_search"].description == "Fake connector"
    assert by_name["fake_search"].inputSchema["properties"]["query"]["type"] == "string"
    results_schema = by_name["fake_search"].outputSchema["properties"]["results"]
    assert results_schema["type"] == "array"
    assert results_schema["items"]["$ref"] == "#/$defs/SearchResult"


@pytest.mark.asyncio
async def test_connector_tool_dispatch_returns_search_results(
    fake_kind: None,
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)

    result = await mcp_server.call_tool(
        "fake_search",
        {"query": "fixture query", "sub_question": "fixture query", "max_results": 1},
    )

    assert result["results"]
    assert result["results"][0]["title"] == "Fake: fixture query"


@pytest.mark.asyncio
async def test_cost_connector_missing_key_is_invalid_params(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SERPAPI_KEY", raising=False)

    with pytest.raises(McpError) as excinfo:
        await mcp_server.call_tool(
            "scholar_search",
            {"query": "Section 230", "sub_question": "Section 230"},
        )

    assert excinfo.value.error.code == types.INVALID_PARAMS
    assert "SERPAPI_KEY" in excinfo.value.error.message
