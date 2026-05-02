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
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass
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
