"""Focused tests for ``research_agent.llm.budgets``.

The router has its own integration tests in ``test_llm_router.py``; this
file pins the BudgetTracker contract directly: precheck/charge math,
ledger + ``jobs.cost_so_far_usd`` dual write, the 90% warning threshold,
graceful degradation when pricing is missing, and the boundary behavior
the daemon depends on (a charge that lands exactly on the cap must trip
the *next* precheck, not the current call).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from research_agent.llm.budgets import (
    BudgetExceeded,
    BudgetTracker,
    TokenUsage,
)
from research_agent.storage import db
from research_agent.storage.jobs import Job

PRICING = {
    "frontier": {"input_usd_per_mtok": 10.0, "output_usd_per_mtok": 30.0},
    "frontier_speed": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 5.0},
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.sqlite"
    db.migrate(path=path).close()
    return path


@pytest.fixture
def jobs_root(tmp_path: Path) -> Path:
    return tmp_path / "jobs"


@pytest.fixture
def job(jobs_root: Path, db_path: Path) -> Job:
    return Job.create(
        {"goal": "Investigate budget tracker"},
        jobs_root=jobs_root,
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# precheck — no cap, pre-cap, boundary, post-cap
# ---------------------------------------------------------------------------


def test_precheck_no_cap_disables_enforcement(job: Job, db_path: Path) -> None:
    bt = BudgetTracker(job.id, cap_usd=None, pricing=PRICING, db_path=db_path)
    bt.spent = 999_999.99
    bt.precheck("frontier")  # must not raise


def test_precheck_passes_below_cap(job: Job, db_path: Path) -> None:
    bt = BudgetTracker(job.id, cap_usd=1.00, pricing=PRICING, db_path=db_path)
    bt.spent = 0.50
    bt.precheck("frontier")


def test_charge_landing_exactly_on_cap_trips_next_precheck(job: Job, db_path: Path) -> None:
    """A charge that hits the cap exactly is allowed; the *next* precheck blocks."""
    cap = 0.30  # = 10*10/1e6 + 30000*30/1e6 below — tuned by usage.
    bt = BudgetTracker(job.id, cap_usd=cap, pricing=PRICING, db_path=db_path)

    # Spend exactly $0.30 in one call: 30,000 input @ $10/Mtok ($0.30 input only).
    usage = TokenUsage(input_tokens=30_000, output_tokens=0)
    cost = bt.charge("frontier", "openrouter", "model-a", usage)
    assert cost == pytest.approx(0.30)
    assert bt.spent == pytest.approx(cap)

    with pytest.raises(BudgetExceeded) as ei:
        bt.precheck("frontier")
    assert ei.value.spent == pytest.approx(cap)
    assert ei.value.cap == pytest.approx(cap)


def test_precheck_post_cap_continues_to_raise(job: Job, db_path: Path) -> None:
    bt = BudgetTracker(job.id, cap_usd=1.0, pricing=PRICING, db_path=db_path)
    bt.spent = 5.0
    with pytest.raises(BudgetExceeded):
        bt.precheck("frontier")
    # Re-checking does not magically reset.
    with pytest.raises(BudgetExceeded):
        bt.precheck("frontier_speed")


# ---------------------------------------------------------------------------
# 90% warning
# ---------------------------------------------------------------------------


def test_precheck_emits_single_90pct_warning(
    job: Job, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bt = BudgetTracker(job.id, cap_usd=1.0, pricing=PRICING, db_path=db_path)
    bt.spent = 0.95  # >= 90% but below cap

    caplog.set_level(logging.WARNING, logger="research_agent.llm.budgets")
    bt.precheck("frontier")  # must not raise
    bt.precheck("frontier")  # second call must not re-emit
    bt.precheck("frontier")

    warnings = [r for r in caplog.records if "90%%" in r.msg or "90%" in r.msg]
    assert len(warnings) == 1
    assert bt._warned_90pct is True


def test_precheck_does_not_warn_below_90pct(
    job: Job, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bt = BudgetTracker(job.id, cap_usd=1.0, pricing=PRICING, db_path=db_path)
    bt.spent = 0.5
    caplog.set_level(logging.WARNING, logger="research_agent.llm.budgets")
    bt.precheck("frontier")
    assert not [r for r in caplog.records if "90%%" in r.msg or "90%" in r.msg]
    assert bt._warned_90pct is False


# ---------------------------------------------------------------------------
# charge — math, dual write, missing pricing
# ---------------------------------------------------------------------------


def test_charge_cost_math_matches_formula(job: Job, db_path: Path) -> None:
    bt = BudgetTracker(job.id, cap_usd=10.0, pricing=PRICING, db_path=db_path)
    usage = TokenUsage(input_tokens=1234, output_tokens=5678)
    cost = bt.charge("frontier_speed", "openrouter", "haiku", usage)
    expected = (1234 * 1.0 + 5678 * 5.0) / 1_000_000
    assert cost == pytest.approx(expected, abs=1e-9)
    assert round(cost, 6) == round(expected, 6)


def test_charge_persists_ledger_row_and_jobs_total_in_one_transaction(
    job: Job, db_path: Path
) -> None:
    bt = BudgetTracker(job.id, cap_usd=10.0, pricing=PRICING, db_path=db_path)
    usage = TokenUsage(input_tokens=100_000, output_tokens=50_000, latency_ms=42)
    cost = bt.charge("frontier", "openrouter", "claude-opus", usage)

    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT tier, provider, model, input_tokens, output_tokens,"
            " latency_ms, cost_usd FROM llm_calls WHERE job_id = ?",
            (job.id,),
        ).fetchall()
        cost_so_far = conn.execute(
            "SELECT cost_so_far_usd FROM jobs WHERE id = ?", (job.id,)
        ).fetchone()[0]
    finally:
        conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["tier"] == "frontier"
    assert row["provider"] == "openrouter"
    assert row["model"] == "claude-opus"
    assert row["input_tokens"] == 100_000
    assert row["output_tokens"] == 50_000
    assert row["latency_ms"] == 42
    assert row["cost_usd"] == pytest.approx(cost)
    assert cost_so_far == pytest.approx(cost)


def test_charge_unknown_tier_logs_warning_and_records_zero_cost(
    job: Job, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bt = BudgetTracker(job.id, cap_usd=10.0, pricing=PRICING, db_path=db_path)
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)

    caplog.set_level(logging.WARNING, logger="research_agent.llm.budgets")
    cost = bt.charge("mystery_tier", "openrouter", "unknown-model", usage)

    assert cost == 0.0
    assert bt.spent == 0.0
    assert any(
        "no pricing for tier" in (r.getMessage() if hasattr(r, "getMessage") else r.msg)
        for r in caplog.records
    )

    # Row still landed; the run kept going.
    conn = db.connect(db_path)
    try:
        rows = conn.execute("SELECT cost_usd FROM llm_calls WHERE job_id = ?", (job.id,)).fetchall()
        cost_so_far = conn.execute(
            "SELECT cost_so_far_usd FROM jobs WHERE id = ?", (job.id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["cost_usd"] == 0.0
    assert cost_so_far == 0.0


def test_charge_zero_pricing_logs_warning_and_records_zero_cost(
    job: Job, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pricing = {"frontier": {"input_usd_per_mtok": 0.0, "output_usd_per_mtok": 0.0}}
    bt = BudgetTracker(job.id, cap_usd=1.0, pricing=pricing, db_path=db_path)
    usage = TokenUsage(input_tokens=10_000_000, output_tokens=10_000_000)

    caplog.set_level(logging.WARNING, logger="research_agent.llm.budgets")
    cost = bt.charge("frontier", "openrouter", "claude-opus", usage)
    assert cost == 0.0
    assert any("zero pricing" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# rehydration
# ---------------------------------------------------------------------------


def test_rehydrate_picks_up_jobs_cost_so_far_usd(job: Job, db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE jobs SET cost_so_far_usd = ? WHERE id = ?",
                (3.14, job.id),
            )
    finally:
        conn.close()

    bt = BudgetTracker(job.id, cap_usd=10.0, pricing=PRICING, db_path=db_path)
    assert bt.spent == pytest.approx(3.14)


def test_rehydrate_with_missing_job_row_starts_at_zero(db_path: Path) -> None:
    bt = BudgetTracker("nonexistent-job", cap_usd=10.0, pricing=PRICING, db_path=db_path)
    assert bt.spent == 0.0


def test_charge_then_rehydrate_preserves_total(job: Job, db_path: Path) -> None:
    """A daemon that crashes after charge() must see the same running total on restart."""
    bt = BudgetTracker(job.id, cap_usd=10.0, pricing=PRICING, db_path=db_path)
    usage = TokenUsage(input_tokens=500_000, output_tokens=200_000)
    cost = bt.charge("frontier", "openrouter", "claude-opus", usage)

    bt2 = BudgetTracker(job.id, cap_usd=10.0, pricing=PRICING, db_path=db_path)
    assert bt2.spent == pytest.approx(cost)
