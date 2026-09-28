"""Comparing one suite across models: the ranking rules, and the real result shape.

The ranking tests use hand-built results so each rule is pinned on its own. The
engine tests run real arms through :func:`execute_evaluation_with_seam`, so the
fields ``compare`` reads are the ones the engine actually produces.
"""

from __future__ import annotations

from typing import Any

import pytest

from nimbus.config import Settings
from nimbus.engine.fake_model import FakeModel, Text
from nimbus.evals import engine as evals_engine
from nimbus.evals.compare import Arm, compare
from nimbus.evals.judge import FakeJudgeModel
from nimbus.evals.schemas import EvaluationRequest
from nimbus.store import db
from tests.test_evals_seam import Recorder, RecordingStore

# --------------------------------------------------------------------------- #
# Ranking, on hand-built results
# --------------------------------------------------------------------------- #


def case(case_id: str, status: str, score: float | None = None) -> dict[str, Any]:
    return {"id": case_id, "status": status, "score": score, "passed": status == "passed"}


def result(
    cases: list[dict[str, Any]], *, score: int | None = 80, cost: float | None = None
) -> dict[str, Any]:
    passed = sum(1 for entry in cases if entry["passed"])
    return {
        "score": score,
        "grade": "B",
        "metrics": {
            "pass_rate": passed / len(cases),
            "cases_passed": passed,
            "cases_total": len(cases),
            "cases_errored": 0,
        },
        "suite": {"repeats": 1},
        "cost": {"total_usd": cost},
        "cases": cases,
    }


def arm(model_id: str, outcome: dict[str, Any] | None, status: str = "completed") -> Arm:
    return Arm("bedrock", model_id, f"eval-{model_id}", status, outcome)


ALL_PASS = [case("a", "passed", 1.0), case("b", "passed", 1.0)]
HALF = [case("a", "passed", 1.0), case("b", "failed", 0.2)]
NONE = [case("a", "failed", 0.1), case("b", "failed", 0.1)]


def test_the_arm_with_the_higher_pass_rate_wins():
    comparison = compare([arm("weak", result(HALF)), arm("strong", result(ALL_PASS))])

    assert comparison["ranking"] == ["bedrock:strong", "bedrock:weak"]
    assert comparison["winner"] == "bedrock:strong"
    assert comparison["tied"] == []


def test_equal_pass_rates_are_split_by_the_mean_judge_score():
    comparison = compare(
        [arm("low", result(ALL_PASS, score=70)), arm("high", result(ALL_PASS, score=90))]
    )

    assert comparison["winner"] == "bedrock:high"


def test_cost_orders_equally_good_arms_but_never_names_a_winner():
    comparison = compare(
        [
            arm("dear", result(ALL_PASS, score=80, cost=2.0)),
            arm("cheap", result(ALL_PASS, score=80, cost=0.5)),
        ]
    )

    assert comparison["ranking"] == ["bedrock:cheap", "bedrock:dear"]  # cheaper first
    assert comparison["winner"] is None  # ...but the quality is level: a tie
    assert sorted(comparison["tied"]) == ["bedrock:cheap", "bedrock:dear"]


def test_a_tie_only_lists_the_arms_level_at_the_top():
    comparison = compare(
        [
            arm("x", result(ALL_PASS, score=80)),
            arm("y", result(ALL_PASS, score=80)),
            arm("z", result(NONE, score=10)),
        ]
    )

    assert comparison["winner"] is None
    assert sorted(comparison["tied"]) == ["bedrock:x", "bedrock:y"]
    assert comparison["ranking"][-1] == "bedrock:z"


def test_an_unpriced_arm_ranks_after_a_priced_one_of_equal_quality():
    comparison = compare(
        [arm("unknown", result(ALL_PASS, cost=None)), arm("known", result(ALL_PASS, cost=9.0))]
    )

    assert comparison["ranking"] == ["bedrock:known", "bedrock:unknown"]


def test_an_arm_with_no_result_is_reported_but_not_ranked():
    comparison = compare([arm("ok", result(ALL_PASS)), arm("broken", None, status="error")])

    assert comparison["ranking"] == ["bedrock:ok"]
    assert comparison["winner"] == "bedrock:ok"
    broken = next(summary for summary in comparison["arms"] if summary["model_id"] == "broken")
    assert broken["status"] == "error"
    assert broken["pass_rate"] is None
    assert {row["agreement"] for row in comparison["cases"]} == {"incomplete"}


@pytest.mark.parametrize("status", ["error", "cancelled"])
def test_an_arm_that_did_not_complete_is_never_ranked_even_with_partial_numbers(status):
    """A cancelled arm that happened to pass its first case must not beat a full run."""
    partial = result([case("a", "passed", 1.0), case("b", "error")])
    partial["metrics"]["pass_rate"] = 1.0  # the tempting number: it only saw one case

    comparison = compare([arm("full", result(HALF)), arm("cut-short", partial, status=status)])

    assert comparison["ranking"] == ["bedrock:full"]
    assert comparison["winner"] == "bedrock:full"
    listed = next(s for s in comparison["arms"] if s["model_id"] == "cut-short")
    assert listed["status"] == status


def test_nothing_is_comparable_when_no_arm_has_a_result():
    comparison = compare([arm("a", None, "error"), arm("b", None, "cancelled")])

    assert comparison["ranking"] == []
    assert comparison["winner"] is None
    assert comparison["tied"] == []


def test_cases_the_arms_disagree_on_are_called_out():
    comparison = compare([arm("a", result(ALL_PASS)), arm("b", result(HALF))])

    verdicts = {row["id"]: row["agreement"] for row in comparison["cases"]}
    assert verdicts == {"a": "all_passed", "b": "split"}
    assert comparison["split_cases"] == ["b"]
    split = next(row for row in comparison["cases"] if row["id"] == "b")
    assert split["results"] == {
        "bedrock:a": {"status": "passed", "score": 1.0},
        "bedrock:b": {"status": "failed", "score": 0.2},
    }


def test_a_case_every_arm_fails_is_not_a_disagreement():
    comparison = compare([arm("a", result(NONE)), arm("b", result(NONE))])

    assert {row["agreement"] for row in comparison["cases"]} == {"all_failed"}
    assert comparison["split_cases"] == []


def test_a_case_an_arm_never_ran_is_incomplete_not_split():
    short = result([case("a", "passed", 1.0)])
    full = result(ALL_PASS)

    comparison = compare([arm("full", full), arm("short", short)])

    row = next(row for row in comparison["cases"] if row["id"] == "b")
    assert row["agreement"] == "incomplete"
    assert row["results"]["bedrock:short"] is None


def test_a_case_that_errored_or_went_unjudged_is_incomplete():
    errored = result([case("a", "error"), case("b", "judge_error")])

    comparison = compare([arm("a", result(ALL_PASS)), arm("b", errored)])

    assert {row["agreement"] for row in comparison["cases"]} == {"incomplete"}


def test_the_same_model_twice_is_refused():
    with pytest.raises(ValueError, match="different model"):
        compare([arm("same", result(ALL_PASS)), arm("same", result(HALF))])


def test_the_same_model_on_two_providers_is_two_arms():
    comparison = compare(
        [
            Arm("bedrock", "m", None, "completed", result(ALL_PASS)),
            Arm("openai", "m", None, "completed", result(HALF)),
        ]
    )

    assert comparison["winner"] == "bedrock:m"


def test_an_arm_summary_carries_what_a_reader_needs_to_judge_the_gap():
    comparison = compare([arm("m", result(HALF, score=61, cost=0.42))])

    summary = comparison["arms"][0]
    assert summary["label"] == "bedrock:m"
    assert summary["evaluation_id"] == "eval-m"
    assert summary["pass_rate"] == 0.5
    assert (summary["cases_passed"], summary["cases_total"]) == (1, 2)
    assert summary["repeats"] == 1
    assert summary["score"] == 61
    assert summary["cost_usd"] == 0.42


# --------------------------------------------------------------------------- #
# The real result shape: arms run through the engine
# --------------------------------------------------------------------------- #

ANSWERS = {"good-model": "The refund is approved. Happy to help.", "bad-model": "No."}


def answering_model(request) -> FakeModel:
    return FakeModel(script=[Text(ANSWERS[request.model_id])], usage_per_turn=(1000, 100))


def suite_request(model_id: str) -> EvaluationRequest:
    return EvaluationRequest.model_validate(
        {
            "kind": "suite",
            "suite": {
                "run_config": {"model_id": model_id},
                "cases": [
                    {
                        "id": "refund",
                        "input": "Refund?",
                        "judge": False,
                        "assert": [{"contains": "refund"}],
                    },
                    {
                        "id": "polite",
                        "input": "Hello",
                        "judge": False,
                        "assert": [{"contains": "help"}],
                    },
                ],
            },
        }
    )


async def run_arm(model_id: str) -> Arm:
    terminal = await evals_engine.execute_evaluation_with_seam(
        suite_request(model_id),
        Recorder().emit,
        RecordingStore(f"eval-{model_id}"),
        deps=evals_engine.EvalDeps(
            settings=Settings(),
            model_factory=answering_model,
            judge_factory=lambda _model_id, _provider="bedrock": FakeJudgeModel(),
        ),
    )
    return Arm("bedrock", model_id, f"eval-{model_id}", terminal["status"], terminal["result"])


async def test_real_arms_compare_on_the_fields_the_engine_produces(tmp_path):
    db.init_db(str(tmp_path / "compare.db"))

    comparison = compare([await run_arm("bad-model"), await run_arm("good-model")])

    assert comparison["winner"] == "bedrock:good-model"
    assert comparison["ranking"] == ["bedrock:good-model", "bedrock:bad-model"]
    by_label = {summary["label"]: summary for summary in comparison["arms"]}
    assert by_label["bedrock:good-model"]["pass_rate"] == 1.0
    assert by_label["bedrock:bad-model"]["pass_rate"] == 0.0
    assert (
        by_label["bedrock:good-model"]["cases_passed"],
        by_label["bedrock:good-model"]["cases_total"],
    ) == (2, 2)
    assert by_label["bedrock:good-model"]["repeats"] == 1
    assert by_label["bedrock:good-model"]["status"] == "completed"
    assert by_label["bedrock:good-model"]["evaluation_id"] == "eval-good-model"
    # Every case was decided by every arm, and the arms split on both.
    assert comparison["split_cases"] == ["refund", "polite"]
    refund = comparison["cases"][0]["results"]
    assert refund["bedrock:good-model"]["status"] == "passed"
    assert refund["bedrock:bad-model"]["status"] == "failed"
