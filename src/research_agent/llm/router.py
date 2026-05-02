"""Tier → provider routing for every LLM call (implementation guide §8).

The router is the single chokepoint between the orchestrator and any model.
Business logic picks a logical *tier* (``fast``, ``general``, ``frontier``…)
and the router maps it to a Pydantic AI :class:`OpenAIModel` bound to either
LM Studio (local, no cost) or OpenRouter (cloud, cost-tracked).

Per §16 the router never silently falls back from a local tier to a cloud
tier — that would let a paid call slip in disguised as a free one. Cloud
tiers can fall back to a configured ``fallback_model`` (still cloud) on
:class:`openai.RateLimitError`.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
from openai import RateLimitError
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from research_agent.config import get as cfg_get
from research_agent.observability.events import emit
from research_agent.storage import db
from research_agent.storage.jobs import Job

from .budgets import BudgetTracker, TokenUsage
from .cache import LLMCache, make_key

Tier = Literal[
    "fast",
    "general",
    "reasoner",
    "vision",
    "embeddings",
    "frontier",
    "frontier_alt",
    "frontier_speed",
]

EXPECTED_TIERS: tuple[str, ...] = (
    "fast",
    "general",
    "reasoner",
    "vision",
    "embeddings",
    "frontier",
    "frontier_alt",
    "frontier_speed",
)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
LMSTUDIO_DEFAULT_BASE_URL = "http://localhost:1234/v1"


def load_models_config(path: Path | str = "config/models.yaml") -> dict[str, Any]:
    """Parse ``config/models.yaml`` and return the raw dict.

    Raises :class:`ValueError` if the top-level ``tiers`` key is missing —
    every other layer of the system assumes it exists.
    """
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "tiers" not in data:
        raise ValueError(f"models config at {p} missing required top-level 'tiers' key")
    if not isinstance(data["tiers"], dict):
        raise ValueError(f"'tiers' in {p} must be a mapping; got {type(data['tiers']).__name__}")
    return data


_PARAM_KEYS: tuple[str, ...] = ("temperature", "top_p", "top_k")


class _CachedResult:
    """Lightweight stand-in for a Pydantic AI ``AgentRunResult`` on cache hits.

    Exposes the same surface the rest of the router/orchestrator expects:
    ``.output`` (the cached string) and a ``usage()`` callable returning a
    zero-token :class:`TokenUsage`. No tokens were spent, so cost is $0.
    """

    __slots__ = ("output", "_usage", "finish_reason")

    def __init__(self, output: str) -> None:
        self.output = output
        self._usage = TokenUsage()
        self.finish_reason = "cache"

    def usage(self) -> TokenUsage:
        return self._usage


def _extract_prompt(args: tuple[Any, ...]) -> str:
    """Pull the prompt string out of ``agent.run`` positional args.

    Pydantic AI's ``Agent.run`` takes the prompt as the first positional arg
    when it's a plain string; for richer inputs we fall back to ``repr`` so
    the cache key still varies with the input shape.
    """
    if not args:
        return ""
    head = args[0]
    if isinstance(head, str):
        return head
    return repr(args)


def _extract_params(kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the cache-relevant sampling params out of ``agent.run`` kwargs."""
    settings = kwargs.get("model_settings")
    if settings is None:
        return None
    if isinstance(settings, dict):
        source: dict[str, Any] = settings
    else:
        source = {k: getattr(settings, k, None) for k in _PARAM_KEYS}
    out = {k: source[k] for k in _PARAM_KEYS if source.get(k) is not None}
    return out or None


def _extract_tool_defs(agent: Any) -> Any:
    """Probe a Pydantic AI ``Agent`` for its tool definitions, if any.

    The exact attribute name has shifted across pydantic-ai releases. We
    probe a small allow-list and return a lightweight ``[{name, schema}]``
    list — enough to make the cache key sensitive to tool changes without
    coupling to a specific internal type.
    """
    for attr in ("_function_tools", "tools", "_tools"):
        defs = getattr(agent, attr, None)
        if defs:
            return _serialize_tool_defs(defs)
    return None


def _serialize_tool_defs(defs: Any) -> list[dict[str, Any]]:
    items = list(defs.values()) if isinstance(defs, dict) else list(defs)
    out: list[dict[str, Any]] = []
    for t in items:
        name = getattr(t, "name", None) or getattr(t, "__name__", None) or t.__class__.__name__
        schema = (
            getattr(t, "parameters_json_schema", None)
            or getattr(t, "json_schema", None)
            or getattr(t, "schema", None)
        )
        out.append({"name": str(name), "schema": schema})
    return out


def _build_model_for_tier(tier: str, spec: dict[str, Any]) -> OpenAIModel:
    """Build an :class:`OpenAIModel` bound to the provider configured for ``tier``.

    Module-level so both :class:`Router` and the smoke helper share one wiring
    point — the LM Studio base-url override and the OpenRouter env-key check
    must stay identical between the production call path and the smoke probe.
    """
    provider = spec["provider"]
    model_name = spec["model"]
    if provider == "lmstudio":
        base_url = cfg_get("LMSTUDIO_BASE_URL") or LMSTUDIO_DEFAULT_BASE_URL
        api_key = "lm-studio"
    elif provider == "openrouter":
        api_key_env = os.environ.get("OPENROUTER_API_KEY")
        if not api_key_env:
            raise RuntimeError(
                f"OPENROUTER_API_KEY environment variable is required for "
                f"cloud tier {tier!r} (provider=openrouter)"
            )
        base_url = OPENROUTER_BASE_URL
        api_key = api_key_env
    else:
        raise ValueError(
            f"unknown provider for tier {tier!r}: {provider!r} "
            "(expected 'lmstudio' or 'openrouter')"
        )
    return OpenAIModel(
        model_name,
        provider=OpenAIProvider(base_url=base_url, api_key=api_key),
    )


def _extract_usage(result: Any, latency_ms: int) -> TokenUsage:
    """Map a Pydantic AI ``AgentRunResult.usage()`` into our :class:`TokenUsage`."""
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    finish_reason: str | None = None

    usage_obj: Any = None
    raw = getattr(result, "usage", None)
    if callable(raw):
        try:
            usage_obj = raw()
        except Exception:
            usage_obj = None
    elif raw is not None:
        usage_obj = raw

    if usage_obj is not None:
        input_tokens = int(getattr(usage_obj, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage_obj, "output_tokens", 0) or 0)
        cached_tokens = int(getattr(usage_obj, "cache_read_tokens", 0) or 0)

    fr = getattr(result, "finish_reason", None)
    if isinstance(fr, str):
        finish_reason = fr

    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        latency_ms=latency_ms,
        finish_reason=finish_reason,
    )


class Router:
    """Map a logical tier to a configured :class:`OpenAIModel` and run calls.

    Construct once per daemon. ``model_for(tier)`` returns the cached model
    object (so repeated calls reuse the same provider client). ``call(...)``
    is the wrapper to use whenever orchestrator code wants to run an agent —
    it precharges/charges the budget for cloud tiers, enforces the per-tier
    timeout, falls back on rate limits, and writes the ``llm_calls`` ledger
    + emits an ``llm_call`` observability event.
    """

    def __init__(
        self,
        config: dict[str, Any],
        budget: BudgetTracker,
        *,
        job: Job | None = None,
        db_path: Path | str | None = None,
        cache: LLMCache | None = None,
    ) -> None:
        if "tiers" not in config:
            raise ValueError("router config missing 'tiers' key")
        tiers = config["tiers"]
        if not isinstance(tiers, dict):
            raise ValueError("router config 'tiers' must be a mapping")
        missing = [t for t in EXPECTED_TIERS if t not in tiers]
        if missing:
            raise ValueError(f"router config missing required tiers: {missing}")

        self.tiers: dict[str, dict[str, Any]] = tiers
        self.budget = budget
        self.job = job
        self.db_path = Path(db_path) if db_path is not None else None
        self.cache = cache
        self._model_cache: dict[str, OpenAIModel] = {}

    # ---- Provider wiring ---------------------------------------------------

    def model_for(self, tier: Tier | str) -> OpenAIModel:
        """Return the cached :class:`OpenAIModel` for ``tier``.

        Builds it on first access, then memoizes per Router instance so
        callers can call this on every dispatch without re-creating the
        underlying ``AsyncOpenAI`` client.
        """
        if tier not in self.tiers:
            raise KeyError(f"unknown tier: {tier!r}")
        cached = self._model_cache.get(tier)
        if cached is not None:
            return cached
        model = _build_model_for_tier(tier, self.tiers[tier])
        self._model_cache[tier] = model
        return model

    def _make_fallback_agent(self, tier: str, fallback_model_name: str) -> Agent:
        """Construct a one-shot Agent bound to the cloud fallback model."""
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                f"OPENROUTER_API_KEY environment variable is required for fallback on tier {tier!r}"
            )
        model = OpenAIModel(
            fallback_model_name,
            provider=OpenAIProvider(base_url=OPENROUTER_BASE_URL, api_key=api_key),
        )
        return Agent(model)

    # ---- Call wrapper ------------------------------------------------------

    async def call(
        self,
        tier: Tier | str,
        agent: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run ``agent.run(*args, **kwargs)`` for ``tier`` with full bookkeeping.

        - precheck the budget for cloud tiers; ``charge`` is the single
          writer of the cloud ``llm_calls`` row + ``jobs.cost_so_far_usd``
        - for local tiers the router writes the ledger row directly with
          cost ``0.0`` (no budget enforcement)
        - enforce the per-tier ``timeout_s`` via :func:`asyncio.wait_for`
        - on :class:`openai.RateLimitError` for a cloud tier with a configured
          ``fallback_model``, retry once on the fallback model
        - emit exactly one ``llm_call`` event regardless of provider

        Local-tier failures are *not* silently rerouted to cloud (per §16);
        the original exception propagates to the caller.
        """
        cache_enabled = bool(kwargs.pop("cache", False))
        if tier not in self.tiers:
            raise KeyError(f"unknown tier: {tier!r}")
        spec = self.tiers[tier]
        provider = spec["provider"]
        model_name = spec["model"]
        timeout_s = spec.get("timeout_s")
        is_cloud = provider == "openrouter"
        fallback_used = False

        cache_key: str | None = None
        if cache_enabled and self.cache is not None:
            cache_key = make_key(
                provider,
                model_name,
                _extract_prompt(args),
                _extract_params(kwargs),
                _extract_tool_defs(agent),
            )
            hit = self.cache.get(cache_key)
            if hit is not None:
                self._emit_llm_call_event(
                    tier=tier,
                    provider=provider,
                    model=model_name,
                    usage=TokenUsage(),
                    cost_usd=0.0,
                    fallback_used=False,
                    cached=True,
                )
                return _CachedResult(hit)

        if is_cloud:
            self.budget.precheck(tier)

        t0 = time.perf_counter()
        try:
            result = await self._run_with_timeout(agent, timeout_s, args, kwargs)
        except RateLimitError:
            fallback_model = spec.get("fallback_model")
            if not (is_cloud and fallback_model):
                raise
            fallback_agent = self._make_fallback_agent(tier, fallback_model)
            result = await self._run_with_timeout(fallback_agent, timeout_s, args, kwargs)
            fallback_used = True
            model_name = fallback_model

        latency_ms = int((time.perf_counter() - t0) * 1000)
        usage = _extract_usage(result, latency_ms)

        if is_cloud:
            cost_usd = self.budget.charge(tier, provider, model_name, usage)
        else:
            cost_usd = 0.0
            self._record_local_call(
                tier=tier,
                provider=provider,
                model=model_name,
                usage=usage,
            )

        if cache_enabled and self.cache is not None and cache_key is not None:
            output = getattr(result, "output", None)
            if isinstance(output, str):
                self.cache.put(
                    cache_key,
                    output,
                    model_meta={"tier": tier, "provider": provider, "model": model_name},
                )

        self._emit_llm_call_event(
            tier=tier,
            provider=provider,
            model=model_name,
            usage=usage,
            cost_usd=cost_usd,
            fallback_used=fallback_used,
            cached=False,
        )

        return result

    @staticmethod
    async def _run_with_timeout(
        agent: Any,
        timeout_s: float | None,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if timeout_s is None:
            return await agent.run(*args, **kwargs)
        return await asyncio.wait_for(agent.run(*args, **kwargs), timeout=timeout_s)

    # ---- Ledger + event ----------------------------------------------------

    def _resolve_db_path(self) -> Path:
        if self.db_path is not None:
            return self.db_path
        if self.job is not None:
            return self.job.db_path
        return db.DEFAULT_DB_PATH

    def _record_local_call(
        self,
        *,
        tier: str,
        provider: str,
        model: str,
        usage: TokenUsage,
    ) -> None:
        """Write the ``llm_calls`` row for a local-tier call (cost = 0)."""
        ts = int(time.time())
        job_id = self.job.id if self.job is not None else None
        db_path = self._resolve_db_path()

        conn = db.connect(db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO llm_calls ("
                    " job_id, ts, tier, provider, model,"
                    " input_tokens, output_tokens, cached_tokens,"
                    " latency_ms, cost_usd, finish_reason"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        ts,
                        tier,
                        provider,
                        model,
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.cached_tokens,
                        usage.latency_ms,
                        0.0,
                        usage.finish_reason,
                    ),
                )
        finally:
            conn.close()

    def _emit_llm_call_event(
        self,
        *,
        tier: str,
        provider: str,
        model: str,
        usage: TokenUsage,
        cost_usd: float,
        fallback_used: bool,
        cached: bool = False,
    ) -> None:
        if self.job is None:
            return
        payload = {
            "tier": tier,
            "provider": provider,
            "model": model,
            "latency_ms": usage.latency_ms,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": cost_usd,
            "finish_reason": usage.finish_reason,
            "fallback_used": fallback_used,
            "cached": cached,
        }
        # Round-trip through json so the payload always contains JSON-safe
        # primitives — `emit` re-serializes in the SQL mirror.
        payload = json.loads(json.dumps(payload, default=str))
        emit(
            self.job,
            "INFO",
            "router",
            "llm_call",
            payload,
            db_path=self._resolve_db_path(),
        )


__all__ = [
    "EXPECTED_TIERS",
    "LMSTUDIO_DEFAULT_BASE_URL",
    "OPENROUTER_BASE_URL",
    "Router",
    "Tier",
    "load_models_config",
]
