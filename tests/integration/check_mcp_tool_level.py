"""Spawn research-mcp and smoke registry-driven connector tools."""

from __future__ import annotations

import os
import sys

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def _main() -> None:
    import research_agent.tools  # noqa: F401 - populate local registry
    from research_agent.tools._registry import iter_kinds

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "research_agent.mcp.server"],
        env={**os.environ, "MUCKWIRE_MCP_TEST_FAKE_CONNECTOR": "1"},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            missing = {entry.name for entry in iter_kinds()} - names
            assert not missing, f"missing connector tools: {sorted(missing)}"
            assert "fake_search" in names

            result = await session.call_tool(
                "fake_search",
                {"query": "fixture", "sub_question": "fixture", "max_results": 1},
            )
            assert result.structuredContent is not None
            rows = result.structuredContent.get("results")
            assert isinstance(rows, list) and rows
            assert {"url", "title", "snippet", "source_kind"} <= set(rows[0])
            print("OK mcp tool-level", len(names), "tools")


if __name__ == "__main__":
    anyio.run(_main)
