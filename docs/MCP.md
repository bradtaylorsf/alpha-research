# MCP

`research-mcp` exposes muckwire over the Model Context Protocol using stdio.
It is built on `mcp.server.lowlevel.Server` so lifecycle tools and connector
tools share one dynamic tool list.

## Install

```bash
pip install -e .
research-mcp
```

The server writes telemetry to `data/mcp_events/<pid>.jsonl`. Each line names
the tool, process id, success flag, error text when present, timestamp, and
schema version.

## Lifecycle Tools

- `start_research_job` - wraps `research_agent.start_job`; accepts `goal`,
  optional `budget_usd`, `time_cap`, `local`, and `fresh_reset`; returns
  `job_id` and `daemon_pid`.
- `get_job_status` - wraps `get_job_status`; returns status, spend, elapsed
  time, current iteration, and latest event summary.
- `list_jobs` - wraps `list_jobs`; returns `jobs`.
- `stop_job` - wraps `stop_job`; returns `stopped`.
- `resume_job` - wraps `resume_job`; returns `resumed` and `daemon_pid`.
- `get_report` - wraps `get_report`; returns `report_md` and `sources`.
- `get_findings` - wraps `get_findings`; returns `findings`.
- `search_findings` - wraps `search_findings`; returns `results`.
- `export_job` - wraps `export_job`; returns output `path` and byte count.

Lifecycle calls use the same programmatic API as the CLI. A
`start_research_job` request respects the job's `budget_usd`; once the daemon
is running, normal LLM calls continue through that job's `BudgetTracker`.

## Claude Code

```json
{
  "mcpServers": {
    "muckwire": {
      "command": "research-mcp",
      "args": []
    }
  }
}
```

## Claude Agent SDK

```json
{
  "mcp_servers": {
    "muckwire": {
      "type": "stdio",
      "command": "research-mcp",
      "args": []
    }
  }
}
```

## Cowork

```json
{
  "servers": {
    "muckwire": {
      "transport": "stdio",
      "command": "research-mcp"
    }
  }
}
```

## Tool-Level Surface

The tool-level connector surface is registered from
`research_agent.tools._registry.iter_kinds()`. Adding a connector kind to the
registry exposes a matching MCP tool without editing `mcp/server.py`.

Tool-level calls are one-shot connector lookups and do not have an active job
context, so they do not use `BudgetTracker` and do not enforce a per-job
budget. Cost-bearing connector tools refuse to dispatch unless their required
credential is configured; the MCP consumer is responsible for any additional
budget gate at the agent layer.

Connector tool output is structured as an MCP object with a `results` field;
`results` is serialized as `list[SearchResult]` from `research_agent.tools.models`.
The object wrapper is intentional because the Python MCP SDK only validates
object-valued `structuredContent`.
