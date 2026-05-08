"""Tests for `research_agent.skills.loader`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_agent.skills import loader as skills_loader
from research_agent.skills.loader import (
    Skill,
    SkillParseError,
    clear_cache,
    list_skills,
    load_skill,
    load_strategies,
)
from research_agent.storage import db
from research_agent.storage.jobs import Job


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def isolated_skills_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the loader at writable connectors/ + strategies/ subdirs."""
    root = tmp_path / "skills"
    (root / "connectors").mkdir(parents=True)
    (root / "strategies").mkdir(parents=True)

    def _dir(category: str) -> Path:
        return root / category

    monkeypatch.setattr(skills_loader, "_skills_dir", _dir)
    return root


@pytest.fixture
def jobs_root(tmp_path: Path) -> Path:
    return tmp_path / "jobs"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.sqlite"
    db.migrate(path=path).close()
    return path


@pytest.fixture
def job(jobs_root: Path, db_path: Path) -> Job:
    return Job.create(
        {"goal": "Investigate skills registry"},
        jobs_root=jobs_root,
        db_path=db_path,
    )


def _write_skill(
    dir_: Path,
    category: str,
    name: str,
    *,
    description: str = "Test skill.",
    when_to_use: str | None = None,
    when_not_to_use: str | None = None,
    body: str = "Body for the skill.\n",
) -> Path:
    path = dir_ / category / f"{name}.md"
    fm_lines = [f'description: "{description}"']
    if when_to_use is not None:
        fm_lines.append(f'when_to_use: "{when_to_use}"')
    if when_not_to_use is not None:
        fm_lines.append(f'when_not_to_use: "{when_not_to_use}"')
    fm = "\n".join(fm_lines)
    path.write_text(f"---\n{fm}\n---\n{body}", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def test_load_skill_returns_body(isolated_skills_dir: Path) -> None:
    _write_skill(
        isolated_skills_dir,
        "connectors",
        "congress",
        description="Congress.gov bills + members.",
        body="When the goal names a bill, set congress=119.\n",
    )
    body = load_skill("connectors", "congress")
    assert body == "When the goal names a bill, set congress=119.\n"


def test_list_skills_parses_frontmatter(isolated_skills_dir: Path) -> None:
    _write_skill(
        isolated_skills_dir,
        "connectors",
        "fec",
        description="OpenFEC candidates and filings.",
        when_to_use="Federal campaign finance lookups.",
        when_not_to_use="State-level disclosures.",
    )
    entries = list_skills("connectors")
    assert len(entries) == 1
    [entry] = entries
    assert entry["name"] == "fec"
    assert entry["description"] == "OpenFEC candidates and filings."
    assert entry["when_to_use"] == "Federal campaign finance lookups."
    assert entry["when_not_to_use"] == "State-level disclosures."
    assert entry["path"].endswith("fec.md")


def test_list_skills_sorts_deterministically(isolated_skills_dir: Path) -> None:
    for name in ("zulu", "alpha", "mike"):
        _write_skill(isolated_skills_dir, "connectors", name, description=f"{name} desc")
    entries = list_skills("connectors")
    assert [e["name"] for e in entries] == ["alpha", "mike", "zulu"]


def test_list_skills_empty_directory_returns_empty(isolated_skills_dir: Path) -> None:
    assert list_skills("connectors") == []
    assert list_skills("strategies") == []


def test_extra_frontmatter_keys_are_ignored(isolated_skills_dir: Path) -> None:
    path = isolated_skills_dir / "connectors" / "extras.md"
    path.write_text(
        '---\ndescription: "ok"\ntags: ["one", "two"]\nauthor: "alice"\n---\nBody.\n',
        encoding="utf-8",
    )
    entries = list_skills("connectors")
    assert entries[0]["name"] == "extras"
    assert entries[0]["description"] == "ok"


# ---------------------------------------------------------------------------
# Missing / malformed
# ---------------------------------------------------------------------------


def test_missing_skill_returns_empty_string(isolated_skills_dir: Path) -> None:
    assert load_skill("connectors", "not-shipped") == ""


def test_missing_frontmatter_raises(isolated_skills_dir: Path) -> None:
    path = isolated_skills_dir / "connectors" / "raw.md"
    path.write_text("Just a body, no frontmatter.\n", encoding="utf-8")
    with pytest.raises(SkillParseError) as excinfo:
        load_skill("connectors", "raw")
    assert "frontmatter" in str(excinfo.value).lower()


def test_invalid_yaml_in_frontmatter_raises(isolated_skills_dir: Path) -> None:
    path = isolated_skills_dir / "connectors" / "bad.md"
    path.write_text(
        "---\ndescription: \"unterminated\nmore: yes\n---\nBody.\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillParseError):
        load_skill("connectors", "bad")


def test_missing_required_description_raises(isolated_skills_dir: Path) -> None:
    path = isolated_skills_dir / "connectors" / "incomplete.md"
    path.write_text(
        '---\nwhen_to_use: "sometimes"\n---\nBody.\n',
        encoding="utf-8",
    )
    with pytest.raises(SkillParseError) as excinfo:
        load_skill("connectors", "incomplete")
    assert "description" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# load_strategies
# ---------------------------------------------------------------------------


def test_load_strategies_concatenates_in_caller_order(isolated_skills_dir: Path) -> None:
    _write_skill(
        isolated_skills_dir,
        "strategies",
        "alpha",
        description="alpha",
        body="Alpha body.\n",
    )
    _write_skill(
        isolated_skills_dir,
        "strategies",
        "beta",
        description="beta",
        body="Beta body.\n",
    )
    out = load_strategies(["beta", "alpha"])
    assert out.startswith("Beta body.")
    assert out.endswith("Alpha body.\n")
    assert "\n\n---\n\n" in out


def test_load_strategies_skips_missing(isolated_skills_dir: Path) -> None:
    _write_skill(
        isolated_skills_dir,
        "strategies",
        "modern-policy",
        description="modern policy era",
        body="Modern policy body.",
    )
    out = load_strategies(["does-not-exist", "modern-policy"])
    assert out == "Modern policy body."


def test_load_strategies_empty_list_returns_empty(isolated_skills_dir: Path) -> None:
    assert load_strategies([]) == ""


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def _events(job: Job) -> list[dict]:
    text = (job.root / "events.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_index_loaded_event_emitted_with_sha256s(
    isolated_skills_dir: Path, job: Job
) -> None:
    _write_skill(isolated_skills_dir, "connectors", "alpha", description="a")
    _write_skill(isolated_skills_dir, "connectors", "beta", description="b")

    list_skills("connectors", job=job)

    events = [e for e in _events(job) if e["kind"] == "index_loaded"]
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["category"] == "connectors"
    assert payload["count"] == 2
    assert isinstance(payload["sha256s"], list)
    assert len(payload["sha256s"]) == 2
    assert all(len(h) == 64 for h in payload["sha256s"])


def test_index_loaded_event_fires_once_per_job_category(
    isolated_skills_dir: Path, job: Job
) -> None:
    _write_skill(isolated_skills_dir, "connectors", "alpha", description="a")
    list_skills("connectors", job=job)
    list_skills("connectors", job=job)
    list_skills("connectors", job=job)
    events = [e for e in _events(job) if e["kind"] == "index_loaded"]
    assert len(events) == 1


def test_skill_loaded_event_emitted_with_sha256(
    isolated_skills_dir: Path, job: Job
) -> None:
    _write_skill(
        isolated_skills_dir,
        "connectors",
        "evented",
        description="x",
        body="The body.",
    )
    load_skill("connectors", "evented", job=job)
    events = [e for e in _events(job) if e["kind"] == "skill_loaded"]
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["category"] == "connectors"
    assert payload["name"] == "evented"
    assert len(payload["sha256"]) == 64
    assert payload["total_chars"] == len("The body.")


def test_skill_loaded_no_emit_for_missing(
    isolated_skills_dir: Path, job: Job
) -> None:
    load_skill("connectors", "missing", job=job)
    events = [e for e in _events(job) if e["kind"] == "skill_loaded"]
    assert events == []


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_load_skill_cached(isolated_skills_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_skill(isolated_skills_dir, "connectors", "cached", description="c")

    calls = {"n": 0}
    real_read_bytes = Path.read_bytes

    def counting_read_bytes(self: Path) -> bytes:
        if self.name == "cached.md":
            calls["n"] += 1
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)

    load_skill("connectors", "cached")
    load_skill("connectors", "cached")
    load_skill("connectors", "cached")

    assert calls["n"] == 1


def test_skill_model_is_frozen(isolated_skills_dir: Path) -> None:
    from pydantic import ValidationError

    _write_skill(isolated_skills_dir, "connectors", "frozen", description="f")
    list_skills("connectors")  # populates cache
    s = skills_loader._CACHE[("connectors", "frozen")]
    assert isinstance(s, Skill)
    with pytest.raises(ValidationError):
        s.body = "tampered"  # type: ignore[misc]
