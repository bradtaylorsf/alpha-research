"""Tests for the optional non-English finding translation pass."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from research_agent.orchestrator.loop import _run_extract_findings
from research_agent.orchestrator.synth import _load_top_findings
from research_agent.skills.loader import load_skill
from research_agent.storage import db
from research_agent.storage.jobs import Job
from research_agent.storage.sources import write_source


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.sqlite"
    db.migrate(path=path).close()
    return path


@pytest.fixture
def jobs_root(tmp_path: Path) -> Path:
    return tmp_path / "jobs"


@pytest.fixture
def french_source_text() -> str:
    return Path("tests/fixtures/translation/french-source.md").read_text(encoding="utf-8")


def _make_job(
    jobs_root: Path,
    db_path: Path,
    *,
    translate_non_english: bool | None = None,
) -> Job:
    intake: dict[str, Any] = {"goal": "Investigate Algerian war archives"}
    if translate_non_english is not None:
        intake["translate_non_english"] = translate_non_english
    return Job.create(intake, jobs_root=jobs_root, db_path=db_path)


def _seed_source(
    job: Job,
    body: str,
    *,
    language_metadata: dict[str, Any],
) -> int:
    return write_source(
        job,
        url="https://gallica.example.test/ark:/12148/bpt6k1",
        title="Rapport sur Alger",
        raw_content=body,
        kind="gallica_search",
        metadata=language_metadata,
    )


class _Budget:
    def __init__(self, *, would_exceed: bool = False) -> None:
        self.would_exceed_result = would_exceed
        self.calls: list[tuple[str, Any]] = []

    def would_exceed(self, tier: str, usage: Any) -> bool:
        self.calls.append((tier, usage))
        return self.would_exceed_result


class _Router:
    def __init__(
        self,
        *,
        extract_output: str | None = None,
        translation_output: str = "The prefect reported intensified police operations.",
        budget: _Budget | None = None,
    ) -> None:
        self.extract_output = extract_output or (
            "```yaml\n"
            '- claim: "Le prefet signale des operations de police intensifiees."\n'
            "  confidence: 0.82\n"
            "  quote: \"operations de police\"\n"
            "  tags: [gallica]\n"
            "```\n"
        )
        self.translation_output = translation_output
        self.budget = budget or _Budget()
        self.calls: list[tuple[str, Any, tuple[Any, ...], dict[str, Any]]] = []

    def model_for(self, tier: str) -> Any:
        from pydantic_ai.models.test import TestModel

        return TestModel()

    async def call(self, tier: str, agent: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((tier, agent, args, kwargs))

        class _Result:
            def __init__(self, output: str) -> None:
                self.output = output

        if tier == "general":
            return _Result(self.extract_output)
        if tier == "frontier_speed":
            return _Result(self.translation_output)
        raise AssertionError(f"unexpected tier: {tier}")


def _event_payloads(job: Job, kind: str) -> list[dict[str, Any]]:
    conn = db.connect(job.db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM events WHERE job_id = ? AND kind = ? ORDER BY id",
            (job.id, kind),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row["payload_json"]) for row in rows]


@pytest.mark.asyncio
async def test_opt_in_translates_non_english_source(
    jobs_root: Path,
    db_path: Path,
    french_source_text: str,
) -> None:
    job = _make_job(jobs_root, db_path, translate_non_english=True)
    source_id = _seed_source(
        job,
        french_source_text,
        language_metadata={"dc:language": "fre"},
    )
    router = _Router(translation_output="The prefect reported intensified police operations.")

    result = await _run_extract_findings(job, {"payload": {"source_id": source_id}}, router=router)

    assert [call[0] for call in router.calls] == ["general", "frontier_speed"]
    assert result["translations_written"] == 1
    translation = job.root / "findings/000001.translation.md"
    assert translation.exists()
    text = translation.read_text(encoding="utf-8")
    assert "source_lang: fr" in text
    assert "target_lang: en" in text
    assert "The prefect reported intensified police operations." in text
    findings = _load_top_findings(job, 10)
    assert findings[0]["claim"] == "The prefect reported intensified police operations."
    assert findings[0]["translated"] is True
    assert (
        findings[0]["original_claim"]
        == "Le prefet signale des operations de police intensifiees."
    )


@pytest.mark.asyncio
async def test_opt_out_keeps_non_english_original_without_translation(
    jobs_root: Path,
    db_path: Path,
    french_source_text: str,
) -> None:
    job = _make_job(jobs_root, db_path, translate_non_english=False)
    source_id = _seed_source(
        job,
        french_source_text,
        language_metadata={"dc:language": "fre"},
    )
    router = _Router()

    result = await _run_extract_findings(job, {"payload": {"source_id": source_id}}, router=router)

    assert [call[0] for call in router.calls] == ["general"]
    assert "translations_written" not in result
    assert not (job.root / "findings/000001.translation.md").exists()


@pytest.mark.asyncio
async def test_english_source_does_not_translate_when_enabled(
    jobs_root: Path,
    db_path: Path,
    french_source_text: str,
) -> None:
    job = _make_job(jobs_root, db_path, translate_non_english=True)
    source_id = _seed_source(
        job,
        french_source_text,
        language_metadata={"language": "English"},
    )
    router = _Router()

    await _run_extract_findings(job, {"payload": {"source_id": source_id}}, router=router)

    assert [call[0] for call in router.calls] == ["general"]
    assert not (job.root / "findings/000001.translation.md").exists()


@pytest.mark.asyncio
async def test_task_payload_can_opt_in_without_job_default(
    jobs_root: Path,
    db_path: Path,
    french_source_text: str,
) -> None:
    job = _make_job(jobs_root, db_path)
    source_id = _seed_source(
        job,
        french_source_text,
        language_metadata={"lang": "fr"},
    )
    router = _Router(translation_output="A translated mirror.")

    await _run_extract_findings(
        job,
        {"payload": {"source_id": source_id, "translate_non_english": True}},
        router=router,
    )

    assert [call[0] for call in router.calls] == ["general", "frontier_speed"]
    assert "A translated mirror." in (
        job.root / "findings/000001.translation.md"
    ).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_budget_tipping_skips_translation_and_keeps_original_finding(
    jobs_root: Path,
    db_path: Path,
    french_source_text: str,
) -> None:
    job = _make_job(jobs_root, db_path, translate_non_english=True)
    source_id = _seed_source(
        job,
        french_source_text,
        language_metadata={"dc:language": "fre"},
    )
    budget = _Budget(would_exceed=True)
    router = _Router(budget=budget)

    result = await _run_extract_findings(job, {"payload": {"source_id": source_id}}, router=router)

    assert result["findings_written"] == 1
    assert result["translations_skipped_budget"] == 1
    assert [call[0] for call in router.calls] == ["general"]
    assert budget.calls and budget.calls[0][0] == "frontier_speed"
    assert (job.root / "findings/000001.md").exists()
    assert not (job.root / "findings/000001.translation.md").exists()
    payloads = _event_payloads(job, "translation_skipped_budget")
    assert payloads[0]["finding_id"] == 1
    assert payloads[0]["source_lang"] == "fr"
    assert payloads[0]["target_lang"] == "en"


def test_multilingual_source_handling_strategy_skill_loadable() -> None:
    body = load_skill("strategies", "multilingual-source-handling")

    assert "translate_non_english: true" in body
    assert "active_strategies" in body
    assert "gallica_search" in body
    assert "persee_search" in body
    assert "bne_search" in body
