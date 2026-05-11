#!/usr/bin/env python3
"""Regenerate the **Direct connector kinds** table in ``README.md``.

Reads :func:`research_agent.tools._registry.iter_kinds` (issue #223) and
rewrites the section between::

    <!-- BEGIN: direct-connector-kinds (auto-generated) -->
    <!-- END: direct-connector-kinds -->

with the same markdown table the planner prompt renders. Run this any
time a connector lands or its description changes; CI in a follow-up
issue will assert idempotency.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from research_agent.tools._registry import (  # noqa: E402
    render_direct_kinds_table,
)
import research_agent.tools  # noqa: E402, F401 — side-effecting registration


BEGIN = "<!-- BEGIN: direct-connector-kinds (auto-generated) -->"
END = "<!-- END: direct-connector-kinds -->"


def regen(readme_path: Path) -> bool:
    """Rewrite the sentinel block. Returns True when the file changed."""
    text = readme_path.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        raise SystemExit(
            f"{readme_path}: missing sentinel block — add"
            f"\n  {BEGIN}\n  {END}\nwhere the table should land."
        )
    pre, _, rest = text.partition(BEGIN)
    _, _, post = rest.partition(END)
    rendered = render_direct_kinds_table()
    block = f"{BEGIN}\n\n{rendered}\n\n{END}"
    new_text = pre + block + post
    if new_text == text:
        return False
    readme_path.write_text(new_text, encoding="utf-8")
    return True


def main() -> int:
    readme = REPO_ROOT / "README.md"
    changed = regen(readme)
    if changed:
        print(f"updated {readme}")
    else:
        print(f"{readme} already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
