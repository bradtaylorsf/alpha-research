"""Environment readiness checks for `research doctor`.

Each check returns a :class:`CheckResult` with a status of ``ok``, ``fail``, or
``skip``. Required checks failing cause `research doctor` to exit non-zero;
optional check absences (`skip`) never affect the exit code. Secrets are
masked everywhere — only the last four characters of any value ever appear in
output.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml  # type: ignore[import-untyped]

from research_agent import config


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # "ok" | "fail" | "skip"
    required: bool
    detail: str


def mask_secret(value: str) -> str:
    """Return a masked form of ``value`` safe for logs.

    Long values render as ``...XXXX`` (last four characters); shorter values
    collapse to ``***`` so we never leak more than half a key.
    """
    if not value:
        return "***"
    if len(value) <= 8:
        return "***"
    return f"...{value[-4:]}"


def check_python() -> CheckResult:
    major, minor = sys.version_info[:2]
    detail = f"{major}.{minor}.{sys.version_info.micro}"
    if (major, minor) >= (3, 12):
        return CheckResult("python", "ok", required=True, detail=detail)
    return CheckResult(
        "python",
        "fail",
        required=True,
        detail=f"{detail} (need >= 3.12)",
    )


def check_env_files(loaded: list[Path]) -> CheckResult:
    """Report which `.env*` files were loaded. Never required (defaults exist)."""
    if not loaded:
        return CheckResult("env_files", "skip", required=False, detail="not found")
    cwd = Path.cwd()
    rendered: list[str] = []
    for path in loaded:
        try:
            rendered.append(str(path.relative_to(cwd)))
        except ValueError:
            rendered.append(str(path))
    return CheckResult("env_files", "ok", required=False, detail=", ".join(rendered))


def check_env_keys() -> list[CheckResult]:
    """Per-key presence + masked detail for everything in EXPECTED_ENV_KEYS."""
    results: list[CheckResult] = []
    for key in config.EXPECTED_ENV_KEYS:
        # Read the raw process value (not the declared default) — defaults are
        # for runtime fallback, not for "is this configured" reporting.
        raw = os.environ.get(key.name)
        name = f"env:{key.name}"
        if raw:
            results.append(
                CheckResult(
                    name,
                    "ok",
                    required=key.required,
                    detail=f"present ({mask_secret(raw)})",
                )
            )
        elif key.required:
            results.append(CheckResult(name, "fail", required=True, detail="missing (required)"))
        else:
            results.append(CheckResult(name, "skip", required=False, detail="missing (optional)"))
    return results


def check_openrouter_key_shape() -> CheckResult:
    """Verify the OpenRouter key starts with ``sk-or-`` (no network call)."""
    raw = os.environ.get("OPENROUTER_API_KEY")
    name = "openrouter_key_shape"
    if not raw:
        return CheckResult(name, "skip", required=False, detail="key not set")
    if raw.startswith("sk-or-"):
        return CheckResult(name, "ok", required=True, detail=f"prefix ok ({mask_secret(raw)})")
    return CheckResult(
        name,
        "fail",
        required=True,
        detail="OPENROUTER_API_KEY does not start with 'sk-or-'",
    )


def check_lm_studio(base_url: str | None) -> CheckResult:
    """GET ``{base_url}/models`` with a short timeout. Never required."""
    name = "lm_studio"
    if not base_url:
        return CheckResult(name, "skip", required=False, detail="LMSTUDIO_BASE_URL unset")
    url = base_url.rstrip("/") + "/models"
    try:
        response = httpx.get(url, timeout=2.0)
    except httpx.HTTPError as exc:
        return CheckResult(
            name,
            "skip",
            required=False,
            detail=f"not reachable at {url} ({type(exc).__name__})",
        )
    if response.status_code >= 400:
        return CheckResult(
            name,
            "skip",
            required=False,
            detail=f"{url} returned HTTP {response.status_code}",
        )
    return CheckResult(name, "ok", required=False, detail=f"reachable at {url}")


def check_writable_dirs(repo_root: Path) -> CheckResult:
    """Ensure ``data/`` and ``jobs/`` exist under repo_root and are writable."""
    name = "writable_dirs"
    failures: list[str] = []
    for sub in ("data", "jobs"):
        target = repo_root / sub
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".doctor-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            failures.append(f"{sub}/: {exc}")
    if failures:
        return CheckResult(name, "fail", required=True, detail="; ".join(failures))
    return CheckResult(
        name,
        "ok",
        required=True,
        detail=f"data/ and jobs/ writable under {repo_root}",
    )


def check_sqlite_wal() -> CheckResult:
    """Open a tempdir SQLite DB and verify WAL mode is selectable."""
    name = "sqlite_wal"
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "probe.sqlite"
        try:
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.execute("PRAGMA journal_mode=WAL")
                mode = cursor.fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return CheckResult(name, "fail", required=True, detail=str(exc))
    if str(mode).lower() == "wal":
        return CheckResult(name, "ok", required=True, detail="journal_mode=wal")
    return CheckResult(
        name,
        "fail",
        required=True,
        detail=f"PRAGMA journal_mode returned {mode!r}",
    )


def check_serpapi_cost_note() -> CheckResult:
    """Surface the per-query SERPAPI cost so the operator sees the spend trajectory.

    SERPAPI bills per call (≈ $0.015 against the $75/mo / 5k-search plan), so
    ``research doctor`` calls this out when ``SERPAPI_KEY`` is set. When the
    key is unset the check skips — the scholar connector can't run anyway.
    """
    name = "serpapi_cost_note"
    raw = os.environ.get("SERPAPI_KEY")
    if not raw:
        return CheckResult(
            name,
            "skip",
            required=False,
            detail="SERPAPI_KEY unset — scholar connector disabled",
        )
    return CheckResult(
        name,
        "ok",
        required=False,
        detail="per-query ≈ $0.015 against SERPAPI plan (5k/mo @ $75)",
    )


def check_trove_api_note() -> CheckResult:
    """Surface Trove renewal and metadata-only constraints in doctor output."""
    name = "trove_api_note"
    raw = os.environ.get("TROVE_API_KEY")
    if not raw:
        return CheckResult(
            name,
            "skip",
            required=False,
            detail=(
                "TROVE_API_KEY unset - trove_search disabled; keys expire after"
                " 12 months; connector is metadata-only to avoid full-text key"
                " revocation risk"
            ),
        )
    return CheckResult(
        name,
        "ok",
        required=False,
        detail=(
            f"present ({mask_secret(raw)}); renew annually; metadata-only default,"
            " no automatic full-text fetching"
        ),
    )


def check_models_yaml(path: Path) -> CheckResult:
    """Parse ``config/models.yaml`` — fail if missing or invalid YAML."""
    name = "models_yaml"
    if not path.is_file():
        return CheckResult(name, "fail", required=True, detail=f"not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
        yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as exc:
        return CheckResult(name, "fail", required=True, detail=f"{path}: {exc}")
    return CheckResult(name, "ok", required=True, detail=f"parses ({path})")


_TESSERACT_MISSING_HINT = (
    "missing — scanned-PDF OCR will be unavailable. brew install tesseract"
)


def check_tesseract() -> CheckResult:
    """Detect the ``tesseract`` binary used by the PDF OCR escalation layer.

    Optional: scanned PDFs degrade silently without it, so we surface a
    `skip` with an install hint instead of failing the run.
    """
    name = "tesseract"
    binary = shutil.which("tesseract")
    if binary is None:
        return CheckResult(name, "skip", required=False, detail=_TESSERACT_MISSING_HINT)
    try:
        completed = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return CheckResult(name, "skip", required=False, detail=_TESSERACT_MISSING_HINT)
    if completed.returncode != 0:
        return CheckResult(name, "skip", required=False, detail=_TESSERACT_MISSING_HINT)
    output = (completed.stdout or b"") + (completed.stderr or b"")
    first_line = output.decode("utf-8", errors="replace").splitlines()[0].strip() if output else ""
    return CheckResult(name, "ok", required=False, detail=first_line or "available")


_SANCTIONS_REFRESH_STALE_SECONDS = 7 * 24 * 60 * 60


def check_sanctions_refresh() -> list[CheckResult]:
    """Surface the most-recent successful refresh per sanctions list.

    Issue #154: emits one ``sanctions:<KIND>`` row per known list. ``ok`` when
    the list refreshed within the last 7 days, ``skip`` for intentionally
    disabled lists (``EU`` is currently dropped per #154), and ``fail`` when a
    list has never been refreshed or is stale beyond 7 days. All rows are
    ``required=False`` so a stale optional list never flips ``doctor`` exit
    code — the operator just sees the freshness on the dashboard.
    """
    from research_agent.tools import sanctions

    results: list[CheckResult] = []
    last = sanctions.get_last_refresh()
    now = time.time()
    for kind in sanctions.LIST_KINDS:
        name = f"sanctions:{kind}"
        if sanctions.is_list_disabled(kind):
            results.append(
                CheckResult(
                    name,
                    "skip",
                    required=False,
                    detail="refresh disabled (issue #154 — endpoint 403)",
                )
            )
            continue
        ts = last.get(kind)
        if ts is None:
            results.append(
                CheckResult(
                    name,
                    "fail",
                    required=False,
                    detail="never refreshed",
                )
            )
            continue
        age = now - ts
        when = datetime.fromtimestamp(ts, tz=UTC).date().isoformat()
        if age <= _SANCTIONS_REFRESH_STALE_SECONDS:
            results.append(
                CheckResult(
                    name,
                    "ok",
                    required=False,
                    detail=f"last refresh {when}",
                )
            )
        else:
            days = int(age // 86_400)
            results.append(
                CheckResult(
                    name,
                    "fail",
                    required=False,
                    detail=f"stale: last refresh {when} ({days}d ago)",
                )
            )
    return results


def check_planner_allowlist_coherence() -> CheckResult:
    """Assert the planner prompt + the connector registry agree on kinds.

    Issue #223: the registry is the single source of truth and the planner
    prompt is rendered from it. Drift between (a) registered kinds and the
    Hard-rules allowlist, (b) the allowlist and the registry, or (c) the
    Direct kinds table row count is a structural failure — it means a
    planner that has fanned out to a connector kind the orchestrator can't
    dispatch (or, more often, a kind that ships in the orchestrator but
    the planner doesn't know about).

    Required check: a registered kind missing from the allowlist would
    leave the planner unable to use a shipped connector; an orphan
    allowlist entry would let the planner emit a kind no handler exists
    for. Either is a hard fail.
    """
    name = "planner_allowlist_coherence"
    try:
        from research_agent.prompts.loader import _render_registry_vars
        from research_agent.tools._registry import iter_kinds

        registered = {entry.name for entry in iter_kinds()}
        # Render against the registry without touching the prompt cache —
        # ``load_prompt_meta`` would emit ``prompt_loaded`` to stdout and
        # contaminate ``doctor --json`` output for downstream consumers.
        rendered = _render_registry_vars()
        # The Hard-rules sentence carries the allowlist. We pull every
        # backticked ``<x>_search`` token from the rendered allowlist and
        # compare to the registry. The sentence-level rendering is what
        # the model actually reads, so we validate against that.
        allowlist_text = rendered["kinds_allowlist"]
        listed = set(re.findall(r"`([a-z_]+_search)`", allowlist_text))
        registered_missing = registered - listed
        orphan = listed - registered
        # The Direct kinds table must contain exactly one row per kind.
        # Each rendered row begins with ``| `<x>_search` ``.
        table_text = rendered["direct_kinds_table"]
        table_kinds = re.findall(r"\|\s*`([a-z_]+_search)`\s*\|", table_text)
        table_set = set(table_kinds)
        table_dupes = [k for k in table_kinds if table_kinds.count(k) > 1]
        table_missing = registered - table_set
        problems: list[str] = []
        if registered_missing:
            problems.append(
                f"registered but not in allowlist: {sorted(registered_missing)}"
            )
        if orphan:
            problems.append(f"in allowlist but not registered: {sorted(orphan)}")
        if table_missing:
            problems.append(
                f"registered but no Direct-kinds-table row: {sorted(table_missing)}"
            )
        if table_dupes:
            problems.append(f"duplicate table rows: {sorted(set(table_dupes))}")
        if problems:
            return CheckResult(
                name,
                "fail",
                required=True,
                detail="; ".join(problems),
            )
        return CheckResult(
            name,
            "ok",
            required=True,
            detail=(
                f"{len(registered)} kinds round-trip through allowlist + table"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — surface every failure mode
        return CheckResult(
            name,
            "fail",
            required=True,
            detail=f"coherence check raised {type(exc).__name__}: {exc}",
        )


def check_registry_skill_coherence() -> list[CheckResult]:
    """Assert each registered kind's skill file exists and parses.

    Issue #223: every connector PR ships a ``skills/connectors/<name>.md``
    file (per #211/#212). Kinds with ``skill_name=None`` are grandfathered
    from the existing-connector skills backfill — they ``skip`` rather than
    fail. Kinds whose ``skill_name`` is set but the file is missing are a
    hard ``fail`` (the planner would fall back to a description-only path).
    """
    from research_agent.skills.loader import SkillParseError, _parse, _skills_dir
    from research_agent.tools._registry import iter_kinds

    results: list[CheckResult] = []
    for entry in iter_kinds():
        row = f"registry_skill:{entry.name}"
        if entry.skill_name is None:
            results.append(
                CheckResult(
                    row,
                    "skip",
                    required=False,
                    detail=(
                        f"{entry.name} grandfathered (skill_name=None);"
                        " backfill pending"
                    ),
                )
            )
            continue
        path = _skills_dir("connectors") / f"{entry.skill_name}.md"
        if not path.exists():
            results.append(
                CheckResult(
                    row,
                    "fail",
                    required=True,
                    detail=(
                        f"missing skills/connectors/{entry.skill_name}.md"
                        f" for kind {entry.name}"
                    ),
                )
            )
            continue
        try:
            _parse("connectors", entry.skill_name, path)
        except SkillParseError as exc:
            results.append(
                CheckResult(
                    row,
                    "fail",
                    required=True,
                    detail=f"{path}: {exc}",
                )
            )
            continue
        results.append(
            CheckResult(
                row,
                "ok",
                required=True,
                detail=f"skills/connectors/{entry.skill_name}.md parses",
            )
        )
    return results


def run_all_checks(
    loaded_env_files: list[Path],
    *,
    repo_root: Path | None = None,
) -> list[CheckResult]:
    root = repo_root or Path.cwd()
    results: list[CheckResult] = [
        check_python(),
        check_env_files(loaded_env_files),
    ]
    results.extend(check_env_keys())
    results.append(check_openrouter_key_shape())
    results.append(check_lm_studio(config.get("LMSTUDIO_BASE_URL")))
    results.append(check_writable_dirs(root))
    results.append(check_sqlite_wal())
    results.append(check_models_yaml(root / "config" / "models.yaml"))
    results.append(check_tesseract())
    results.append(check_serpapi_cost_note())
    results.append(check_trove_api_note())
    results.extend(check_sanctions_refresh())
    results.append(check_planner_allowlist_coherence())
    results.extend(check_registry_skill_coherence())
    return results


_GLYPHS = {
    "ok": ("[green]✓[/green]", "green"),
    "fail": ("[red]✗[/red]", "red"),
    "skip": ("[yellow]–[/yellow]", "yellow"),
}


def render_table(results: list[CheckResult]) -> None:
    """Print a Rich table summarising every check."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title="research doctor", show_lines=False)
    table.add_column("", width=2)
    table.add_column("Check")
    table.add_column("Required")
    table.add_column("Detail")

    for result in results:
        glyph, _ = _GLYPHS.get(result.status, ("?", "white"))
        table.add_row(
            glyph,
            result.name,
            "yes" if result.required else "no",
            result.detail,
        )
    console.print(table)


def to_json(results: list[CheckResult], loaded_env_files: list[Path]) -> dict[str, Any]:
    """Return a JSON-serialisable summary. Never includes raw secret values."""
    return {
        "loaded_env_files": [str(p) for p in loaded_env_files],
        "checks": [asdict(r) for r in results],
        "ok": all(r.status != "fail" for r in results if r.required),
    }


def has_required_failure(results: list[CheckResult]) -> bool:
    return any(r.status == "fail" and r.required for r in results)


def emit_json(
    results: list[CheckResult],
    loaded_env_files: list[Path],
) -> str:
    """Serialise the report exactly the way the CLI prints it."""
    return json.dumps(to_json(results, loaded_env_files), indent=2, sort_keys=True)
