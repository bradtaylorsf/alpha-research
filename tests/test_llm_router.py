"""Tests for ``research_agent.llm.router`` and the supporting BudgetTracker."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from openai import RateLimitError

from research_agent.llm.budgets import BudgetTracker, TokenUsage
from research_agent.llm.router import (
    EXPECTED_TIERS,
    LMSTUDIO_DEFAULT_BASE_URL,
    OPENROUTER_BASE_URL,
    Router,
    load_models_config,
)
from research_agent.storage import db
from research_agent.storage.jobs import Job

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_MODELS_YAML = REPO_ROOT / "config" / "models.yaml"


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
        {"goal": "Investigate router"},
        jobs_root=jobs_root,
        db_path=db_path,
    )


@pytest.fixture
def models_config() -> dict[str, Any]:
    return load_models_config(SHIPPED_MODELS_YAML)


@pytest.fixture
def budget(db_path: Path) -> BudgetTracker:
    return BudgetTracker("budget-test", cap_usd=None, db_path=db_path)


@pytest.fixture
def make_router(models_config: dict[str, Any], db_path: Path):
    def _factory(
        *,
        budget: BudgetTracker | None = None,
        job: Job | None = None,
    ) -> Router:
        b = (
            budget
            if budget is not None
            else BudgetTracker(
                job.id if job is not None else "router-test",
                cap_usd=None,
                db_path=db_path,
            )
        )
        return Router(models_config, b, job=job, db_path=db_path)

    return _factory


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeUsage:
    def __init__(
        self,
        input_tokens: int = 12,
        output_tokens: int = 34,
        cache_read_tokens: int = 5,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_tokens = cache_read_tokens


class _FakeResult:
    def __init__(self, usage: _FakeUsage | None = None) -> None:
        self._usage = usage or _FakeUsage()
        self.finish_reason = "stop"

    def usage(self) -> _FakeUsage:
        return self._usage


class _FakeAgent:
    """Minimal stand-in for `pydantic_ai.Agent` — exposes async ``run``."""

    def __init__(
        self,
        *,
        result: Any = None,
        raises: list[BaseException] | None = None,
        sleep_s: float = 0.0,
    ) -> None:
        self.result = result if result is not None else _FakeResult()
        self.raises = list(raises or [])
        self.sleep_s = sleep_s
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self.sleep_s:
            await asyncio.sleep(self.sleep_s)
        if self.raises:
            raise self.raises.pop(0)
        return self.result


def _rate_limit_error() -> RateLimitError:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(429, request=request)
    return RateLimitError("rate limited", response=response, body=None)


# ---------------------------------------------------------------------------
# load_models_config + shipped tier table
# ---------------------------------------------------------------------------


def test_load_models_config_ships_with_tier_table() -> None:
    cfg = load_models_config(SHIPPED_MODELS_YAML)
    tiers = cfg["tiers"]

    for t in EXPECTED_TIERS:
        assert t in tiers, f"missing tier: {t}"

    expected_provider = {
        "fast": "lmstudio",
        "general": "lmstudio",
        "reasoner": "lmstudio",
        "vision": "lmstudio",
        "embeddings": "lmstudio",
        "frontier": "openrouter",
        "frontier_alt": "openrouter",
        "frontier_speed": "openrouter",
    }
    expected_model = {
        "fast": "qwen3-4b-instruct-q4_k_m",
        "general": "qwen3-32b-instruct-q6_k",
        "reasoner": "deepseek-r1-distill-32b-q6_k",
        "vision": "qwen3-vl-8b-instruct",
        "embeddings": "qwen3-embedding-4b",
        "frontier": "anthropic/claude-opus-4-7",
        "frontier_alt": "moonshotai/kimi-k2-1t",
        "frontier_speed": "anthropic/claude-haiku-4-5",
    }
    for tier, provider in expected_provider.items():
        assert tiers[tier]["provider"] == provider
        assert tiers[tier]["model"] == expected_model[tier]
    assert tiers["frontier"]["fallback_model"] == "openai/gpt-5"


def test_load_models_config_raises_when_tiers_missing(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"something_else": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="tiers"):
        load_models_config(bad)


# ---------------------------------------------------------------------------
# model_for(tier)
# ---------------------------------------------------------------------------


def test_model_for_lmstudio_tier_uses_local_base_url(
    monkeypatch: pytest.MonkeyPatch,
    make_router,
) -> None:
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    router = make_router()
    model = router.model_for("fast")

    base_url = str(model.provider.base_url).rstrip("/")
    assert base_url == LMSTUDIO_DEFAULT_BASE_URL.rstrip("/")
    # api_key is not strictly required for LM Studio but the provider stores it.
    assert model.provider.client.api_key == "lm-studio"


def test_model_for_lmstudio_respects_env_override(
    monkeypatch: pytest.MonkeyPatch,
    make_router,
) -> None:
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://lmstudio.local:9999/v1")
    router = make_router()
    model = router.model_for("general")
    base_url = str(model.provider.base_url).rstrip("/")
    assert base_url == "http://lmstudio.local:9999/v1"


def test_model_for_openrouter_tier_uses_cloud_base_url_and_env_key(
    monkeypatch: pytest.MonkeyPatch,
    make_router,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-12345")
    router = make_router()
    model = router.model_for("frontier")

    base_url = str(model.provider.base_url).rstrip("/")
    assert base_url == OPENROUTER_BASE_URL.rstrip("/")
    assert model.provider.client.api_key == "sk-or-test-12345"


def test_missing_openrouter_key_raises_clearly(
    monkeypatch: pytest.MonkeyPatch,
    make_router,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    router = make_router()
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        router.model_for("frontier")


def test_model_for_caches_per_tier(
    monkeypatch: pytest.MonkeyPatch,
    make_router,
) -> None:
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    router = make_router()
    a = router.model_for("fast")
    b = router.model_for("fast")
    assert a is b


def test_router_construction_validates_required_tiers(
    db_path: Path,
) -> None:
    bad_cfg = {"tiers": {"fast": {"provider": "lmstudio", "model": "x", "timeout_s": 1}}}
    budget = BudgetTracker("x", cap_usd=None, db_path=db_path)
    with pytest.raises(ValueError, match="missing required tiers"):
        Router(bad_cfg, budget, db_path=db_path)


# ---------------------------------------------------------------------------
# call(...) — budget interaction
# ---------------------------------------------------------------------------


class _RecordingBudget:
    """Test double for :class:`BudgetTracker`.

    Mirrors the production contract: ``charge`` returns the priced cost and
    is the single writer of the cloud ``llm_calls`` row, so cloud-tier tests
    that assert ledger state see exactly what production would emit.
    """

    def __init__(
        self,
        *,
        cost_per_call: float = 0.0,
        db_path: Path | None = None,
        job_id: str | None = None,
    ) -> None:
        self.precheck_calls: list[str] = []
        self.charge_calls: list[tuple[str, str, str, TokenUsage]] = []
        self._cost = cost_per_call
        self._db_path = db_path
        self._job_id = job_id
        self.last_cost: float = 0.0

    def precheck(self, tier: str) -> None:
        self.precheck_calls.append(tier)

    def charge(
        self,
        tier: str,
        provider: str,
        model: str,
        usage: TokenUsage,
    ) -> float:
        self.charge_calls.append((tier, provider, model, usage))
        cost = self._cost
        if self._db_path is not None and self._job_id is not None:
            ts = int(time.time())
            conn = db.connect(self._db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO llm_calls ("
                        " job_id, ts, tier, provider, model,"
                        " input_tokens, output_tokens, cached_tokens,"
                        " latency_ms, cost_usd, finish_reason"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            self._job_id,
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
            finally:
                conn.close()
        self.last_cost = cost
        return cost


@pytest.mark.asyncio
async def test_call_runs_precheck_and_charge_for_cloud(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
    db_path: Path,
    job: Job,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    budget = _RecordingBudget()
    router = Router(models_config, budget, job=job, db_path=db_path)  # type: ignore[arg-type]

    agent = _FakeAgent()
    result = await router.call("frontier_speed", agent, "hello")

    assert result is agent.result
    assert budget.precheck_calls == ["frontier_speed"]
    assert len(budget.charge_calls) == 1
    tier, provider, model, usage = budget.charge_calls[0]
    assert tier == "frontier_speed"
    assert provider == "openrouter"
    assert model == "anthropic/claude-haiku-4-5"
    assert usage.input_tokens == 12
    assert usage.output_tokens == 34
    assert usage.cached_tokens == 5


@pytest.mark.asyncio
async def test_call_skips_budget_for_local(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
    db_path: Path,
    job: Job,
) -> None:
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    budget = _RecordingBudget()
    router = Router(models_config, budget, job=job, db_path=db_path)  # type: ignore[arg-type]

    agent = _FakeAgent()
    await router.call("fast", agent, "hello")

    assert budget.precheck_calls == []
    assert budget.charge_calls == []


# ---------------------------------------------------------------------------
# call(...) — fallback semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ratelimit_falls_back_to_fallback_model_for_cloud(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
    db_path: Path,
    job: Job,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    budget = BudgetTracker(
        job.id,
        cap_usd=None,
        pricing=models_config.get("pricing"),
        db_path=db_path,
    )
    router = Router(models_config, budget, job=job, db_path=db_path)

    fallback_agent = _FakeAgent(result=_FakeResult())
    monkeypatch.setattr(
        router,
        "_make_fallback_agent",
        lambda tier, fm: fallback_agent,
    )

    primary_agent = _FakeAgent(raises=[_rate_limit_error()])
    result = await router.call("frontier", primary_agent, "synthesize this")

    assert result is fallback_agent.result
    assert len(primary_agent.calls) == 1
    assert len(fallback_agent.calls) == 1

    # llm_calls row should be tagged with the fallback model and carry the
    # priced cost computed from frontier-tier rates.
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT model, tier, provider, cost_usd FROM llm_calls WHERE job_id = ?",
            (job.id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["model"] == "openai/gpt-5"
    assert row["tier"] == "frontier"
    assert row["provider"] == "openrouter"
    pricing = models_config["pricing"]["frontier"]
    expected_cost = (
        12 * float(pricing["input_usd_per_mtok"]) + 34 * float(pricing["output_usd_per_mtok"])
    ) / 1_000_000
    assert row["cost_usd"] == pytest.approx(expected_cost)

    # Event payload should mark fallback_used=True.
    events_path = job.root / "events.jsonl"
    lines = [line for line in events_path.read_text().splitlines() if line]
    payloads = [_json_payload(line) for line in lines if '"llm_call"' in line]
    llm_call_payloads = [p for p in payloads if p is not None]
    assert any(p.get("fallback_used") is True for p in llm_call_payloads)
    assert any(p.get("model") == "openai/gpt-5" for p in llm_call_payloads)


def _json_payload(line: str) -> dict[str, Any] | None:
    import json as _json

    try:
        ev = _json.loads(line)
    except _json.JSONDecodeError:
        return None
    if ev.get("kind") != "llm_call":
        return None
    payload = ev.get("payload")
    return payload if isinstance(payload, dict) else None


@pytest.mark.asyncio
async def test_local_failure_does_not_silently_reroute_to_cloud(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
    db_path: Path,
    job: Job,
) -> None:
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    budget = _RecordingBudget()
    router = Router(models_config, budget, job=job, db_path=db_path)  # type: ignore[arg-type]

    def _fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("fallback path must not run for lmstudio failures")

    monkeypatch.setattr(router, "_make_fallback_agent", _fail_if_called)

    boom = RuntimeError("LM Studio process crashed")
    agent = _FakeAgent(raises=[boom])

    with pytest.raises(RuntimeError, match="LM Studio process crashed"):
        await router.call("general", agent, "extract")

    # Nothing should have been charged or logged.
    assert budget.charge_calls == []
    conn = db.connect(db_path)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM llm_calls WHERE job_id = ?", (job.id,)).fetchone()
    finally:
        conn.close()
    assert rows[0] == 0


@pytest.mark.asyncio
async def test_ratelimit_on_local_tier_reraises(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
    db_path: Path,
    job: Job,
) -> None:
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    budget = _RecordingBudget()
    router = Router(models_config, budget, job=job, db_path=db_path)  # type: ignore[arg-type]

    monkeypatch.setattr(
        router,
        "_make_fallback_agent",
        lambda *a, **k: pytest.fail("local tier must not invoke openrouter fallback"),
    )

    agent = _FakeAgent(raises=[_rate_limit_error()])
    with pytest.raises(RateLimitError):
        await router.call("fast", agent, "x")


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_tier_timeout_is_enforced(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    job: Job,
) -> None:
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    cfg = {
        "tiers": {
            t: {"provider": "lmstudio", "model": "stub", "timeout_s": 60} for t in EXPECTED_TIERS
        }
    }
    # Clamp the tier we'll exercise to a tiny timeout.
    cfg["tiers"]["fast"]["timeout_s"] = 0.05  # type: ignore[index]
    budget = _RecordingBudget()
    router = Router(cfg, budget, job=job, db_path=db_path)  # type: ignore[arg-type]

    agent = _FakeAgent(sleep_s=1.0)
    with pytest.raises(asyncio.TimeoutError):
        await router.call("fast", agent, "slow")


# ---------------------------------------------------------------------------
# Ledger + event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_emits_llm_call_event_and_inserts_llm_calls_row(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
    db_path: Path,
    job: Job,
) -> None:
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    budget = _RecordingBudget()
    router = Router(models_config, budget, job=job, db_path=db_path)  # type: ignore[arg-type]

    agent = _FakeAgent()
    await router.call("general", agent, "summarize")

    conn = db.connect(db_path)
    try:
        llm_rows = conn.execute(
            "SELECT tier, provider, model, input_tokens, output_tokens, cached_tokens,"
            " latency_ms, finish_reason FROM llm_calls WHERE job_id = ?",
            (job.id,),
        ).fetchall()
        event_rows = conn.execute(
            "SELECT kind, actor FROM events WHERE job_id = ? AND kind = 'llm_call'",
            (job.id,),
        ).fetchall()
    finally:
        conn.close()

    assert len(llm_rows) == 1
    row = llm_rows[0]
    assert row["tier"] == "general"
    assert row["provider"] == "lmstudio"
    assert row["model"] == "qwen3-32b-instruct-q6_k"
    assert row["input_tokens"] == 12
    assert row["output_tokens"] == 34
    assert row["cached_tokens"] == 5
    assert row["latency_ms"] >= 0
    assert row["finish_reason"] == "stop"

    assert len(event_rows) == 1
    assert event_rows[0]["kind"] == "llm_call"
    assert event_rows[0]["actor"] == "router"


# ---------------------------------------------------------------------------
# BudgetTracker
# ---------------------------------------------------------------------------


def test_budget_precheck_raises_when_cap_reached(db_path: Path) -> None:
    from research_agent.llm.budgets import BudgetExceeded

    bt = BudgetTracker("budget-1", cap_usd=1.0, db_path=db_path)
    bt.spent = 1.0
    with pytest.raises(BudgetExceeded):
        bt.precheck("frontier")


def test_budget_precheck_no_cap_is_noop(db_path: Path) -> None:
    bt = BudgetTracker("budget-2", cap_usd=None, db_path=db_path)
    bt.spent = 999.0
    bt.precheck("frontier")  # must not raise


def test_budget_rehydrates_running_total_from_jobs_row(job: Job, db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE jobs SET cost_so_far_usd = ? WHERE id = ?",
                (2.25, job.id),
            )
    finally:
        conn.close()

    bt = BudgetTracker(job.id, cap_usd=10.0, db_path=db_path)
    assert bt.spent == pytest.approx(2.25)


def test_budget_charge_persists_to_ledger_and_jobs_total(job: Job, db_path: Path) -> None:
    pricing = {
        "frontier": {"input_usd_per_mtok": 10.0, "output_usd_per_mtok": 30.0},
    }
    bt = BudgetTracker(job.id, cap_usd=10.0, pricing=pricing, db_path=db_path)
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=500_000)
    cost = bt.charge("frontier", "openrouter", "anthropic/claude-opus-4-7", usage)

    expected = 1_000_000 * 10.0 / 1_000_000 + 500_000 * 30.0 / 1_000_000
    assert cost == pytest.approx(expected)
    assert bt.spent == pytest.approx(expected)

    conn = db.connect(db_path)
    try:
        rows = conn.execute("SELECT cost_usd FROM llm_calls WHERE job_id = ?", (job.id,)).fetchall()
        cost_so_far = conn.execute(
            "SELECT cost_so_far_usd FROM jobs WHERE id = ?", (job.id,)
        ).fetchone()[0]
    finally:
        conn.close()
    # charge() is the single writer of the cloud ledger row.
    assert len(rows) == 1
    assert rows[0]["cost_usd"] == pytest.approx(expected)
    assert cost_so_far == pytest.approx(expected)
