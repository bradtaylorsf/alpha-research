"""Tests for `research_agent.prompts.loader`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_agent.prompts import loader as prompts_loader
from research_agent.prompts.loader import (
    Prompt,
    PromptNotFoundError,
    PromptVariableMissing,
    clear_cache,
    load_prompt,
    load_prompt_meta,
)
from research_agent.storage import db
from research_agent.storage.jobs import Job


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """Each test starts with an empty in-process prompt cache."""
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def isolated_prompts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the loader at a writable temp directory of test prompts."""
    target = tmp_path / "prompts"
    target.mkdir()
    monkeypatch.setattr(prompts_loader, "_prompts_dir", lambda: target)
    return target


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
        {"goal": "Investigate prompt registry"},
        jobs_root=jobs_root,
        db_path=db_path,
    )


def _write_prompt(
    dir_: Path,
    name: str,
    *,
    version: str = "1",
    model_tier: str = "general",
    description: str = "Test prompt.",
    body: str = "Hello {{who}}!",
) -> Path:
    path = dir_ / f"{name}.md"
    path.write_text(
        f"---\n"
        f'version: "{version}"\n'
        f"model_tier: {model_tier}\n"
        f'description: "{description}"\n'
        f"---\n"
        f"{body}",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Shipped prompt files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["planner", "researcher", "synthesizer", "critic", "intake_followup"],
)
def test_shipped_prompt_loads_with_valid_frontmatter(name: str) -> None:
    meta = load_prompt_meta(name)
    assert meta.name == name
    assert meta.version  # non-empty
    assert meta.model_tier in {
        "fast",
        "general",
        "reasoner",
        "frontier",
        "frontier_alt",
        "frontier_speed",
    }
    assert meta.description
    assert len(meta.sha256) == 64
    assert meta.path.exists()
    assert meta.template  # body non-empty


def test_planner_substitutes_goal_placeholder() -> None:
    rendered = load_prompt("planner", goal="map who funded ACME Corp 2019-2024")
    assert "{{goal}}" not in rendered
    assert "map who funded ACME Corp 2019-2024" in rendered


def test_intake_followup_substitutes_question_placeholder() -> None:
    rendered = load_prompt("intake_followup", question="who is jane doe")
    assert "{{question}}" not in rendered
    assert "who is jane doe" in rendered


# ---------------------------------------------------------------------------
# Missing prompt
# ---------------------------------------------------------------------------


def test_missing_prompt_raises_with_helpful_message(
    isolated_prompts_dir: Path,
) -> None:
    _write_prompt(isolated_prompts_dir, "alpha")
    _write_prompt(isolated_prompts_dir, "beta")

    with pytest.raises(PromptNotFoundError) as excinfo:
        load_prompt("nonexistent")

    msg = str(excinfo.value)
    assert "nonexistent" in msg
    assert "alpha" in msg and "beta" in msg
    # PromptNotFoundError is also a FileNotFoundError so existing handlers work.
    assert isinstance(excinfo.value, FileNotFoundError)


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------


def test_substitution_renders_value(isolated_prompts_dir: Path) -> None:
    _write_prompt(isolated_prompts_dir, "greet", body="Hello {{who}}!")
    assert load_prompt("greet", who="world") == "Hello world!"


def test_substitution_is_whitespace_tolerant(isolated_prompts_dir: Path) -> None:
    _write_prompt(
        isolated_prompts_dir,
        "spaced",
        body="A={{ x }}, B={{y }}, C={{ z}}, D={{w}}",
    )
    out = load_prompt("spaced", x="1", y="2", z="3", w="4")
    assert out == "A=1, B=2, C=3, D=4"


def test_substitution_unused_vars_ignored(isolated_prompts_dir: Path) -> None:
    _write_prompt(isolated_prompts_dir, "greet", body="Hello {{who}}!")
    # `extra` is not in template — should be silently accepted.
    assert load_prompt("greet", who="bob", extra="unused") == "Hello bob!"


def test_substitution_missing_variable_raises(isolated_prompts_dir: Path) -> None:
    _write_prompt(isolated_prompts_dir, "greet", body="Hello {{who}}!")
    with pytest.raises(PromptVariableMissing) as excinfo:
        load_prompt("greet")
    assert "who" in str(excinfo.value)


def test_substitution_repeated_placeholder(isolated_prompts_dir: Path) -> None:
    _write_prompt(isolated_prompts_dir, "echo", body="{{x}} and {{x}} again")
    assert load_prompt("echo", x="ping") == "ping and ping again"


def test_template_without_placeholders_returns_body_verbatim(
    isolated_prompts_dir: Path,
) -> None:
    _write_prompt(isolated_prompts_dir, "static", body="No placeholders here.\n")
    assert load_prompt("static") == "No placeholders here.\n"


# ---------------------------------------------------------------------------
# Frontmatter / metadata
# ---------------------------------------------------------------------------


def test_frontmatter_fields_surface_via_meta(isolated_prompts_dir: Path) -> None:
    _write_prompt(
        isolated_prompts_dir,
        "p",
        version="3",
        model_tier="frontier",
        description="A meaningful prompt.",
        body="Body.",
    )
    meta = load_prompt_meta("p")
    assert isinstance(meta, Prompt)
    assert meta.name == "p"
    assert meta.version == "3"
    assert meta.model_tier == "frontier"
    assert meta.description == "A meaningful prompt."
    assert meta.template == "Body."


def test_frontmatter_missing_version_raises(isolated_prompts_dir: Path) -> None:
    path = isolated_prompts_dir / "bad.md"
    path.write_text(
        "---\nmodel_tier: general\ndescription: missing version\n---\nBody.",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        load_prompt("bad")
    assert "version" in str(excinfo.value).lower()


def test_frontmatter_missing_block_raises(isolated_prompts_dir: Path) -> None:
    path = isolated_prompts_dir / "raw.md"
    path.write_text("Just a body, no frontmatter.\n", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        load_prompt("raw")
    assert "frontmatter" in str(excinfo.value).lower()


def test_frontmatter_invalid_model_tier_raises(isolated_prompts_dir: Path) -> None:
    _write_prompt(isolated_prompts_dir, "p", model_tier="hyper-mega-frontier")
    with pytest.raises(ValueError):
        load_prompt("p")


def test_frontmatter_unknown_field_rejected(isolated_prompts_dir: Path) -> None:
    path = isolated_prompts_dir / "extra.md"
    path.write_text(
        '---\nversion: "1"\nmodel_tier: general\ndescription: x\nsurprise: nope\n---\nBody.',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_prompt("extra")


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_cache_returns_same_instance(isolated_prompts_dir: Path) -> None:
    _write_prompt(isolated_prompts_dir, "cached")
    a = load_prompt_meta("cached")
    b = load_prompt_meta("cached")
    assert a is b


def test_cache_hash_computed_once(
    isolated_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_prompt(isolated_prompts_dir, "once")

    calls = {"n": 0}
    real_read_bytes = Path.read_bytes

    def counting_read_bytes(self: Path) -> bytes:
        if self.name == "once.md":
            calls["n"] += 1
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)

    load_prompt("once", who="a")
    load_prompt("once", who="b")
    load_prompt_meta("once")

    assert calls["n"] == 1


def test_clear_cache_forces_reload(
    isolated_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_prompt(isolated_prompts_dir, "reload")

    calls = {"n": 0}
    real_read_bytes = Path.read_bytes

    def counting_read_bytes(self: Path) -> bytes:
        if self.name == "reload.md":
            calls["n"] += 1
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)

    load_prompt_meta("reload")
    clear_cache()
    load_prompt_meta("reload")

    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


def test_first_load_with_job_emits_prompt_loaded_event(
    isolated_prompts_dir: Path, job: Job
) -> None:
    _write_prompt(isolated_prompts_dir, "evented")

    load_prompt("evented", who="x", job=job)

    text = (job.root / "events.jsonl").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["kind"] == "prompt_loaded"
    assert payload["actor"] == "prompts"
    assert payload["payload"]["name"] == "evented"
    assert len(payload["payload"]["sha256"]) == 64
    assert payload["payload"]["version"] == "1"
    assert payload["payload"]["model_tier"] == "general"


def test_event_emitted_only_on_first_load(isolated_prompts_dir: Path, job: Job) -> None:
    _write_prompt(isolated_prompts_dir, "evented")

    load_prompt("evented", who="x", job=job)
    load_prompt("evented", who="y", job=job)
    load_prompt_meta("evented", job=job)

    text = (job.root / "events.jsonl").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    # Exactly one prompt_loaded event despite three load calls.
    assert sum(1 for line in lines if json.loads(line)["kind"] == "prompt_loaded") == 1


def test_load_without_job_does_not_emit(isolated_prompts_dir: Path, job: Job) -> None:
    _write_prompt(isolated_prompts_dir, "silent")
    load_prompt("silent", who="x")  # no job kwarg
    # Job's events.jsonl is created at Job.create() and starts empty.
    text = (job.root / "events.jsonl").read_text(encoding="utf-8")
    assert text == ""
