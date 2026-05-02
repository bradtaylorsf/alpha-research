#!/usr/bin/env bash
# Tolerant test runner used as `test_command` in .alpha-loop.yaml.
#
# During the bootstrap phases (issues #1–#3 — scaffold, pyproject, doctor)
# there's no Python project and no tests yet, so a strict `pytest` would
# fail every validation. This wrapper degrades gracefully:
#
#   no pyproject.toml          → pass (project not bootstrapped yet)
#   pyproject exists, no tests → pass (pytest exit code 5)
#   tests exist, all pass      → pass (pytest exit code 0)
#   tests exist, any fail      → fail (pytest exit code 1)
#   real errors (2,3,4)        → fail
#
# Once Phase 1 lands real tests, this wrapper is effectively a no-op
# around `uv run pytest`. It is safe to keep long-term.

set +e

if [ ! -f pyproject.toml ]; then
  echo "[test.sh] No pyproject.toml — bootstrap phase, skipping pytest."
  exit 0
fi

uv run pytest -q "$@"
rc=$?

if [ $rc -eq 0 ]; then
  exit 0
fi

if [ $rc -eq 5 ]; then
  echo "[test.sh] pytest collected no tests (exit 5) — treating as pass."
  exit 0
fi

exit $rc
