#!/usr/bin/env python3
"""Coherence check: planner prompt + connector registry + skills folder agree.

Verification script for issue #223 AC-X1. Imports
:mod:`research_agent.tools` (so each connector module's ``register_kind``
runs), loads the planner prompt via :func:`prompts.loader.load_prompt`,
parses the rendered allowlist + Direct kinds table, and asserts:

  (a) every registered kind appears in the rendered Hard-rules allowlist;
  (b) every kind in the allowlist is in the registry;
  (c) the Direct kinds table contains exactly one row per registered kind;
  (d) for every kind whose ``skill_name`` is set, the corresponding
      ``src/research_agent/skills/connectors/<skill_name>.md`` file exists
      and parses via :func:`research_agent.skills.loader._parse`.

Exits 0 on success; prints a per-failure summary and exits 1 otherwise.
Run as ``uv run python tests/integration/check_planner_allowlist.py``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> int:
    import research_agent.tools  # noqa: F401 — side-effecting registration
    from research_agent.prompts.loader import _render_registry_vars, load_prompt
    from research_agent.skills.loader import SkillParseError, _parse, _skills_dir
    from research_agent.tools._registry import iter_kinds

    failures: list[str] = []

    # Render the prompt to verify substitution actually happens (no
    # ``{{...}}`` placeholders survive) — but use the structured rendered
    # values from ``_render_registry_vars`` for the allowlist + table
    # round-trip so the regex stays scoped to direct-connector kinds only.
    rendered = load_prompt(
        "planner",
        goal="placeholder goal",
        connector_skills_index="(none)",
        strategy_skills_index="(none)",
    )
    if "{{" in rendered:
        failures.append(
            f"planner template still contains placeholder substrings: "
            f"first 200 chars of leftover region: "
            f"{rendered[rendered.find('{{'):rendered.find('{{')+200]!r}"
        )

    registry_vars = _render_registry_vars()
    registered = {entry.name for entry in iter_kinds()}

    # (a) + (b): allowlist round-trip — operate on the rendered allowlist
    # *string*, not the whole prompt, so we don't mistake legitimate non-
    # direct kinds (``web_search``, ``news_search``, etc.) for orphans.
    allowlist_text = registry_vars["kinds_allowlist"]
    listed = set(re.findall(r"`([a-z_]+_search)`", allowlist_text))
    missing_from_prompt = registered - listed
    orphan_in_prompt = listed - registered
    if missing_from_prompt:
        failures.append(
            f"(a) registered kinds missing from rendered allowlist: "
            f"{sorted(missing_from_prompt)}"
        )
    if orphan_in_prompt:
        failures.append(
            f"(b) allowlist mentions kinds not in registry: "
            f"{sorted(orphan_in_prompt)}"
        )

    # (c) Direct kinds table contains exactly one row per registered kind.
    table_text = registry_vars["direct_kinds_table"]
    rows = re.findall(r"\|\s*`([a-z_]+_search)`\s*\|", table_text)
    row_set = set(rows)
    if len(rows) != len(row_set):
        dup = sorted({r for r in rows if rows.count(r) > 1})
        failures.append(f"(c) duplicate rows in Direct kinds table: {dup}")
    if row_set != registered:
        missing = registered - row_set
        extra = row_set - registered
        details: list[str] = []
        if missing:
            details.append(f"missing from table: {sorted(missing)}")
        if extra:
            details.append(f"extra rows in table: {sorted(extra)}")
        failures.append("(c) " + "; ".join(details))

    # (d) skill-file existence + parses for every kind with ``skill_name`` set.
    connectors_dir = _skills_dir("connectors")
    for entry in iter_kinds():
        if entry.skill_name is None:
            continue
        path = connectors_dir / f"{entry.skill_name}.md"
        if not path.exists():
            failures.append(
                f"(d) {entry.name}: missing skills/connectors/{entry.skill_name}.md"
            )
            continue
        try:
            _parse("connectors", entry.skill_name, path)
        except SkillParseError as exc:
            failures.append(f"(d) {entry.name}: {path}: {exc}")

    if failures:
        print("FAIL planner_allowlist coherence:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"OK: {len(registered)} registered kinds round-trip through the planner prompt"
        f" and skills folder."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
