"""Fixture-backed regression backtest for 2026 candidate roster jobs."""

from __future__ import annotations

import csv
import json
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from research_agent.orchestrator import synth as synth_module
from research_agent.orchestrator.loop import default_handlers, run_loop
from research_agent.orchestrator.plan import Plan, Subgoal, TaskSpec
from research_agent.storage import artifacts, coverage, db
from research_agent.storage.jobs import Job
from research_agent.storage.markdown import write_plan
from research_agent.storage.tasks import enqueue
from research_agent.tools import fec, state_election

GOAL = (
    "create a sourced state-by-state roster of 2026 U.S. House and Senate "
    "candidates with CSV output"
)
REQUIRED_COLUMNS = {
    "state",
    "chamber",
    "district_or_seat",
    "candidate_name",
    "party",
    "candidate_status",
    "confidence",
    "official_campaign_website",
    "source_url",
}
TERMINAL_COVERAGE = {"complete", "not_yet_public", "confirmed_gap"}


class _StubAgent:
    def __init__(
        self,
        model: Any,
        *,
        output_type: Any = None,
        system_prompt: str | None = None,
    ) -> None:
        self.model = model
        self.output_type = output_type
        self.system_prompt = system_prompt


class _FailingSynthRouter:
    tiers = {
        "frontier": {"provider": "openrouter", "model": "fixture-primary"},
        "frontier_speed": {"provider": "openrouter", "model": "fixture-fallback"},
    }

    def __init__(self) -> None:
        self.budget = SimpleNamespace(last_cost=0.0)
        self.calls: list[str] = []

    def model_for(self, tier: str) -> Any:
        return SimpleNamespace(tier=tier)

    async def call(self, tier: str, _agent: Any, *_args: Any, **_kwargs: Any) -> Any:
        self.calls.append(tier)
        raise RuntimeError(f"fixture synthesis HTTP 400 for {tier}")


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, fixture_root: Path) -> None:
    payloads = json.loads(
        (fixture_root / "fec" / "candidate_enumeration_2026.json").read_text(
            encoding="utf-8"
        )
    )
    state_csv_url = "https://fixtures.example.test/co_2026.csv"
    state_csv_text = (fixture_root / "state_election" / "co_2026.csv").read_text(
        encoding="utf-8"
    )

    class _Response:
        status_code = 200

        def __init__(self, payload: dict[str, Any] | None = None, text: str = "") -> None:
            self._payload = payload
            self.text = text

        def json(self) -> dict[str, Any]:
            return self._payload or {}

    @asynccontextmanager
    async def _client_factory(*_args: Any, **_kwargs: Any):
        class _Client:
            async def get(
                self,
                url: str,
                *,
                params: dict[str, Any] | None = None,
                **_kwargs: Any,
            ) -> _Response:
                if url == state_csv_url:
                    return _Response(text=state_csv_text)
                params = params or {}
                state = str(params.get("state") or "").upper()
                office = str(params.get("office") or "").upper()
                district = str(params.get("district") or "").strip()
                key = "-".join(part for part in (state, office, district) if part)
                empty = {"pagination": {"page": 1, "pages": 1, "count": 0}, "results": []}
                return _Response(payloads.get(key, empty))

        yield _Client()

    monkeypatch.setenv("DATA_GOV_API_KEY", "test-key-1234567890abcdef")
    fec.reset_for_tests()
    monkeypatch.setattr(fec.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(fec.httpx, "AsyncClient", _client_factory)


def _patch_state_election(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://fixtures.example.test/co_2026.csv"

    monkeypatch.setattr(
        state_election,
        "_RECIPES",
        {
            "CO": {
                "source_url": url,
                "source_type": "csv",
                "retrieval_method": "static_fetch",
                "cycle_coverage": [2026],
            }
        },
    )


def _make_job(tmp_path: Path) -> tuple[Job, Plan]:
    db_path = tmp_path / "index.sqlite"
    db.migrate(path=db_path).close()
    job = Job.create(
        {
            "goal": GOAL,
            "domain": "political",
            "enumeration": {
                "required": True,
                "coverage_units": [
                    {
                        "state": "CA",
                        "chamber": "House",
                        "district_or_seat": "01",
                        "source_type": "fec-filed",
                    },
                    {"state": "FL", "chamber": "Senate", "source_type": "fec-filed"},
                    {
                        "state": "CO",
                        "chamber": "House",
                        "district_or_seat": "12",
                        "source_type": "state-ballot-qualified",
                    },
                    {
                        "state": "MD",
                        "chamber": "House",
                        "source_type": "state-ballot-qualified",
                    },
                ],
            },
        },
        jobs_root=tmp_path / "jobs",
        db_path=db_path,
        today=date(2026, 5, 15),
    )
    plan = Plan(
        version=1,
        objective="Build a complete 2026 federal candidate roster fixture.",
        subgoals=[
            Subgoal(
                id=1,
                description="Enumerate FEC and state-election candidate rows",
                done=True,
            )
        ],
        task_template=[],
        expected_iterations=1,
        scope_class="broad",
    )
    write_plan(job, plan.model_dump())
    coverage.declare_from_intake(job)
    enqueue(
        job,
        [
            TaskSpec(
                kind="fec_search",
                payload={
                    "query": "",
                    "kind": "candidates_enumerate",
                    "cycle": 2026,
                    "office": "H",
                    "state": "CA",
                    "district": "01",
                    "max_rows": 25,
                    "expand_top_k": 0,
                },
            ),
            TaskSpec(
                kind="fec_search",
                payload={
                    "query": "",
                    "kind": "candidates_enumerate",
                    "cycle": 2026,
                    "office": "S",
                    "state": "FL",
                    "max_rows": 25,
                    "expand_top_k": 0,
                },
            ),
            TaskSpec(
                kind="state_election_search",
                payload={
                    "query": "House",
                    "cycle": 2026,
                    "state": "CO",
                    "office": "House",
                    "expand_top_k": 0,
                },
            ),
            TaskSpec(
                kind="state_election_search",
                payload={
                    "query": "2026 U.S. House candidates",
                    "cycle": 2026,
                    "state": "MD",
                    "office": "House",
                    "source_type": "state-ballot-qualified",
                    "empty_coverage_status": "confirmed_gap",
                    "empty_coverage_reason": "fixture intentionally lacks Maryland source",
                    "unblocker": "Refresh Maryland SBE fixture after filing lists publish",
                    "expand_top_k": 0,
                },
            ),
        ],
        plan_version=1,
    )
    return job, plan


@pytest.mark.asyncio
async def test_candidate_roster_fixture_backtest_completes_or_gaps_honestly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_root = Path(__file__).parent / "fixtures"
    _patch_httpx(monkeypatch, fixture_root)
    _patch_state_election(monkeypatch)
    monkeypatch.setattr(synth_module, "Agent", _StubAgent)
    job, plan = _make_job(tmp_path)

    result = await run_loop(
        job,
        router=_FailingSynthRouter(),
        plan=plan,
        handlers=default_handlers(_FailingSynthRouter()),
        max_tasks=20,
        retry_waits=(0,),
    )
    synth_out = await synth_module.final_synthesis(
        job,
        plan,
        router=_FailingSynthRouter(),
    )

    listed = artifacts.list_artifacts(job)
    assert [item["name"] for item in listed] == ["candidates"]
    csv_path = job.root / listed[0]["csv_path"]
    assert csv_path.exists()

    schema, rows = artifacts.read_artifact(job, "candidates")
    assert REQUIRED_COLUMNS.issubset({column.name for column in schema.columns})
    assert REQUIRED_COLUMNS.issubset(rows[0].keys())
    assert len(rows) == 3
    assert {row["candidate_name"] for row in rows} == {
        "DOE, JANE",
        "SMITH, ROBERT",
        "Ana Candidate",
    }
    assert all(row["party"] for row in rows)
    assert all(row["source_url"].startswith("http") for row in rows)
    assert not any("portal" in row["candidate_name"].lower() for row in rows)

    csv_rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(csv_rows) == len(rows)
    assert REQUIRED_COLUMNS.issubset(csv_rows[0].keys())

    units = coverage.list_units(job)
    statuses = {unit.status for unit in units}
    assert statuses <= TERMINAL_COVERAGE
    assert "pending" not in statuses
    assert any(unit.status == "confirmed_gap" for unit in units)
    assert result["completed"] is False
    assert result["completion_reason"] == "confirmed_gap"

    assert synth_out.model == "deterministic_fallback"
    report = (job.root / "report.md").read_text(encoding="utf-8")
    assert "## Artifacts" in report
    assert "[CSV](artifacts/candidates.csv)" in report
    assert "## Confirmed Gaps" in report
    assert "Maryland SBE fixture" in report
