"""Per-job cost cap enforcement (implementation guide §9).

This module is the single point of truth for cloud LLM spend on a job:

- :class:`TokenUsage` — provider-agnostic shape for the numbers we ledger.
- :class:`BudgetExceeded` — raised by ``precheck()`` once the cap is hit.
- :class:`BudgetTracker` — ``precheck()`` before cloud calls, ``charge()`` after.

The pricing block lives in ``config/models.yaml`` (manually maintained — the
OpenRouter pricing API is a future-deps item). ``charge()`` reads it to
compute cost from ``usage`` and is the single writer of the cloud
``llm_calls`` row plus the ``jobs.cost_so_far_usd`` running total. The
:mod:`router` no longer writes the ledger for cloud tiers; that lives here
so the cap stays consistent across crashes and restarts. ``BudgetTracker``
re-hydrates ``spent`` from ``jobs.cost_so_far_usd`` at construction.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_agent.storage import db

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TokenUsage:
    """Provider-agnostic usage shape mirrored to the ``llm_calls`` table."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str | None = None


class BudgetExceeded(Exception):
    """Raised by :meth:`BudgetTracker.precheck` when the per-job cap is hit."""

    def __init__(self, job_id: str, spent: float, cap: float) -> None:
        self.job_id = job_id
        self.spent = spent
        self.cap = cap
        super().__init__(f"budget cap exceeded for job {job_id!r}: ${spent:.4f} >= ${cap:.4f}")


class BudgetTracker:
    """Track per-job cloud spend and enforce a USD cap.

    ``cap_usd`` of ``None`` disables enforcement (precheck always passes).
    State is loaded from ``jobs.cost_so_far_usd`` at construction so a
    daemon that restarts mid-run picks up the same running total.
    """

    def __init__(
        self,
        job_id: str,
        cap_usd: float | None,
        *,
        pricing: dict[str, dict[str, Any]] | None = None,
        db_path: Path | str | None = None,
    ) -> None:
        self.job_id = job_id
        self.cap = cap_usd
        self.pricing: dict[str, dict[str, Any]] = pricing or {}
        self.db_path = Path(db_path) if db_path is not None else db.DEFAULT_DB_PATH
        self.spent = self._load_from_db()
        self.last_cost: float = 0.0
        self._warned_90pct = False

    def _load_from_db(self) -> float:
        conn = db.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT cost_so_far_usd FROM jobs WHERE id = ?",
                (self.job_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or row[0] is None:
            return 0.0
        return float(row[0])

    def precheck(self, tier: str) -> None:  # noqa: ARG002 — tier reserved for future per-tier caps
        """Raise :class:`BudgetExceeded` if the running total has reached the cap.

        Logs a single ``WARNING`` once spend crosses 90% of the cap so an
        operator tailing logs gets a heads-up before enforcement bites.
        """
        if self.cap is None:
            return
        if self.spent >= self.cap:
            raise BudgetExceeded(self.job_id, self.spent, self.cap)
        if not self._warned_90pct and self.spent >= 0.9 * self.cap:
            logger.warning(
                "budget at 90%% for job %r: $%.4f / $%.4f",
                self.job_id,
                self.spent,
                self.cap,
            )
            self._warned_90pct = True

    def charge(
        self,
        tier: str,
        provider: str,
        model: str,
        usage: TokenUsage,
    ) -> float:
        """Compute cost from ``pricing`` block, ledger one row, bump running total.

        Inserts one row into ``llm_calls`` and updates ``jobs.cost_so_far_usd``
        in a single transaction so the rehydratable total never drifts from
        the per-call ledger. Returns the priced cost for the caller to embed
        in observability events.
        """
        cost = self._compute_cost(tier, usage)
        new_total = self.spent + cost
        ts = int(time.time())

        conn = db.connect(self.db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO llm_calls ("
                    " job_id, ts, tier, provider, model,"
                    " input_tokens, output_tokens, cached_tokens,"
                    " latency_ms, cost_usd, finish_reason"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.job_id,
                        ts,
                        tier,
                        provider,
                        model,
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.cached_tokens,
                        usage.latency_ms,
                        cost,
                        usage.finish_reason,
                    ),
                )
                conn.execute(
                    "UPDATE jobs SET cost_so_far_usd = ? WHERE id = ?",
                    (new_total, self.job_id),
                )
        finally:
            conn.close()

        self.spent = new_total
        self.last_cost = cost
        return cost

    def estimate_cost(self, tier: str, usage: TokenUsage) -> float:
        """Return the configured USD estimate for ``usage`` without ledger writes."""
        return self._compute_cost(tier, usage)

    def would_exceed(self, tier: str, usage: TokenUsage) -> bool:
        """Return True when estimated spend would push the job past its cap."""
        if self.cap is None:
            return False
        return self.spent + self.estimate_cost(tier, usage) > self.cap

    def _compute_cost(self, tier: str, usage: TokenUsage) -> float:
        block = self.pricing.get(tier)
        if not block:
            logger.warning("no pricing for tier %r; charging $0.00 for this call", tier)
            return 0.0
        try:
            input_rate = float(block.get("input_usd_per_mtok") or 0.0)
            output_rate = float(block.get("output_usd_per_mtok") or 0.0)
        except (TypeError, ValueError):
            logger.warning("unparseable pricing for tier %r: %r; charging $0.00", tier, block)
            return 0.0
        if input_rate <= 0.0 and output_rate <= 0.0:
            logger.warning("zero pricing for tier %r; charging $0.00 for this call", tier)
            return 0.0
        return (usage.input_tokens * input_rate + usage.output_tokens * output_rate) / 1_000_000


__all__ = ["BudgetExceeded", "BudgetTracker", "TokenUsage"]
