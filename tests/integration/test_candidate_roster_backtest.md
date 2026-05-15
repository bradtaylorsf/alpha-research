# Candidate Roster CSV Backtest

Regression target for issue #310. This backtest mirrors the failed 2026
House/Senate roster goal:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_candidate_roster_backtest.py -q
```

The test uses only local fixtures:

- `tests/fixtures/fec/candidate_enumeration_2026.json` records two OpenFEC
  candidate-enumeration responses: CA House district 01 and FL Senate.
- `tests/fixtures/state_election/co_2026.csv` records one state-election
  roster row for CO House district 12.
- Maryland state-election coverage is intentionally absent. The queued task
  marks the unit `confirmed_gap` with a specific unblocker, proving the final
  report renders honest gaps instead of silently claiming completion.

Expected failures on pre-fix behavior:

- no `artifacts/candidates.csv`, or rows describe portals/sources instead of
  candidates;
- coverage units remain `pending` or `failed` after the fixture run;
- confirmed gaps are not rendered in `report.md`;
- a synthesis HTTP 400 leaves only `synthesis/*.failed.md` and does not point
  `report.md` at deterministic fallback content.
