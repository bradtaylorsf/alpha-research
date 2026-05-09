"""Tests for ``research_agent.llm.router`` and the supporting BudgetTracker."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
import yaml
from openai import RateLimitError

from research_agent.llm.budgets import BudgetTracker, TokenUsage
from research_agent.llm.router import (
    EXPECTED_TIERS,
    LMSTUDIO_DEFAULT_BASE_URL,
    OPENROUTER_BASE_URL,
    Router,
    _is_retryable,
    load_models_config,
)
from research_agent.storage import db
from research_agent.storage.jobs import Job

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_MODELS_YAML = REPO_ROOT / "config" / "models.yaml"
SHIPPED_LOCAL_MODELS_YAML = REPO_ROOT / "config" / "models.local.yaml"


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
        router = Router(models_config, b, job=job, db_path=db_path)
        # Default tests off the OpenRouter retry loop so transient failure
        # tests don't burn two minutes of wall clock per attempt.
        router.openrouter_retry_min_delay = 0.0
        router.openrouter_retry_max_delay = 0.0
        router.openrouter_retry_stop_delay = 0.0
        return router

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
    def __init__(
        self,
        usage: _FakeUsage | None = None,
        *,
        output: str | None = None,
    ) -> None:
        self._usage = usage or _FakeUsage()
        self.finish_reason = "stop"
        self.output = output if output is not None else "fake-output"

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


class _TimeoutThenSuccessAgent:
    """Times out for N calls, then succeeds immediately.

    This lets router fallback tests assert that reroutes preserve the original
    agent object while overriding its model for the fallback tier.
    """

    def __init__(
        self,
        *,
        result: Any = None,
        timeout_attempts: int = 2,
    ) -> None:
        self.result = result if result is not None else _FakeResult()
        self.timeout_attempts = timeout_attempts
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if len(self.calls) <= self.timeout_attempts:
            await asyncio.sleep(1.0)
            return None  # pragma: no cover - cancelled by router timeout
        return self.result


def _override_model_name(call: tuple[tuple[Any, ...], dict[str, Any]]) -> str | None:
    return getattr(call[1].get("model"), "model_name", None)


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
        # Mirrors :class:`BudgetTracker.spent` so the router's reroute path
        # (which snapshots ``budget.spent`` before/after to compute per-reroute
        # cost) sees a realistic running total when this fake stands in.
        self.spent: float = 0.0

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
        self.spent += cost
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
    # Disable the OpenRouter retry loop so the rate-limit fallback test
    # doesn't loop for two minutes before the fallback_model path runs.
    router.openrouter_retry_min_delay = 0.0
    router.openrouter_retry_max_delay = 0.0
    router.openrouter_retry_stop_delay = 0.0

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


# ---------------------------------------------------------------------------
# Router ↔ LLMCache integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_skips_agent_run_and_budget_charge(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
    db_path: Path,
    tmp_path: Path,
    job: Job,
) -> None:
    """Same prompt+params twice → second call serves from cache."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    from research_agent.llm.cache import LLMCache

    cache = LLMCache(tmp_path / "llm_cache.sqlite")
    budget = _RecordingBudget()
    router = Router(
        models_config,
        budget,  # type: ignore[arg-type]
        job=job,
        db_path=db_path,
        cache=cache,
    )

    agent = _FakeAgent(result=_FakeResult(output="cached body"))

    # Miss → real call, cache write.
    out1 = await router.call("frontier_speed", agent, "summarize this", cache=True)
    assert out1.output == "cached body"
    assert len(agent.calls) == 1
    assert len(budget.precheck_calls) == 1
    assert len(budget.charge_calls) == 1

    # Hit → no agent.run, no precheck, no charge.
    out2 = await router.call("frontier_speed", agent, "summarize this", cache=True)
    assert out2.output == "cached body"
    assert len(agent.calls) == 1, "cache hit must not invoke agent.run again"
    assert len(budget.precheck_calls) == 1, "cache hit must skip budget precheck"
    assert len(budget.charge_calls) == 1, "cache hit must skip budget charge"

    # Event payload distinguishes hits via cached=True.
    events_path = job.root / "events.jsonl"
    import json as _json

    payloads = []
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        ev = _json.loads(line)
        if ev.get("kind") == "llm_call":
            payloads.append(ev["payload"])
    cached_flags = [p.get("cached") for p in payloads]
    assert cached_flags.count(True) == 1
    assert cached_flags.count(False) == 1
    cache.close()


@pytest.mark.asyncio
async def test_cache_default_off_does_not_consult_cache(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
    db_path: Path,
    tmp_path: Path,
    job: Job,
) -> None:
    """Without ``cache=True`` the router never reads or writes the cache."""
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    from research_agent.llm.cache import LLMCache, make_key

    cache = LLMCache(tmp_path / "llm_cache.sqlite")
    budget = _RecordingBudget()
    router = Router(
        models_config,
        budget,  # type: ignore[arg-type]
        job=job,
        db_path=db_path,
        cache=cache,
    )
    agent = _FakeAgent(result=_FakeResult(output="hello"))
    await router.call("general", agent, "x")
    # No write
    spec = models_config["tiers"]["general"]
    key = make_key(spec["provider"], spec["model"], "x", None, None)
    assert cache.get(key) is None
    cache.close()


@pytest.mark.asyncio
async def test_cache_keys_split_on_prompt_and_params(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
    db_path: Path,
    tmp_path: Path,
    job: Job,
) -> None:
    """Different prompt or sampling params → independent cache entries."""
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    from research_agent.llm.cache import LLMCache

    cache = LLMCache(tmp_path / "llm_cache.sqlite")
    budget = _RecordingBudget()
    router = Router(
        models_config,
        budget,  # type: ignore[arg-type]
        job=job,
        db_path=db_path,
        cache=cache,
    )

    a = _FakeAgent(result=_FakeResult(output="A"))
    b = _FakeAgent(result=_FakeResult(output="B"))
    c = _FakeAgent(result=_FakeResult(output="C"))

    await router.call("general", a, "prompt-1", cache=True)
    await router.call("general", b, "prompt-2", cache=True)
    await router.call("general", c, "prompt-1", cache=True, model_settings={"temperature": 0.7})

    # All three must have actually run — different cache keys.
    assert len(a.calls) == 1
    assert len(b.calls) == 1
    assert len(c.calls) == 1

    # Re-run prompt-1 with no params → hits the first entry.
    again = _FakeAgent(result=_FakeResult(output="should-not-be-used"))
    out = await router.call("general", again, "prompt-1", cache=True)
    assert out.output == "A"
    assert len(again.calls) == 0
    cache.close()


@pytest.mark.asyncio
async def test_cache_hit_emits_zero_cost_event(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
    db_path: Path,
    tmp_path: Path,
    job: Job,
) -> None:
    """The llm_call event for a hit shows zero tokens/cost."""
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    from research_agent.llm.cache import LLMCache, make_key

    cache = LLMCache(tmp_path / "llm_cache.sqlite")
    spec = models_config["tiers"]["general"]
    cache.put(
        make_key(spec["provider"], spec["model"], "preloaded prompt", None, None),
        "preloaded answer",
    )

    budget = _RecordingBudget()
    router = Router(
        models_config,
        budget,  # type: ignore[arg-type]
        job=job,
        db_path=db_path,
        cache=cache,
    )

    agent = _FakeAgent(result=_FakeResult(output="should-not-run"))
    out = await router.call("general", agent, "preloaded prompt", cache=True)
    assert out.output == "preloaded answer"
    assert len(agent.calls) == 0

    # No llm_calls ledger row written for a pure cache hit.
    conn = db.connect(db_path)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM llm_calls WHERE job_id = ?", (job.id,)).fetchone()
    finally:
        conn.close()
    assert rows[0] == 0

    import json as _json

    events_path = job.root / "events.jsonl"
    payloads = [
        _json.loads(line)["payload"]
        for line in events_path.read_text().splitlines()
        if line.strip() and _json.loads(line).get("kind") == "llm_call"
    ]
    assert len(payloads) == 1
    p = payloads[0]
    assert p["cached"] is True
    assert p["input_tokens"] == 0
    assert p["output_tokens"] == 0
    assert p["cost_usd"] == 0.0
    cache.close()


# ---------------------------------------------------------------------------
# LM Studio health check + auto-recovery (#36)
# ---------------------------------------------------------------------------


def _read_events_by_kind(job: Job, kind: str) -> list[dict[str, Any]]:
    """Pull all events of ``kind`` from the per-job ``events.jsonl``."""
    import json as _json

    out: list[dict[str, Any]] = []
    events_path = job.root / "events.jsonl"
    if not events_path.exists():
        return out
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        ev = _json.loads(line)
        if ev.get("kind") == kind:
            out.append(ev)
    return out


def test_models_yaml_ships_lmstudio_fallback_tiers() -> None:
    """Each lmstudio tier (except embeddings) must point at a cloud equivalent."""
    cfg = load_models_config(SHIPPED_MODELS_YAML)
    tiers = cfg["tiers"]
    assert tiers["fast"].get("fallback_tier") == "frontier_speed"
    assert tiers["general"].get("fallback_tier") == "frontier_speed"
    assert tiers["reasoner"].get("fallback_tier") == "frontier"
    assert tiers["vision"].get("fallback_tier") == "frontier_speed"
    # Embeddings intentionally omits fallback — no chat-tier substitute.
    assert "fallback_tier" not in tiers["embeddings"]


def test_local_models_yaml_routes_frontier_alt_to_frontier() -> None:
    """Local critique must have a declared same-family fallback tier."""
    cfg = load_models_config(SHIPPED_LOCAL_MODELS_YAML)
    tiers = cfg["tiers"]
    assert tiers["frontier_alt"]["provider"] == "lmstudio"
    assert tiers["frontier_alt"].get("fallback_tier") == "frontier"


def _make_lmstudio_router(
    *,
    job: Job,
    db_path: Path,
    fallback_tier: str | None = "frontier_speed",
    fast_timeout_s: float = 0.05,
    cloud_pricing: dict[str, dict[str, float]] | None = None,
) -> tuple[Router, _RecordingBudget]:
    """Stand up a Router whose ``fast`` tier hangs quickly + reroutes deterministically.

    Centralizes the boilerplate of: tiny ``timeout_s`` so timeouts fire fast,
    a zero retry-sleep so two timeouts complete in one event-loop tick, and a
    ``_RecordingBudget`` that tracks ``spent`` so the reroute path's
    before/after cost snapshot returns a meaningful number.
    """
    cfg: dict[str, Any] = {
        "tiers": {
            t: {"provider": "lmstudio", "model": "stub", "timeout_s": 60} for t in EXPECTED_TIERS
        },
    }
    cfg["tiers"]["fast"]["timeout_s"] = fast_timeout_s
    if fallback_tier is not None:
        cfg["tiers"]["fast"]["fallback_tier"] = fallback_tier
    cfg["tiers"]["frontier_speed"] = {
        "provider": "openrouter",
        "model": "anthropic/claude-haiku-4-5",
        "timeout_s": 60,
    }
    cfg["tiers"]["frontier"] = {
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4-7",
        "timeout_s": 600,
    }
    if cloud_pricing:
        cfg["pricing"] = cloud_pricing
    budget = _RecordingBudget(cost_per_call=0.42, db_path=db_path, job_id=job.id)
    router = Router(cfg, budget, job=job, db_path=db_path)  # type: ignore[arg-type]
    router.retry_sleep_s = 0.0
    # Reroute target is a cloud tier — keep its retry loop short so a
    # transient cloud failure inside a degrade-and-reroute test doesn't
    # stall for two minutes.
    router.openrouter_retry_min_delay = 0.0
    router.openrouter_retry_max_delay = 0.0
    router.openrouter_retry_stop_delay = 0.0
    return router, budget


@pytest.mark.asyncio
async def test_two_timeouts_marks_tier_degraded_and_routes_to_fallback(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    job: Job,
) -> None:
    """First call: timeout, retry, second timeout, degrade, then reroute to cloud."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    router, budget = _make_lmstudio_router(job=job, db_path=db_path)

    local_agent = _TimeoutThenSuccessAgent(result=_FakeResult(output="cloud body"))
    result = await router.call("fast", local_agent, "do work")

    assert result is local_agent.result
    assert "fast" in router._tier_degraded_until
    # Retry path: agent.run was attempted twice before we gave up on local.
    assert len(local_agent.calls) == 3
    assert _override_model_name(local_agent.calls[0]) is None
    assert _override_model_name(local_agent.calls[1]) is None
    # The fallback tier reuses the original agent so output_type/system prompt
    # are preserved, but overrides the model for the rerouted call.
    assert _override_model_name(local_agent.calls[2]) == "anthropic/claude-haiku-4-5"

    degraded = _read_events_by_kind(job, "lmstudio_degraded")
    rerouted = _read_events_by_kind(job, "lmstudio_rerouted")
    assert len(degraded) == 1
    assert degraded[0]["level"] == "WARN"
    assert degraded[0]["payload"]["tier"] == "fast"
    assert degraded[0]["payload"]["fallback_tier"] == "frontier_speed"
    assert degraded[0]["payload"]["until_ts"] >= int(time.time())

    assert len(rerouted) == 1
    assert rerouted[0]["level"] == "WARN"
    assert rerouted[0]["payload"]["original_tier"] == "fast"
    assert rerouted[0]["payload"]["fallback_tier"] == "frontier_speed"
    assert rerouted[0]["payload"]["cost_usd"] == pytest.approx(0.42)

    # The cloud reroute went through the normal cloud bookkeeping (precheck +
    # charge) so the budget side recorded one cloud call.
    assert budget.precheck_calls == ["frontier_speed"]
    assert len(budget.charge_calls) == 1


@pytest.mark.asyncio
async def test_frontier_alt_lmstudio_degradation_defaults_to_frontier_fallback(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    job: Job,
) -> None:
    """Critique's local frontier_alt tier falls through to frontier even without config."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    cfg: dict[str, Any] = {
        "tiers": {
            t: {"provider": "lmstudio", "model": "stub", "timeout_s": 60}
            for t in EXPECTED_TIERS
        },
    }
    cfg["tiers"]["frontier_alt"] = {
        "provider": "lmstudio",
        "model": "local-critic",
        "timeout_s": 0.05,
    }
    cfg["tiers"]["frontier"] = {
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4-7",
        "timeout_s": 60,
    }
    budget = _RecordingBudget(cost_per_call=0.17, db_path=db_path, job_id=job.id)
    router = Router(cfg, budget, job=job, db_path=db_path)  # type: ignore[arg-type]
    router.retry_sleep_s = 0.0
    router.openrouter_retry_min_delay = 0.0
    router.openrouter_retry_max_delay = 0.0
    router.openrouter_retry_stop_delay = 0.0

    local_agent = _TimeoutThenSuccessAgent(result=_FakeResult(output="frontier fallback"))
    result = await router.call("frontier_alt", local_agent, "critique context")

    assert result is local_agent.result
    assert len(local_agent.calls) == 3
    assert _override_model_name(local_agent.calls[2]) == "anthropic/claude-opus-4-7"
    assert budget.precheck_calls == ["frontier"]
    assert len(budget.charge_calls) == 1

    second_local_agent = _FakeAgent(result=_FakeResult(output="frontier fallback 2"))
    second = await router.call("frontier_alt", second_local_agent, "critique context 2")
    assert second is second_local_agent.result
    assert len(second_local_agent.calls) == 1
    assert _override_model_name(second_local_agent.calls[0]) == "anthropic/claude-opus-4-7"
    assert budget.precheck_calls == ["frontier", "frontier"]

    degraded = _read_events_by_kind(job, "lmstudio_degraded")
    rerouted = _read_events_by_kind(job, "lmstudio_rerouted")
    assert len(degraded) == 1
    assert degraded[0]["payload"]["tier"] == "frontier_alt"
    assert degraded[0]["payload"]["fallback_tier"] == "frontier"
    assert len(rerouted) == 2
    assert rerouted[0]["payload"]["original_tier"] == "frontier_alt"
    assert rerouted[0]["payload"]["fallback_tier"] == "frontier"
    assert rerouted[1]["payload"]["original_tier"] == "frontier_alt"
    assert rerouted[1]["payload"]["fallback_tier"] == "frontier"

    assert router.last_call_metadata is not None
    assert router.last_call_metadata["tier"] == "frontier"
    assert router.last_call_metadata["model"] == "anthropic/claude-opus-4-7"
    assert router.last_call_metadata["original_tier"] == "frontier_alt"
    assert router.last_call_metadata["rerouted"] is True


@pytest.mark.asyncio
async def test_calls_within_degraded_window_skip_local_and_reroute(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    job: Job,
) -> None:
    """Once the window is set, lmstudio is bypassed entirely until it expires."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    router, budget = _make_lmstudio_router(job=job, db_path=db_path)

    # Pretend the prior call already degraded the tier.
    router._tier_degraded_until["fast"] = time.time() + 600.0

    local_agent = _FakeAgent(result=_FakeResult(output="reroute"))

    for _ in range(3):
        out = await router.call("fast", local_agent, "x")
        assert out is local_agent.result

    # Local was never tried with the original model - the bypass kicks in and
    # every call uses the fallback model override immediately.
    assert len(local_agent.calls) == 3
    assert all(
        _override_model_name(call) == "anthropic/claude-haiku-4-5"
        for call in local_agent.calls
    )
    # Three reroutes → three cloud calls and three WARN events with cost.
    rerouted = _read_events_by_kind(job, "lmstudio_rerouted")
    assert len(rerouted) == 3
    for ev in rerouted:
        assert ev["level"] == "WARN"
        assert ev["payload"]["original_tier"] == "fast"
        assert ev["payload"]["fallback_tier"] == "frontier_speed"
        assert ev["payload"]["cost_usd"] == pytest.approx(0.42)
    # No degrade event this time — the window was pre-existing, not freshly set.
    assert _read_events_by_kind(job, "lmstudio_degraded") == []


@pytest.mark.asyncio
async def test_local_recovery_after_window_expires_emits_recovered(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    job: Job,
) -> None:
    """Once the window has elapsed, the next successful local call clears the flag."""
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    router, _budget = _make_lmstudio_router(job=job, db_path=db_path, fast_timeout_s=60)
    # Pre-set an *expired* degraded window. The next call must (a) not reroute
    # (window is in the past) and (b) clear the flag on success.
    router._tier_degraded_until["fast"] = time.time() - 1.0

    healthy_local = _FakeAgent(result=_FakeResult(output="local back"))
    out = await router.call("fast", healthy_local, "x")
    assert out is healthy_local.result

    assert "fast" not in router._tier_degraded_until
    recovered = _read_events_by_kind(job, "lmstudio_recovered")
    assert len(recovered) == 1
    assert recovered[0]["level"] == "INFO"
    assert recovered[0]["payload"] == {"tier": "fast"}
    # Sanity: a normal llm_call event was still emitted for the local call.
    llm_calls = _read_events_by_kind(job, "llm_call")
    assert len(llm_calls) == 1


@pytest.mark.asyncio
async def test_two_timeouts_without_fallback_tier_propagates_timeout(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    job: Job,
) -> None:
    """When ``fallback_tier`` is omitted (e.g. embeddings) the second timeout raises."""
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    router, budget = _make_lmstudio_router(job=job, db_path=db_path, fallback_tier=None)
    local_agent = _FakeAgent(sleep_s=1.0)
    with pytest.raises(asyncio.TimeoutError):
        await router.call("fast", local_agent, "x")

    # The tier is still marked degraded — operator can see the problem.
    assert "fast" in router._tier_degraded_until
    degraded = _read_events_by_kind(job, "lmstudio_degraded")
    assert len(degraded) == 1
    assert degraded[0]["payload"]["fallback_tier"] is None
    # And no reroute was attempted.
    assert _read_events_by_kind(job, "lmstudio_rerouted") == []
    assert budget.charge_calls == []


@pytest.mark.asyncio
async def test_first_timeout_then_retry_succeeds_does_not_degrade(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    job: Job,
) -> None:
    """A single transient timeout that recovers on retry must not flip the tier."""
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    router, _budget = _make_lmstudio_router(job=job, db_path=db_path)

    success_result = _FakeResult(output="recovered on retry")

    class _OneShotHangingAgent:
        """Hangs on first .run(), succeeds instantly on the second."""

        def __init__(self) -> None:
            self.calls = 0

        async def run(self, *args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(10.0)
                return None  # pragma: no cover — should be cancelled by timeout
            return success_result

    agent = _OneShotHangingAgent()
    out = await router.call("fast", agent, "x")
    assert out is success_result
    assert agent.calls == 2
    assert "fast" not in router._tier_degraded_until
    assert _read_events_by_kind(job, "lmstudio_degraded") == []
    assert _read_events_by_kind(job, "lmstudio_rerouted") == []


# ---------------------------------------------------------------------------
# OpenRouter network-blip handling (#37)
# ---------------------------------------------------------------------------


def _api_status_error(status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return openai.APIStatusError("boom", response=response, body=None)


def _bad_request_error() -> openai.BadRequestError:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(400, request=request)
    return openai.BadRequestError("bad request", response=response, body=None)


def _api_connection_error() -> openai.APIConnectionError:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return openai.APIConnectionError(request=request)


def _fast_retry_router(
    models_config: dict[str, Any],
    db_path: Path,
    job: Job,
) -> tuple[Router, _RecordingBudget]:
    """Build a router with retry collapsed to ~zero so retry tests are <1s."""
    budget = _RecordingBudget(cost_per_call=0.0, db_path=db_path, job_id=job.id)
    router = Router(models_config, budget, job=job, db_path=db_path)  # type: ignore[arg-type]
    # Allow several retries inside a tight wall-clock window so tests can
    # observe the loop iterating without actually waiting.
    router.openrouter_retry_min_delay = 0.0
    router.openrouter_retry_max_delay = 0.0
    router.openrouter_retry_stop_delay = 0.5
    return router, budget


def test_is_retryable_classifies_transient_vs_terminal() -> None:
    # Transient — should retry.
    assert _is_retryable(_rate_limit_error()) is True
    assert _is_retryable(_api_status_error(503)) is True
    assert _is_retryable(_api_status_error(500)) is True
    assert _is_retryable(_api_connection_error()) is True
    assert _is_retryable(httpx.ConnectError("nope")) is True
    assert _is_retryable(httpx.ReadTimeout("slow")) is True

    # Terminal — must not retry.
    assert _is_retryable(_bad_request_error()) is False
    assert _is_retryable(_api_status_error(404)) is False
    assert _is_retryable(_api_status_error(401)) is False
    assert _is_retryable(_api_status_error(422)) is False
    assert _is_retryable(RuntimeError("nope")) is False


@pytest.mark.asyncio
async def test_openrouter_retries_on_connect_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
    db_path: Path,
    job: Job,
) -> None:
    """Two transient httpx.ConnectError followed by success → one charged call."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    router, budget = _fast_retry_router(models_config, db_path, job)

    agent = _FakeAgent(
        result=_FakeResult(),
        raises=[httpx.ConnectError("blip 1"), httpx.ConnectError("blip 2")],
    )
    result = await router.call("frontier_speed", agent, "summarize")

    assert result is agent.result
    # Three .run() invocations: two failures + one success.
    assert len(agent.calls) == 3
    # Single ledger row for the eventual success (no charge for failed attempts).
    assert len(budget.charge_calls) == 1

    llm_calls = _read_events_by_kind(job, "llm_call")
    assert len(llm_calls) == 1


@pytest.mark.asyncio
async def test_openrouter_retries_on_503_until_stop_delay(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
    db_path: Path,
    job: Job,
) -> None:
    """Continuous 503s → retry until stop_after_delay fires, then APIStatusError propagates."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    router, budget = _fast_retry_router(models_config, db_path, job)

    class _Always503Agent:
        """Pydantic AI agent stand-in that always raises an APIStatusError(503)."""

        def __init__(self) -> None:
            self.calls = 0

        async def run(self, *args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            raise _api_status_error(503)

    agent = _Always503Agent()
    with pytest.raises(openai.APIStatusError) as excinfo:
        await router.call("frontier_speed", agent, "summarize")
    assert excinfo.value.status_code == 503

    # The loop must have actually retried (otherwise we'd have a single
    # attempt and the retry contract isn't met).
    assert agent.calls >= 2
    # No success → no charge, no llm_call event.
    assert budget.charge_calls == []
    assert _read_events_by_kind(job, "llm_call") == []


@pytest.mark.asyncio
async def test_openrouter_retries_on_rate_limit_until_fallback_kicks_in(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
    db_path: Path,
    job: Job,
) -> None:
    """RateLimitError survives retries, then the fallback_model branch runs once."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    router, budget = _fast_retry_router(models_config, db_path, job)

    # Override fallback_model construction so we can observe a single call on it.
    fallback_agent = _FakeAgent(result=_FakeResult(output="fb"))
    monkeypatch.setattr(router, "_make_fallback_agent", lambda *_a, **_k: fallback_agent)

    class _AlwaysRateLimitedAgent:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, *args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            raise _rate_limit_error()

    primary = _AlwaysRateLimitedAgent()
    result = await router.call("frontier", primary, "synthesize")

    assert result is fallback_agent.result
    # Primary was retried at least twice (proves the retry loop ran) before
    # the stop_delay fired and the fallback path kicked in.
    assert primary.calls >= 2
    assert len(fallback_agent.calls) == 1
    # Fallback charged exactly once.
    assert len(budget.charge_calls) == 1


@pytest.mark.asyncio
async def test_openrouter_does_not_retry_on_bad_request(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
    db_path: Path,
    job: Job,
) -> None:
    """A 400 BadRequestError is terminal — propagates after a single attempt."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    router, budget = _fast_retry_router(models_config, db_path, job)

    agent = _FakeAgent(raises=[_bad_request_error()])

    with pytest.raises(openai.BadRequestError):
        await router.call("frontier_speed", agent, "summarize")

    # Exactly one attempt — no retry.
    assert len(agent.calls) == 1
    assert budget.charge_calls == []


@pytest.mark.asyncio
async def test_lmstudio_path_is_not_wrapped_in_openrouter_retry(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
    db_path: Path,
    job: Job,
) -> None:
    """A transient httpx error on a local tier must propagate (no cloud retry)."""
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    router, budget = _fast_retry_router(models_config, db_path, job)

    agent = _FakeAgent(raises=[httpx.ConnectError("local blip")])

    with pytest.raises(httpx.ConnectError):
        await router.call("general", agent, "x")

    # Exactly one attempt — local-tier failures are not silently retried by
    # the OpenRouter retry wrapper. The §16 rule is loud failures, not silent
    # reroutes.
    assert len(agent.calls) == 1
    assert budget.charge_calls == []
