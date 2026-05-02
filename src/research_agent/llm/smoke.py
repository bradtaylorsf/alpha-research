"""Per-tier smoke probe for the LLM stack (issue #12).

CI and operators need a way to verify that every tier in ``models.yaml``
actually responds — including structured-output handling — without spinning
up a full job. ``run_llm_smoke`` sends a one-shot prompt through the same
provider wiring the :mod:`router` uses (so any LM Studio/OpenRouter config
issues surface here too) and returns a :class:`SmokeResult` the CLI can
print and CI can grep.

The probe is *jobless*: it does not write the SQLite ledger or emit
``llm_call`` events. Cost is computed in-memory from the same pricing block
:class:`research_agent.llm.budgets.BudgetTracker` reads.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openai
from pydantic import BaseModel
from pydantic_ai import Agent

from .router import (
    LMSTUDIO_DEFAULT_BASE_URL,
    OPENROUTER_BASE_URL,
    _build_model_for_tier,
    _extract_usage,
)


class Greeting(BaseModel):
    """Trivial structured-output schema used to prove the tier honors it."""

    greeting: str


@dataclass(slots=True)
class SmokeResult:
    """Result of a single smoke probe against one tier.

    ``ok=True`` and ``skipped_reason`` set together means the tier was
    intentionally skipped (e.g. vision without an image). ``ok=False``
    means the call ran but failed validation, the connection, or the
    timeout — ``error`` carries the message.
    """

    tier: str
    provider: str
    model: str
    ok: bool
    output: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    skipped_reason: str | None = None
    error: str | None = None


def _compute_cost(
    pricing_block: dict[str, Any] | None,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Mirror :meth:`BudgetTracker._compute_cost` without the DB writes.

    Smoke runs are jobless, so we just multiply tokens by the configured
    list prices — bad/missing pricing yields ``0.0`` to match production
    behavior on a stale price book.
    """
    if not pricing_block:
        return 0.0
    try:
        input_rate = float(pricing_block.get("input_usd_per_mtok") or 0.0)
        output_rate = float(pricing_block.get("output_usd_per_mtok") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if input_rate <= 0.0 and output_rate <= 0.0:
        return 0.0
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


async def _run_embeddings_smoke(
    tier: str,
    spec: dict[str, Any],
    prompt: str,
) -> SmokeResult:
    """Hit the configured embeddings endpoint once and report the dim."""
    provider_name = spec["provider"]
    model_name = spec["model"]
    if provider_name == "lmstudio":
        base_url = os.environ.get("LMSTUDIO_BASE_URL") or LMSTUDIO_DEFAULT_BASE_URL
        api_key = "lm-studio"
    elif provider_name == "openrouter":
        api_key_env = os.environ.get("OPENROUTER_API_KEY")
        if not api_key_env:
            return SmokeResult(
                tier=tier,
                provider=provider_name,
                model=model_name,
                ok=False,
                error="OPENROUTER_API_KEY environment variable is required",
            )
        base_url = OPENROUTER_BASE_URL
        api_key = api_key_env
    else:
        return SmokeResult(
            tier=tier,
            provider=provider_name,
            model=model_name,
            ok=False,
            error=f"unknown provider {provider_name!r}",
        )

    client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key)
    try:
        resp = await client.embeddings.create(model=model_name, input=prompt)
    except Exception as e:  # noqa: BLE001 — surface every failure to the operator
        return SmokeResult(
            tier=tier,
            provider=provider_name,
            model=model_name,
            ok=False,
            error=str(e),
        )

    vec = resp.data[0].embedding
    return SmokeResult(
        tier=tier,
        provider=provider_name,
        model=model_name,
        ok=True,
        output=f"dim={len(vec)}",
    )


async def run_llm_smoke(
    tier: str,
    prompt: str,
    models_config: dict[str, Any],
    *,
    image_path: Path | None = None,
) -> SmokeResult:
    """Run a single end-to-end probe against ``tier``.

    Returns a :class:`SmokeResult`. On success ``ok=True`` and the output
    plus token counts and (for cloud tiers) computed cost are populated.
    On the vision tier without an image the probe is skipped with
    ``ok=True`` and ``skipped_reason`` set so CI can mark it green
    without making a call.
    """
    tiers = models_config.get("tiers") or {}
    spec = tiers.get(tier)
    if spec is None:
        return SmokeResult(
            tier=tier,
            provider="?",
            model="?",
            ok=False,
            error=f"unknown tier: {tier!r} (known: {sorted(tiers)})",
        )

    provider_name = spec["provider"]
    model_name = spec["model"]

    if tier == "embeddings":
        return await _run_embeddings_smoke(tier, spec, prompt)

    if tier == "vision" and image_path is None:
        return SmokeResult(
            tier=tier,
            provider=provider_name,
            model=model_name,
            ok=True,
            skipped_reason="vision: no image provided",
        )

    try:
        model = _build_model_for_tier(tier, spec)
    except Exception as e:  # noqa: BLE001 — config errors should surface here too
        return SmokeResult(
            tier=tier,
            provider=provider_name,
            model=model_name,
            ok=False,
            error=str(e),
        )

    agent = Agent(model, output_type=Greeting)
    timeout_s = spec.get("timeout_s")

    t0 = time.perf_counter()
    try:
        if timeout_s is None:
            result = await agent.run(prompt)
        else:
            import asyncio

            result = await asyncio.wait_for(agent.run(prompt), timeout=timeout_s)
    except Exception as e:  # noqa: BLE001 — anything from the SDK becomes ok=False
        return SmokeResult(
            tier=tier,
            provider=provider_name,
            model=model_name,
            ok=False,
            error=str(e),
        )

    if not isinstance(result.output, Greeting):
        return SmokeResult(
            tier=tier,
            provider=provider_name,
            model=model_name,
            ok=False,
            error="structured output failed validation",
        )

    latency_ms = int((time.perf_counter() - t0) * 1000)
    usage = _extract_usage(result, latency_ms)

    pricing = (models_config.get("pricing") or {}).get(tier)
    cost = (
        _compute_cost(pricing, usage.input_tokens, usage.output_tokens)
        if provider_name == "openrouter"
        else 0.0
    )

    return SmokeResult(
        tier=tier,
        provider=provider_name,
        model=model_name,
        ok=True,
        output=result.output.greeting,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
    )


__all__ = ["Greeting", "SmokeResult", "run_llm_smoke"]
