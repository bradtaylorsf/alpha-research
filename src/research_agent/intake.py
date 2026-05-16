"""Interactive Q&A intake — captures research goal and constraints.

Implements the 11-step Questionary flow from §5.1 of the implementation
guide. Steps 1–8 collect the structured constraints (goal, domain, caps,
output orientation, aggressiveness, optional corpus). Step 9 hits the
local ``general`` tier for up to three adaptive clarifying questions
(via ``intake_followup.md``). Step 10 prints a Rich summary; on revise
the loop drops back to step 1 with no preserved state.

The LLM follow-up call is best-effort: any error surfaces as a structlog
warning and ``followup_qa=[]`` rather than blocking the user behind a
cloud failure (intake must not block on cloud failure).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import questionary
import structlog
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from rich.console import Console
from rich.panel import Panel

from research_agent.llm.router import _build_model_for_tier, load_models_config
from research_agent.prompts.loader import load_prompt

_log = structlog.get_logger(__name__)

DOMAIN_CHOICES: tuple[str, ...] = (
    "Political / corruption",
    "Corporate / financial",
    "Legal / regulatory",
    "Technical / scientific",
    "Media / public figure",
    "Other",
)

TIME_CAP_CHOICES: tuple[str, ...] = ("4h", "12h", "24h", "48h", "1 week", "open-ended")
_TIME_CAP_HOURS: dict[str, int | None] = {
    "4h": 4,
    "12h": 12,
    "24h": 24,
    "48h": 48,
    "1 week": 24 * 7,
    "open-ended": None,
}

BUDGET_CHOICES: tuple[str, ...] = ("$5", "$25", "$100", "$500", "no cap")
_BUDGET_USD: dict[str, float | None] = {
    "$5": 5.0,
    "$25": 25.0,
    "$100": 100.0,
    "$500": 500.0,
    "no cap": None,
}

OUTPUT_CHOICES: tuple[str, ...] = (
    "Substack-ready long-form",
    "internal brief",
    "raw findings dump",
    "research dossier",
)

AGGRESSIVENESS_CHOICES: tuple[str, ...] = ("conservative", "balanced", "aggressive")

MAX_FOLLOWUPS = 3


class FollowupQuestion(BaseModel):
    """A single adaptive clarifying question emitted by the intake agent."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    why_it_matters: str = ""
    suggested_defaults: list[str] = Field(default_factory=list)


def _ask_text(prompt: str, *, default: str = "") -> str:
    """Ask a non-empty text question; ``default`` pre-fills the input."""
    return questionary.text(prompt, default=default).ask() or ""


def _ask_select(prompt: str, choices: tuple[str, ...], *, default: str) -> str:
    return questionary.select(prompt, choices=list(choices), default=default).ask()


def _budget_default(budget_usd: float | None) -> str:
    if budget_usd is None:
        return BUDGET_CHOICES[0]
    for label, val in _BUDGET_USD.items():
        if val is not None and float(val) == float(budget_usd):
            return label
    return BUDGET_CHOICES[0]


def _time_cap_default(time_cap: int | None) -> str:
    if time_cap is None:
        return TIME_CAP_CHOICES[0]
    for label, val in _TIME_CAP_HOURS.items():
        if val == time_cap:
            return label
    return TIME_CAP_CHOICES[0]


def _generate_followup_questions(answers_so_far: dict[str, Any]) -> list[FollowupQuestion]:
    """Run the local ``general`` tier to produce 0–N follow-up questions.

    Separated from :func:`_collect_followups` so tests can stub the LLM
    call independently of the user-facing prompt loop.
    """
    cfg = load_models_config(Path("config/models.yaml"))
    tiers = cfg["tiers"]
    if "general" not in tiers:
        raise KeyError("'general' tier missing from config/models.yaml")
    model = _build_model_for_tier("general", tiers["general"])

    question_text = (
        f"goal: {answers_so_far.get('goal', '')}\n"
        f"successful answer: {answers_so_far.get('goal_one_sentence', '')}\n"
        f"domain: {answers_so_far.get('domain', '')}"
    )
    rendered = load_prompt("intake_followup", question=question_text)
    agent = Agent(model, output_type=list[FollowupQuestion], system_prompt=rendered)
    result = asyncio.run(agent.run(question_text))
    output = result.output
    if not isinstance(output, list):
        return []
    return list(output)


def _collect_followups(answers_so_far: dict[str, Any]) -> list[dict[str, str]]:
    """Generate adaptive follow-ups via the local LLM and prompt the user.

    Returns a list of ``{"question", "answer"}`` dicts capped at
    :data:`MAX_FOLLOWUPS`. Any LLM error is logged at WARN and yields ``[]``
    so the intake loop never blocks on cloud failure.
    """
    try:
        questions = _generate_followup_questions(answers_so_far)
    except Exception as e:  # noqa: BLE001 — best-effort; never block intake
        _log.warning("intake_followup_failed", error=str(e))
        return []

    qa: list[dict[str, str]] = []
    for q in questions[:MAX_FOLLOWUPS]:
        if q.suggested_defaults:
            answer = questionary.select(
                q.question,
                choices=list(q.suggested_defaults),
            ).ask()
        else:
            answer = questionary.text(q.question).ask()
        qa.append({"question": q.question, "answer": answer or ""})
    return qa


def _render_summary(intake: dict[str, Any]) -> Panel:
    """Render the confirmation summary before spawning the daemon."""
    time_cap = intake.get("time_cap")
    budget = intake.get("budget_usd")
    time_str = f"{time_cap}h" if isinstance(time_cap, int) else "open-ended"
    budget_str = f"${budget:.0f}" if isinstance(budget, (int, float)) else "no cap"
    lines = [
        f"Goal: {intake['goal']}",
        f"Domain: {intake['domain']}",
        f"Caps: time={time_str}, budget={budget_str}",
        f"Synthesis: {'fragments' if intake.get('fragments') else 'legacy'}",
        f"Output: {intake['output_orientation']} ({intake['aggressiveness']})",
        f"Follow-ups answered: {len(intake.get('followup_qa', []))}",
    ]
    return Panel("\n".join(lines), title="Research plan", border_style="cyan")


def run_intake(
    corpus: str | None = None,
    budget_usd: float | None = None,
    time_cap: int | None = None,
    fragments: bool = False,
) -> dict[str, Any]:
    """Run the interactive intake flow and return a populated intake dict.

    Loops the 1–10 step flow until the user confirms; on revise we re-prompt
    every field from scratch (per §5.1, no preserved state). The CLI pre-fills
    ``time_cap`` / ``budget_usd`` / ``corpus`` defaults; the user can still
    override any of them inside the flow.

    Returns a dict with stable intake keys: ``goal``, ``goal_one_sentence``,
    ``domain``, ``time_cap``, ``budget_usd``, ``output_orientation``,
    ``aggressiveness``, ``corpus_path``, ``fragments``, ``followup_qa``.
    """
    console = Console()

    while True:
        # Step 1 — goal (required, non-empty).
        goal = ""
        while not goal.strip():
            goal = _ask_text("Who or what do you want to research?")

        # Step 2 — clarification.
        goal_one_sentence = _ask_text(
            "In one sentence, what would a successful answer look like?",
        )

        # Step 3 — domain.
        domain_choice = _ask_select(
            "Domain?",
            DOMAIN_CHOICES,
            default=DOMAIN_CHOICES[0],
        )
        if domain_choice == "Other":
            domain = _ask_text("Describe the domain:") or "other"
        else:
            domain = domain_choice

        # Step 4 — time cap.
        time_cap_label = _ask_select(
            "Time cap?",
            TIME_CAP_CHOICES,
            default=_time_cap_default(time_cap),
        )
        time_cap_hours = _TIME_CAP_HOURS[time_cap_label]

        # Step 5 — budget cap.
        budget_label = _ask_select(
            "Budget cap (cloud only)?",
            BUDGET_CHOICES,
            default=_budget_default(budget_usd),
        )
        budget_value = _BUDGET_USD[budget_label]

        # Step 6 — output orientation.
        output_orientation = _ask_select(
            "Output orientation?",
            OUTPUT_CHOICES,
            default=OUTPUT_CHOICES[0],
        )

        # Step 7 — aggressiveness.
        aggressiveness = _ask_select(
            "Aggressiveness?",
            AGGRESSIVENESS_CHOICES,
            default=AGGRESSIVENESS_CHOICES[1],
        )

        # Step 8 — optional corpus path.
        corpus_answer = _ask_text(
            "Any local files I should index first? (default: skip)",
            default=corpus or "",
        )
        corpus_path = corpus_answer.strip() or None

        partial = {
            "goal": goal.strip(),
            "goal_one_sentence": goal_one_sentence.strip(),
            "domain": domain,
        }

        # Step 9 — adaptive follow-ups (best-effort, capped at 3).
        followup_qa = _collect_followups(partial)[:MAX_FOLLOWUPS]

        intake: dict[str, Any] = {
            "goal": partial["goal"],
            "goal_one_sentence": partial["goal_one_sentence"],
            "domain": domain,
            "time_cap": time_cap_hours,
            "budget_usd": budget_value,
            "output_orientation": output_orientation,
            "aggressiveness": aggressiveness,
            "corpus_path": corpus_path,
            "fragments": bool(fragments),
            "followup_qa": followup_qa,
        }

        # Step 10 — confirm. Revise drops back to step 1.
        console.print(_render_summary(intake))
        if questionary.confirm("Proceed with this plan?").ask():
            return intake


__all__ = [
    "AGGRESSIVENESS_CHOICES",
    "BUDGET_CHOICES",
    "DOMAIN_CHOICES",
    "FollowupQuestion",
    "MAX_FOLLOWUPS",
    "OUTPUT_CHOICES",
    "TIME_CAP_CHOICES",
    "run_intake",
]
