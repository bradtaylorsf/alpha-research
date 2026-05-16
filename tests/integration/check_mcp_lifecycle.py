"""Spawn research-mcp over stdio and smoke lifecycle tools."""

from __future__ import annotations

import shutil
import sys

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

EXPECTED = {
    "start_research_job",
    "get_job_status",
    "list_jobs",
    "stop_job",
    "resume_job",
    "get_report",
    "get_findings",
    "search_findings",
    "export_job",
}


async def _main() -> None:
    executable = shutil.which("research-mcp")
    if executable:
        params = StdioServerParameters(command=executable)
    else:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "research_agent.mcp.server"],
        )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            missing = EXPECTED - names
            assert not missing, f"missing tools: {sorted(missing)}"

            result = await session.call_tool("list_jobs", {})
            assert result.structuredContent is not None
            assert isinstance(result.structuredContent.get("jobs"), list)
            print("OK mcp lifecycle", len(names), "tools")


if __name__ == "__main__":
    anyio.run(_main)
