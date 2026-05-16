# HTTP API

The HTTP surface is a thin FastAPI wrapper around the stable programmatic API
in `research_agent.api`. It is lifecycle-only: it starts and manages research
jobs, reads job outputs, searches indexed findings, and exports job artifacts.
Connector tool-level calls remain MCP-only unless a later issue asks for HTTP
connector endpoints.

## Install

```bash
pip install -e ".[http]"
```

The `http` extra installs FastAPI and an ASGI server without adding FastAPI to
the default package install.

## Run

```bash
uvicorn research_agent.http.server:app --host 127.0.0.1 --port 8765
```

OpenAPI docs are available at `http://127.0.0.1:8765/docs`.

## Authentication

There is no built-in authentication in this initial wrapper. The issue was
captured before a concrete non-Python, non-MCP consumer existed, so the auth
model is intentionally not invented here. Run the server on loopback or behind
a trusted local boundary. When a real consumer needs HTTP access, wire its
chosen auth scheme into the FastAPI dependency in
`research_agent.http.server`.

## Status Codes

- `200` - request succeeded.
- `400` - invalid goal, search query, resume/export options, or other
  `InvalidGoal` input.
- `404` - requested job, report, or exportable artifact was not found.
- `409` - job is already running.
- `422` - request body or query parameters failed FastAPI/Pydantic validation.
- `500` - unexpected server error; response body is redacted to
  `{"detail": "internal server error"}`.

## Endpoints

| Method | Path | B2 function | Request | Response |
|---|---|---|---|---|
| `POST` | `/jobs` | `start_job` | `StartJobRequest` | `StartJobResult` |
| `GET` | `/jobs` | `list_jobs` | optional `status` query | `list[JobSummary]` |
| `GET` | `/jobs/{job_id}/status` | `get_job_status` | path `job_id` | `JobStatus` |
| `POST` | `/jobs/{job_id}/stop` | `stop_job` | `StopJobRequest` | `StopJobResult` |
| `POST` | `/jobs/{job_id}/resume` | `resume_job` | `ResumeJobRequest` | `ResumeJobResult` |
| `GET` | `/jobs/{job_id}/report` | `get_report` | path `job_id` | `ReportResult` |
| `GET` | `/jobs/{job_id}/findings` | `get_findings` | path `job_id` | `list[FindingResult]` |
| `POST` | `/findings/search` | `search_findings` | `SearchFindingsRequest` | `list[SearchFindingResult]` |
| `POST` | `/jobs/{job_id}/export` | `export_job` | `ExportJobRequest` | `ExportResult` |

`ReportResult.sources` reuses `research_agent.tools.models.Source`; connector
shapes are not duplicated in the HTTP layer.

## Examples

Start a job:

```bash
curl -sS -X POST http://127.0.0.1:8765/jobs \
  -H 'content-type: application/json' \
  -d '{"goal":"Investigate Widget Co","budget_usd":1.0,"time_cap":2}'
```

Read status:

```bash
curl -sS http://127.0.0.1:8765/jobs/2026-05-16-investigate-widget-co/status
```

Search findings:

```bash
curl -sS -X POST http://127.0.0.1:8765/findings/search \
  -H 'content-type: application/json' \
  -d '{"query":"Widget Co","kind":"both","fts_only":true}'
```

Export a markdown bundle:

```bash
curl -sS -X POST \
  http://127.0.0.1:8765/jobs/2026-05-16-investigate-widget-co/export \
  -H 'content-type: application/json' \
  -d '{"md_bundle":true,"out":"exports/widget-co.md"}'
```
