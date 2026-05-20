"""Tests for the interactive intake flow (`research start`'s 11-step flow)."""

from __future__ import annotations

from typing import Any

import pytest

from research_agent import intake


class _Scripted:
    """Helper: build a fake questionary Question whose ``.ask()`` pops scripted values."""

    def __init__(self, values: list[Any]) -> None:
        self._values = values
        self._calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def factory(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        self._calls.append((args, kwargs))
        if not self._values:
            raise AssertionError(
                f"no scripted answer left for call args={args!r} kwargs={kwargs!r}"
            )
        value = self._values.pop(0)

        class _Q:
            def ask(self_inner) -> Any:  # noqa: N805
                return value

        return _Q()

    @property
    def calls(self) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return self._calls


def _patch_questionary(
    mocker,
    *,
    text: list[Any],
    select: list[Any],
    confirm: list[Any],
) -> tuple[_Scripted, _Scripted, _Scripted]:
    """Patch questionary's text/select/confirm with scripted scripts."""
    text_s = _Scripted(text)
    select_s = _Scripted(select)
    confirm_s = _Scripted(confirm)
    mocker.patch("research_agent.intake.questionary.text", side_effect=text_s.factory)
    mocker.patch("research_agent.intake.questionary.select", side_effect=select_s.factory)
    mocker.patch("research_agent.intake.questionary.confirm", side_effect=confirm_s.factory)
    return text_s, select_s, confirm_s


def test_run_intake_happy_path(mocker):
    mocker.patch("research_agent.intake._collect_followups", return_value=[])
    _patch_questionary(
        mocker,
        text=[
            "ABC Corp",  # step 1 goal
            "a sourced overview of governance issues",  # step 2 goal_one_sentence
            "",  # step 8 corpus (skip)
        ],
        select=[
            "Corporate / financial",  # step 3 domain
            "12h",  # step 4 time cap
            "$25",  # step 5 budget
            "internal brief",  # step 6 output
            "balanced",  # step 7 aggressiveness
        ],
        confirm=[True],  # step 10 confirm
    )

    result = intake.run_intake()

    assert set(result.keys()) == {
        "goal",
        "goal_one_sentence",
        "domain",
        "time_cap",
        "budget_usd",
        "output_orientation",
        "aggressiveness",
        "corpus_path",
        "corpus_dossier",
        "fragments",
        "followup_qa",
    }
    assert result["goal"] == "ABC Corp"
    assert result["goal_one_sentence"] == "a sourced overview of governance issues"
    assert result["domain"] == "Corporate / financial"
    assert result["time_cap"] == 12
    assert result["budget_usd"] == 25.0
    assert result["output_orientation"] == "internal brief"
    assert result["aggressiveness"] == "balanced"
    assert result["corpus_path"] is None
    # No corpus path → dossier prompt is hidden, defaults to False.
    assert result["corpus_dossier"] is False
    assert result["fragments"] is False
    assert result["followup_qa"] == []


def test_run_intake_revise_loops_back_to_step_1(mocker):
    mocker.patch("research_agent.intake._collect_followups", return_value=[])
    text_s, _, _ = _patch_questionary(
        mocker,
        text=[
            "First goal",  # round 1 step 1
            "first answer",  # round 1 step 2
            "",  # round 1 step 8
            "Second goal",  # round 2 step 1
            "second answer",  # round 2 step 2
            "",  # round 2 step 8
        ],
        select=[
            "Political / corruption",
            "4h",
            "$5",
            "internal brief",
            "balanced",
            "Political / corruption",
            "4h",
            "$5",
            "internal brief",
            "balanced",
        ],
        confirm=[False, True],  # revise once, then accept
    )

    result = intake.run_intake()

    # Step 1 is the very first text prompt each round; we should see two rounds.
    step_1_prompts = [c for c in text_s.calls if c[0] and "Who or what" in c[0][0]]
    assert len(step_1_prompts) == 2
    assert result["goal"] == "Second goal"


def test_run_intake_followups_max_three(mocker):
    five_qa = [{"question": f"q{i}", "answer": f"a{i}"} for i in range(5)]
    mocker.patch("research_agent.intake._collect_followups", return_value=five_qa)
    _patch_questionary(
        mocker,
        text=["goal", "answer", ""],
        select=[
            "Political / corruption",
            "4h",
            "$5",
            "internal brief",
            "balanced",
        ],
        confirm=[True],
    )

    result = intake.run_intake()
    assert len(result["followup_qa"]) == 3
    assert [qa["question"] for qa in result["followup_qa"]] == ["q0", "q1", "q2"]


def test_run_intake_followups_swallow_llm_error(mocker):
    mocker.patch(
        "research_agent.intake._generate_followup_questions",
        side_effect=RuntimeError("LM Studio offline"),
    )
    _patch_questionary(
        mocker,
        text=["goal", "answer", ""],
        select=[
            "Political / corruption",
            "4h",
            "$5",
            "internal brief",
            "balanced",
        ],
        confirm=[True],
    )

    result = intake.run_intake()
    assert result["followup_qa"] == []


def test_run_intake_prefills_from_cli_flags(mocker):
    mocker.patch("research_agent.intake._collect_followups", return_value=[])
    text_s, select_s, _ = _patch_questionary(
        mocker,
        text=["goal", "answer", "./x"],
        select=[
            "Political / corruption",
            "12h",
            "$25",
            "internal brief",
            "balanced",
        ],
        # Corpus path "./x" is non-empty so the dossier prompt fires
        # (step 8b). First confirm = dossier, second = final summary.
        confirm=[False, True],
    )

    result = intake.run_intake(corpus="./x", budget_usd=25.0, time_cap=12)

    # Time cap select default came from time_cap=12 → "12h".
    time_cap_call = next(c for c in select_s.calls if c[0] and "Time cap" in c[0][0])
    assert time_cap_call[1].get("default") == "12h"

    # Budget select default came from budget_usd=25.0 → "$25".
    budget_call = next(c for c in select_s.calls if c[0] and "Budget" in c[0][0])
    assert budget_call[1].get("default") == "$25"

    # Corpus text default came from corpus="./x".
    corpus_call = next(c for c in text_s.calls if c[0] and "local files" in c[0][0])
    assert corpus_call[1].get("default") == "./x"

    assert result["corpus_path"] == "./x"
    assert result["corpus_dossier"] is False
    assert result["time_cap"] == 12
    assert result["budget_usd"] == 25.0


def test_run_intake_dossier_prompt_only_when_corpus_supplied(mocker):
    """With no corpus path the dossier confirm() prompt is hidden."""
    mocker.patch("research_agent.intake._collect_followups", return_value=[])
    _, _, confirm_s = _patch_questionary(
        mocker,
        text=["goal", "answer", ""],  # step 8 corpus blank → no dossier prompt
        select=[
            "Political / corruption",
            "4h",
            "$5",
            "internal brief",
            "balanced",
        ],
        confirm=[True],
    )

    result = intake.run_intake()

    # Exactly one confirm — the final summary; no dossier prompt fired.
    assert len(confirm_s.calls) == 1
    assert result["corpus_dossier"] is False


def test_run_intake_dossier_prompt_records_yes(mocker):
    """When corpus is supplied and operator says yes, dossier flag is True."""
    mocker.patch("research_agent.intake._collect_followups", return_value=[])
    _, _, confirm_s = _patch_questionary(
        mocker,
        text=["goal", "answer", "./corpus"],
        select=[
            "Political / corruption",
            "4h",
            "$5",
            "internal brief",
            "balanced",
        ],
        confirm=[True, True],  # dossier=yes, summary=yes
    )

    result = intake.run_intake(corpus="./corpus")

    # The dossier confirm was offered (and accepted), then the summary confirm.
    assert len(confirm_s.calls) == 2
    assert result["corpus_dossier"] is True


def test_run_intake_dossier_prompt_default_follows_cli_flag(mocker):
    """CLI --corpus-dossier pre-sets the dossier confirm() default to True."""
    mocker.patch("research_agent.intake._collect_followups", return_value=[])
    _, _, confirm_s = _patch_questionary(
        mocker,
        text=["goal", "answer", "./corpus"],
        select=[
            "Political / corruption",
            "4h",
            "$5",
            "internal brief",
            "balanced",
        ],
        confirm=[True, True],
    )

    intake.run_intake(corpus="./corpus", corpus_dossier=True)

    dossier_call = next(
        c for c in confirm_s.calls if c[0] and "dossier" in c[0][0].lower()
    )
    assert dossier_call[1].get("default") is True


def test_run_intake_other_domain_prompts_freetext(mocker):
    mocker.patch("research_agent.intake._collect_followups", return_value=[])
    text_s, _, _ = _patch_questionary(
        mocker,
        text=[
            "goal",  # step 1
            "answer",  # step 2
            "ornithology",  # step 3 free-text domain
            "",  # step 8 corpus skip
        ],
        select=[
            "Other",  # step 3
            "4h",
            "$5",
            "internal brief",
            "balanced",
        ],
        confirm=[True],
    )

    result = intake.run_intake()
    assert result["domain"] == "ornithology"
    # The free-text domain prompt should have been triggered.
    assert any("domain" in (c[0][0].lower() if c[0] else "") for c in text_s.calls)


def test_module_constants_match_spec():
    """Lock the §5.1 choice tuples so future edits are intentional."""
    assert intake.DOMAIN_CHOICES == (
        "Political / corruption",
        "Corporate / financial",
        "Legal / regulatory",
        "Technical / scientific",
        "Media / public figure",
        "Other",
    )
    assert intake.TIME_CAP_CHOICES == ("4h", "12h", "24h", "48h", "1 week", "open-ended")
    assert intake.BUDGET_CHOICES == ("$5", "$25", "$100", "$500", "no cap")
    assert intake.OUTPUT_CHOICES == (
        "Substack-ready long-form",
        "internal brief",
        "raw findings dump",
        "research dossier",
    )
    assert intake.AGGRESSIVENESS_CHOICES == ("conservative", "balanced", "aggressive")
    assert intake.MAX_FOLLOWUPS == 3


def test_run_intake_step_1_rejects_empty_goal(mocker):
    """Step 1 must re-prompt until the user enters a non-empty goal."""
    mocker.patch("research_agent.intake._collect_followups", return_value=[])
    _patch_questionary(
        mocker,
        text=[
            "",  # first attempt — empty
            "   ",  # second attempt — whitespace
            "Real goal",  # third — accepted
            "answer",  # step 2
            "",  # step 8
        ],
        select=[
            "Political / corruption",
            "4h",
            "$5",
            "internal brief",
            "balanced",
        ],
        confirm=[True],
    )

    result = intake.run_intake()
    assert result["goal"] == "Real goal"


@pytest.mark.parametrize(
    "label,hours",
    [("4h", 4), ("12h", 12), ("24h", 24), ("48h", 48), ("1 week", 168), ("open-ended", None)],
)
def test_time_cap_label_to_hours(label, hours):
    assert intake._TIME_CAP_HOURS[label] == hours


@pytest.mark.parametrize(
    "label,usd",
    [("$5", 5.0), ("$25", 25.0), ("$100", 100.0), ("$500", 500.0), ("no cap", None)],
)
def test_budget_label_to_usd(label, usd):
    assert intake._BUDGET_USD[label] == usd
