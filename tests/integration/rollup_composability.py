"""Rollup smoke for the composable research service MCP surface.

Default mode is fixture-backed and deterministic: it spawns the real MCP
server in a temporary workspace, reads a materialized fixture job through MCP,
exports it, and calls one registry-driven connector tool. Pass ``--live`` to
exercise ``start_research_job`` against the configured daemon/model stack.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SAMPLE_JOB_ID = "2026-05-16-investigate-widget-co-financials"
FIXTURE_JOB_ROOT = Path("tests/fixtures/jobs/sample")
LIFECYCLE_TOOLS = {
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


def _structured(result: Any) -> dict[str, Any]:
    data = getattr(result, "structuredContent", None)
    if not isinstance(data, dict):
        raise AssertionError(f"MCP result did not include structured content: {result!r}")
    return data


async def _spawn_session(workdir: Path, *, fake_connector: bool) -> ClientSession:
    env = dict(os.environ)
    if fake_connector:
        env["MUCKWIRE_MCP_TEST_FAKE_CONNECTOR"] = "1"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "research_agent.mcp.server"],
        cwd=str(workdir),
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _assert_tools(session: ClientSession, *, expect_fake: bool) -> set[str]:
    tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}
    missing = LIFECYCLE_TOOLS - names
    assert not missing, f"missing lifecycle tools: {sorted(missing)}"
    connector_tools = {name for name in names if name.endswith("_search")}
    assert connector_tools, "no registry-driven connector tools exposed"
    if expect_fake:
        assert "fake_search" in names
    return names


async def _run_fixture_rollup(workdir: Path) -> None:
    jobs_dir = workdir / "jobs"
    jobs_dir.mkdir(parents=True)
    shutil.copytree(FIXTURE_JOB_ROOT, jobs_dir / "sample")
    export_path = workdir / "exports" / "fixture.md"

    async for session in _spawn_session(workdir, fake_connector=True):
        names = await _assert_tools(session, expect_fake=True)
        assert len(names) >= len(LIFECYCLE_TOOLS) + 1

        listed = _structured(await session.call_tool("list_jobs", {}))
        jobs = listed.get("jobs")
        assert isinstance(jobs, list) and jobs, "list_jobs returned no jobs"
        assert any(row.get("job_id") == SAMPLE_JOB_ID for row in jobs), jobs

        report = _structured(await session.call_tool("get_report", {"job_id": SAMPLE_JOB_ID}))
        assert str(report.get("report_md") or "").startswith("# Report")
        assert isinstance(report.get("sources"), list) and report["sources"]

        findings = _structured(await session.call_tool("get_findings", {"job_id": SAMPLE_JOB_ID}))
        assert isinstance(findings.get("findings"), list) and findings["findings"]

        exported = _structured(
            await session.call_tool(
                "export_job",
                {
                    "job_id": SAMPLE_JOB_ID,
                    "md_bundle": True,
                    "out": str(export_path),
                },
            )
        )
        assert exported["bytes"] > 0
        assert export_path.read_text(encoding="utf-8").startswith("# Report")

        connector = _structured(
            await session.call_tool(
                "fake_search",
                {"query": "fixture", "sub_question": "fixture", "max_results": 1},
            )
        )
        rows = connector.get("results")
        assert isinstance(rows, list) and rows, "fake_search returned no rows"
        assert {"url", "title", "snippet", "source_kind"} <= set(rows[0])


async def _run_live_rollup(workdir: Path) -> None:
    async for session in _spawn_session(workdir, fake_connector=False):
        names = await _assert_tools(session, expect_fake=False)
        assert "web_search" in names, "live rollup expects free web_search connector"

        started = _structured(
            await session.call_tool(
                "start_research_job",
                {
                    "goal": "A 100-word brief on the WPA Federal Writers Project",
                    "budget_usd": 0.10,
                    "time_cap": 1,
                },
            )
        )
        job_id = started["job_id"]
        deadline = time.monotonic() + 900
        while True:
            status = _structured(await session.call_tool("get_job_status", {"job_id": job_id}))
            if status.get("status") in {"completed", "failed", "stopped"}:
                break
            if time.monotonic() > deadline:
                raise AssertionError(f"job {job_id} did not finish before timeout: {status}")
            await anyio.sleep(5)
        assert status["status"] == "completed", status

        report = _structured(await session.call_tool("get_report", {"job_id": job_id}))
        assert len(str(report.get("report_md") or "").strip()) > 100

        web_rows = _structured(
            await session.call_tool(
                "web_search",
                {
                    "query": "WPA Federal Writers Project",
                    "sub_question": "WPA Federal Writers Project",
                    "max_results": 1,
                },
            )
        ).get("results")
        assert isinstance(web_rows, list) and web_rows, "web_search returned no rows"

        export_path = workdir / "exports" / f"{job_id}.md"
        exported = _structured(
            await session.call_tool(
                "export_job",
                {"job_id": job_id, "md_bundle": True, "out": str(export_path)},
            )
        )
        assert exported["bytes"] > 0
        assert export_path.read_text(encoding="utf-8").strip()


async def _main(live: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="muckwire-composability-") as raw:
        workdir = Path(raw)
        (workdir / "data").mkdir()
        if live:
            await _run_live_rollup(workdir)
        else:
            await _run_fixture_rollup(workdir)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run a real daemon-backed MCP job instead of the deterministic fixture rollup.",
    )
    args = parser.parse_args()
    anyio.run(_main, args.live)


if __name__ == "__main__":
    main()
