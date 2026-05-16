"""Orchestrator — job lifecycle, planning, research loop, synthesis, critique, checkpointing."""

from research_agent.orchestrator.checkpoint import (
    CHECKPOINT_KINDS,
    RestoreState,
    checkpoint,
    restore,
)
from research_agent.orchestrator.critique import (
    CritiqueOutput,
    Gap,
    critique,
)
from research_agent.orchestrator.errors import FatalError, RetriableError
from research_agent.orchestrator.fragments import (
    FRAGMENT_REGISTRY,
    FragmentSpec,
    all_fragments,
    dependency_closure,
    fragment_ids,
    get_fragment,
    synthesis_order,
)
from research_agent.orchestrator.loop import (
    HEURISTIC_CHECK_EVERY_N,
    MAX_TASKS_PER_JOB,
    RETRY_MAX_ATTEMPTS,
    RETRY_WAITS,
    Handler,
    default_handlers,
    run_loop,
)
from research_agent.orchestrator.plan import (
    MAX_PLAN_VERSIONS,
    Plan,
    PlanVersionCapExceeded,
    Subgoal,
    TaskKind,
    TaskSpec,
    cloud_replan,
    initial_plan,
    tactical_replan,
)
from research_agent.orchestrator.synth import (
    FINAL_TOP_N,
    TOP_N_FINDINGS,
    SynthesisOutput,
    final_synthesis,
    fragment_synth_enabled,
    synthesize,
    synthesize_fragments,
)

__all__ = [
    "CHECKPOINT_KINDS",
    "FINAL_TOP_N",
    "FRAGMENT_REGISTRY",
    "HEURISTIC_CHECK_EVERY_N",
    "CritiqueOutput",
    "Gap",
    "Handler",
    "FragmentSpec",
    "MAX_PLAN_VERSIONS",
    "MAX_TASKS_PER_JOB",
    "Plan",
    "PlanVersionCapExceeded",
    "RETRY_MAX_ATTEMPTS",
    "RETRY_WAITS",
    "RestoreState",
    "Subgoal",
    "SynthesisOutput",
    "TOP_N_FINDINGS",
    "TaskKind",
    "TaskSpec",
    "FatalError",
    "RetriableError",
    "all_fragments",
    "checkpoint",
    "cloud_replan",
    "critique",
    "default_handlers",
    "dependency_closure",
    "final_synthesis",
    "fragment_ids",
    "fragment_synth_enabled",
    "get_fragment",
    "initial_plan",
    "restore",
    "run_loop",
    "synthesis_order",
    "synthesize",
    "synthesize_fragments",
    "tactical_replan",
]
