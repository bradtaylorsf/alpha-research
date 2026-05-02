"""Per-job cost cap enforcement (implementation guide §9).

This module exposes the minimal contract the LLM router depends on:

- :class:`TokenUsage` — provider-agnostic shape for the numbers we ledger.
- :class:`BudgetExceeded` — raised by ``precheck()`` once the cap is hit.
- :class:`BudgetTracker` — ``precheck()`` before cloud calls, ``charge()`` after.

The full per-model cost table lives in ``config/models.yaml`` and will be
consumed by a richer pricing implementation in a later issue. For now
``charge()`` accepts an explicit ``cost_usd`` from the caller and bumps the
in-memory running total. The :mod:`router` is the single writer of the
``llm_calls`` ledger row, so a cloud call produces exactly one row no matter
which path it took. ``BudgetTracker`` re-hydrates its running total from that
ledger at construction so a daemon that restarts mid-run picks up where it
left off.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from research_agent.storage import db


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
    State is loaded from the ``llm_calls`` ledger at construction so a daemon
    that restarts mid-run picks up the same running total.
    """

    def __init__(
        self,
        job_id: str,
        cap_usd: float | None,
        *,
        db_path: Path | str | None = None,
    ) -> None:
        self.job_id = job_id
        self.cap = cap_usd
        self.db_path = Path(db_path) if db_path is not None else db.DEFAULT_DB_PATH
        self.spent = self._load_from_db()

    def _load_from_db(self) -> float:
        conn = db.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE job_id = ?",
                (self.job_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return 0.0
        return float(row[0] or 0.0)

    def precheck(self, tier: str) -> None:  # noqa: ARG002 — tier reserved for future per-tier caps
        """Raise :class:`BudgetExceeded` if the running total has reached the cap."""
        if self.cap is None:
            return
        if self.spent >= self.cap:
            raise BudgetExceeded(self.job_id, self.spent, self.cap)

    def charge(
        self,
        tier: str,  # noqa: ARG002 — tier reserved for future per-tier accounting
        provider: str,  # noqa: ARG002 — same
        model: str,  # noqa: ARG002 — same
        usage: TokenUsage,  # noqa: ARG002 — same
        cost_usd: float,
    ) -> None:
        """Bump the in-memory running total by ``cost_usd``.

        The :mod:`router` writes the actual ``llm_calls`` ledger row so the
        ledger has exactly one row per call. Keeping this in-memory makes
        ``precheck`` cheap and lets the cap survive a daemon restart by
        reloading from the ledger in :meth:`__init__`.
        """
        self.spent += cost_usd


__all__ = ["BudgetExceeded", "BudgetTracker", "TokenUsage"]
