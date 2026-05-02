"""Tests for the per-tier smoke probe (issue #12)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from research_agent.llm import smoke
from research_agent.llm.budgets import TokenUsage
from research_agent.llm.router import load_models_config
from research_agent.llm.smoke import Greeting, SmokeResult, run_llm_smoke

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_MODELS_YAML = REPO_ROOT / "config" / "models.yaml"


@pytest.fixture
def models_config() -> dict[str, Any]:
    return load_models_config(SHIPPED_MODELS_YAML)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAgentResult:
    def __init__(
        self,
        output: Greeting,
        input_tokens: int = 10,
        output_tokens: int = 2,
    ) -> None:
        self.output = output
        self._usage = TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
        self.finish_reason = "stop"

    def usage(self) -> TokenUsage:
        return self._usage


def _patch_agent_run(monkeypatch: pytest.MonkeyPatch, run_impl) -> None:
    """Replace ``pydantic_ai.Agent.run`` with a stub for the duration of a test."""
    from pydantic_ai import Agent

    monkeypatch.setattr(Agent, "run", run_impl, raising=True)


# ---------------------------------------------------------------------------
# Happy path: structured output + token + cost
# ---------------------------------------------------------------------------


def test_run_llm_smoke_returns_structured_output_with_cost_for_frontier(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    async def _stub_run(self, *args, **kwargs):  # noqa: ANN001 — mock signature
        return _FakeAgentResult(Greeting(greeting="hi"))

    _patch_agent_run(monkeypatch, _stub_run)

    result = asyncio.run(run_llm_smoke("frontier", "Say hello", models_config))

    assert isinstance(result, SmokeResult)
    assert result.ok is True
    assert result.output == "hi"
    assert result.input_tokens == 10
    assert result.output_tokens == 2
    # frontier pricing: $15/$75 per Mtok → 10*15/1e6 + 2*75/1e6
    expected = (10 * 15.00 + 2 * 75.00) / 1_000_000
    assert result.cost_usd == pytest.approx(expected)
    assert result.tier == "frontier"
    assert result.provider == "openrouter"
    assert result.model == "anthropic/claude-opus-4-7"


def test_run_llm_smoke_local_tier_charges_zero(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
) -> None:
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)

    async def _stub_run(self, *args, **kwargs):  # noqa: ANN001
        return _FakeAgentResult(Greeting(greeting="hello"))

    _patch_agent_run(monkeypatch, _stub_run)

    result = asyncio.run(run_llm_smoke("fast", "hi", models_config))
    assert result.ok is True
    assert result.cost_usd == 0.0
    assert result.output == "hello"
    assert result.provider == "lmstudio"


# ---------------------------------------------------------------------------
# Embeddings tier
# ---------------------------------------------------------------------------


def test_run_llm_smoke_embeddings_returns_dim(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
) -> None:
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)

    class _StubEmbeddingResp:
        def __init__(self, dim: int) -> None:
            self.data = [type("Datum", (), {"embedding": [0.0] * dim})()]

    class _StubEmbeddings:
        async def create(self, *, model: str, input: str) -> _StubEmbeddingResp:
            return _StubEmbeddingResp(1536)

    class _StubAsyncOpenAI:
        def __init__(self, *, base_url: str, api_key: str) -> None:
            self.embeddings = _StubEmbeddings()

    monkeypatch.setattr(smoke.openai, "AsyncOpenAI", _StubAsyncOpenAI)

    result = asyncio.run(run_llm_smoke("embeddings", "text", models_config))
    assert result.ok is True
    assert result.output == "dim=1536"
    assert result.skipped_reason is None


def test_run_llm_smoke_embeddings_failure_marks_not_ok(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
) -> None:
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)

    class _BoomEmbeddings:
        async def create(self, *, model: str, input: str):
            raise RuntimeError("connection refused")

    class _StubAsyncOpenAI:
        def __init__(self, *, base_url: str, api_key: str) -> None:
            self.embeddings = _BoomEmbeddings()

    monkeypatch.setattr(smoke.openai, "AsyncOpenAI", _StubAsyncOpenAI)

    result = asyncio.run(run_llm_smoke("embeddings", "text", models_config))
    assert result.ok is False
    assert "connection refused" in (result.error or "")


# ---------------------------------------------------------------------------
# Vision tier without image → skipped
# ---------------------------------------------------------------------------


def test_run_llm_smoke_vision_without_image_skipped(
    models_config: dict[str, Any],
) -> None:
    result = asyncio.run(run_llm_smoke("vision", "describe", models_config))
    assert result.ok is True
    assert result.skipped_reason == "vision: no image provided"


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_run_llm_smoke_run_exception_returns_not_ok(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
) -> None:
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)

    async def _boom(self, *args, **kwargs):  # noqa: ANN001
        raise RuntimeError("LM Studio not running")

    _patch_agent_run(monkeypatch, _boom)

    result = asyncio.run(run_llm_smoke("fast", "hi", models_config))
    assert result.ok is False
    assert "LM Studio not running" in (result.error or "")


def test_run_llm_smoke_unknown_tier_errors(models_config: dict[str, Any]) -> None:
    result = asyncio.run(run_llm_smoke("nope", "hi", models_config))
    assert result.ok is False
    assert "unknown tier" in (result.error or "")


def test_run_llm_smoke_missing_openrouter_key_marks_not_ok(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    result = asyncio.run(run_llm_smoke("frontier", "hi", models_config))
    assert result.ok is False
    assert "OPENROUTER_API_KEY" in (result.error or "")


def test_run_llm_smoke_invalid_structured_output_marks_not_ok(
    monkeypatch: pytest.MonkeyPatch,
    models_config: dict[str, Any],
) -> None:
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)

    class _BadResult:
        def __init__(self) -> None:
            self.output = "raw string, not a Greeting"
            self.finish_reason = "stop"

        def usage(self) -> TokenUsage:
            return TokenUsage()

    async def _stub_run(self, *args, **kwargs):  # noqa: ANN001
        return _BadResult()

    _patch_agent_run(monkeypatch, _stub_run)

    result = asyncio.run(run_llm_smoke("fast", "hi", models_config))
    assert result.ok is False
    assert "structured output" in (result.error or "")
