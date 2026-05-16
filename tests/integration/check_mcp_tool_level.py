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
        env=dict(os.environ),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            missing = {entry.name for entry in iter_kinds()} - names
            assert not missing, f"missing connector tools: {sorted(missing)}"
            assert "loc_search" in names

            result = await session.call_tool(
                "loc_search",
                {
                    "query": "WPA Federal Writers Project",
                    "sub_question": "WPA Federal Writers Project",
                    "max_results": 1,
                },
            )
            assert result.structuredContent is not None
            rows = result.structuredContent.get("results")
            assert isinstance(rows, list) and rows
            assert {"url", "title", "snippet", "source_kind"} <= set(rows[0])
            assert "Writers" in rows[0]["title"] or "Writers" in rows[0]["snippet"]
            print("OK mcp tool-level loc_search", len(names), "tools")


if __name__ == "__main__":
    anyio.run(_main)
