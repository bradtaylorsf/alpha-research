"""Tests for the optional FastAPI HTTP wrapper."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from research_agent.errors import InvalidGoal, JobAlreadyRunning  # noqa: E402
from research_agent.http import server  # noqa: E402
from research_agent.storage import db  # noqa: E402

SAMPLE_JOB_ID = "2026-05-16-investigate-widget-co-financials"
FIXTURE_JOBS_ROOT = Path("tests/fixtures/jobs")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.sqlite"
    db.migrate(path=path).close()
    return path


def test_http_lifecycle_routes_delegate_to_programmatic_api(
    tmp_path: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs_root = tmp_path / "jobs"
    monkeypatch.setattr(server.public_api.daemon, "spawn_daemon", lambda _job_id, **_kw: 4242)
    monkeypatch.setattr(server.public_api.daemon, "is_daemon_alive", lambda *_a, **_kw: False)
    client = TestClient(server.create_app(jobs_root=jobs_root, db_path=db_path))

    started = client.post(
        "/jobs",
        json={"goal": "Investigate HTTP lifecycle", "budget_usd": 1.0},
    )
    assert started.status_code == 200
    started_data = started.json()
    assert started_data["daemon_pid"] == 4242
    job_id = started_data["job_id"]

    listed = client.get("/jobs")
    assert listed.status_code == 200
    assert listed.json()[0]["job_id"] == job_id

    status = client.get(f"/jobs/{job_id}/status")
    assert status.status_code == 200
    assert status.json()["job_id"] == job_id

    stopped = client.post(f"/jobs/{job_id}/stop", json={"graceful": True})
    assert stopped.status_code == 200
    assert stopped.json() == {"stopped": True}
    assert (jobs_root / job_id / "STOP").exists()

    resumed = client.post(f"/jobs/{job_id}/resume", json={})
    assert resumed.status_code == 200
    assert resumed.json() == {"resumed": True, "daemon_pid": 4242}
    assert not (jobs_root / job_id / "STOP").exists()


def test_http_read_search_and_export_routes_use_fixture_job(
    tmp_path: Path,
    db_path: Path,
) -> None:
    client = TestClient(server.create_app(jobs_root=FIXTURE_JOBS_ROOT, db_path=db_path))

    report = client.get(f"/jobs/{SAMPLE_JOB_ID}/report")
    assert report.status_code == 200
    assert report.json()["report_md"].startswith("# Report")
    assert report.json()["sources"][0]["source_kind"] == "web"

    findings = client.get(f"/jobs/{SAMPLE_JOB_ID}/findings")
    assert findings.status_code == 200
    assert findings.json()[0]["citations"] == [1]

    hits = client.post(
        "/findings/search",
        json={"query": "Widget Co", "job_id": SAMPLE_JOB_ID, "fts_only": True},
    )
    assert hits.status_code == 200
    assert hits.json()[0]["job_id"] == SAMPLE_JOB_ID

    out = tmp_path / "fixture.md"
    exported = client.post(
        f"/jobs/{SAMPLE_JOB_ID}/export",
        json={"md_bundle": True, "out": str(out)},
    )
    assert exported.status_code == 200
    assert exported.json()["bytes"] > 0
    assert out.read_text(encoding="utf-8").startswith("# Report")


def test_http_maps_public_api_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(server.create_app(), raise_server_exceptions=False)

    monkeypatch.setattr(
        server.public_api,
        "start_job",
        lambda **_kwargs: (_ for _ in ()).throw(InvalidGoal("bad goal")),
    )
    invalid = client.post("/jobs", json={"goal": ""})
    assert invalid.status_code == 400
    assert invalid.json() == {"detail": "bad goal"}

    monkeypatch.setattr(
        server.public_api,
        "resume_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(JobAlreadyRunning("busy")),
    )
    conflict = client.post("/jobs/job-1/resume", json={})
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "busy"}

    missing = client.get("/jobs/missing/report")
    assert missing.status_code == 404

    monkeypatch.setattr(
        server.public_api,
        "list_jobs",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("secret token")),
    )
    failed = client.get("/jobs")
    assert failed.status_code == 500
    assert failed.json() == {"detail": "internal server error"}
