"""A judge panel, and the judge never being told which model it is grading.

The merge rules are pinned on hand-built verdicts; the engine tests then run
real panels (scripted judges, real ``execute_evaluation_with_seam``) so the
fields a panel adds to a result are the ones the engine actually emits.
"""

from __future__ import annotations

from typing import Any

import pytest

from nimbus.config import Settings
from nimbus.engine.fake_model import FakeModel, Text
from nimbus.errors import BadRequestError
from nimbus.evals import engine as evals_engine
from nimbus.evals.grader import CaseVerdict, SuiteJudgement, combine_judgements
from nimbus.evals.judge import FakeJudgeModel
from nimbus.evals.schemas import MAX_PANEL_JUDGES, EvaluationRequest
from nimbus.store import db
from tests.test_evals_seam import Recorder, RecordingStore

# --------------------------------------------------------------------------- #
# Merging verdicts
# --------------------------------------------------------------------------- #


def judgement(
    scores: dict[int, float] | None = None,
    *,
    reasons: list[str] | None = None,
    errors: dict[int, str] | None = None,
    case_id: str = "c",
    error: str | None = None,
) -> SuiteJudgement:
    verdict = CaseVerdict(
        scores=dict(scores or {}), reasons=list(reasons or []), judge_errors=dict(errors or {})
    )
    return SuiteJudgement(verdicts={case_id: verdict}, error=error)


def test_a_single_judge_is_returned_unchanged():
    only = judgement({0: 0.8}, reasons=["fine"])

    assert combine_judgements([("bedrock:a", only)]) is only


def test_a_repeats_score_is_the_mean_of_the_judges_that_scored_it():
    merged = combine_judgements(
        [("bedrock:a", judgement({0: 0.9, 1: 0.5})), ("openai:b", judgement({0: 0.5, 1: 0.5}))]
    )

    verdict = merged.verdicts["c"]
    assert verdict.scores == {0: pytest.approx(0.7), 1: pytest.approx(0.5)}
    assert verdict.spread == pytest.approx(0.4)  # the widest gap, on repeat 0
    assert merged.error is None


def test_reasons_say_which_judge_gave_them():
    merged = combine_judgements(
        [
            ("bedrock:a", judgement({0: 1.0}, reasons=["exact"])),
            ("openai:b", judgement({0: 0.4}, reasons=["missed the date"])),
        ]
    )

    assert merged.verdicts["c"].reasons == ["[bedrock:a] exact", "[openai:b] missed the date"]


def test_a_judge_that_failed_a_repeat_others_scored_is_left_out_and_noted():
    merged = combine_judgements(
        [
            ("bedrock:a", judgement({0: 0.8})),
            ("openai:b", judgement(errors={0: "rate limited"})),
        ]
    )

    verdict = merged.verdicts["c"]
    assert verdict.scores == {0: 0.8}
    assert verdict.judge_errors == {}
    assert verdict.spread is None  # only one judge scored it: no gap to report
    assert "[openai:b] judge error on repeat 0: rate limited" in verdict.reasons


def test_a_repeat_no_judge_scored_stays_a_judge_error():
    merged = combine_judgements(
        [
            ("bedrock:a", judgement(errors={0: "boom"})),
            ("openai:b", judgement(errors={0: "also boom"})),
        ]
    )

    assert merged.verdicts["c"].scores == {}
    assert merged.verdicts["c"].judge_errors == {0: "boom"}


def test_the_suite_only_errors_when_every_judge_did():
    failed = SuiteJudgement(error="judge down")
    fine = judgement({0: 0.9})

    assert combine_judgements([("a", failed), ("b", fine)]).error is None
    assert combine_judgements([("a", failed), ("b", SuiteJudgement(error="also down"))]).error == (
        "judge down"
    )


def with_calibration(judged: SuiteJudgement, **probes: float | None) -> SuiteJudgement:
    judged.calibration = {"c": dict(probes)}
    return judged


def test_calibration_is_the_mean_of_the_judges_that_scored_each_probe():
    merged = combine_judgements(
        [
            ("a", with_calibration(judgement({0: 0.9}), reference=0.9, empty=0.1)),
            ("b", with_calibration(judgement({0: 0.5}), reference=0.5, empty=0.3)),
        ]
    )

    assert merged.calibration["c"]["reference"] == pytest.approx(0.7)
    assert merged.calibration["c"]["empty"] == pytest.approx(0.2)


def test_a_probe_only_some_judges_scored_is_the_mean_of_those_and_none_when_unjudged():
    merged = combine_judgements(
        [
            ("a", with_calibration(judgement({0: 0.9}), reference=0.8, empty=None)),
            ("b", with_calibration(judgement({0: 0.5}), reference=None, empty=None)),
        ]
    )

    assert merged.calibration["c"] == {"reference": 0.8, "empty": None}


def test_a_probe_no_judge_was_shown_is_absent():
    merged = combine_judgements(
        [
            ("a", with_calibration(judgement({0: 0.9}), empty=0.1)),
            ("b", with_calibration(judgement({0: 0.5}), empty=0.3)),
        ]
    )

    assert set(merged.calibration["c"]) == {"empty"}  # a case with no `expected` has no reference


def test_a_single_judges_calibration_passes_through_untouched():
    only = with_calibration(judgement({0: 0.9}), reference=0.9, empty=0.1)

    assert combine_judgements([("a", only)]).calibration == {"c": {"reference": 0.9, "empty": 0.1}}


def test_a_case_only_some_judges_saw_is_still_merged():
    merged = combine_judgements(
        [("a", judgement({0: 0.6}, case_id="x")), ("b", judgement({0: 0.8}, case_id="y"))]
    )

    assert merged.verdicts["x"].scores == {0: 0.6}
    assert merged.verdicts["y"].scores == {0: 0.8}


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #

SUITE = {
    "run_config": {"model_id": "m"},
    "cases": [{"id": "c1", "input": "q1"}, {"id": "c2", "input": "q2"}],
}
PRIMARY = {"model_id": "judge-a"}


def request(**fields: Any) -> EvaluationRequest:
    return EvaluationRequest.model_validate(
        {"kind": "suite", "suite": SUITE, "grader": PRIMARY, **fields}
    )


def test_a_suite_can_have_a_panel():
    built = request(panel=[{"model_id": "judge-b"}, {"provider": "openai", "model_id": "gpt-x"}])

    assert [judge.model_id for judge in built.panel] == ["judge-b", "gpt-x"]


def test_a_panel_is_optional_and_absent_from_the_stored_config_unless_used():
    assert request().panel == []
    assert "panel" not in request().stored_config()
    stored = request(panel=[{"model_id": "judge-b"}]).stored_config()
    assert [judge["model_id"] for judge in stored["panel"]] == ["judge-b"]


def test_a_panel_only_judges_suites():
    with pytest.raises(BadRequestError) as caught:
        EvaluationRequest.model_validate(
            {
                "kind": "determinism",
                "run_config": {"model_id": "m", "user_prompt": "hi"},
                "panel": [{"model_id": "judge-b"}],
            }
        )

    assert caught.value.code == "panel_requires_suite"


@pytest.mark.parametrize(
    "panel",
    [
        [{"model_id": "judge-a"}],  # the primary judge again
        [{"model_id": "judge-b"}, {"model_id": "judge-b"}],  # a repeat within the panel
    ],
)
def test_the_same_judge_twice_is_refused(panel):
    with pytest.raises(BadRequestError) as caught:
        request(panel=panel)

    assert caught.value.code == "panel_duplicate_judge"


def test_the_same_model_on_another_provider_is_a_different_judge():
    built = request(panel=[{"provider": "openai", "model_id": "judge-a"}])

    assert len(built.panel) == 1


def test_a_panel_has_a_bounded_size():
    too_many = [{"model_id": f"judge-{n}"} for n in range(MAX_PANEL_JUDGES + 1)]

    with pytest.raises(ValueError, match="at most"):
        request(panel=too_many)


def test_a_non_bedrock_panel_judge_must_name_its_model():
    with pytest.raises(BadRequestError) as caught:
        request(panel=[{"provider": "openai"}])

    assert caught.value.code == "judge_model_requires_provider"


# --------------------------------------------------------------------------- #
# Through the engine
# --------------------------------------------------------------------------- #

RUN_MODEL = "secret-model-xyz"
MILLION = 1_000_000


def run_model(_request) -> FakeModel:
    return FakeModel(script=[Text("An answer.")])


async def evaluate(built: EvaluationRequest, judges: dict[str, FakeJudgeModel]) -> dict[str, Any]:
    return await evals_engine.execute_evaluation_with_seam(
        built,
        Recorder().emit,
        RecordingStore("eval-panel"),
        deps=evals_engine.EvalDeps(
            settings=Settings(),
            model_factory=run_model,
            judge_factory=lambda model_id, provider="bedrock": judges[model_id],
        ),
    )


def suite_request(**fields: Any) -> EvaluationRequest:
    return EvaluationRequest.model_validate(
        {
            "kind": "suite",
            "suite": {
                "run_config": {"model_id": RUN_MODEL},
                "cases": [{"id": "c1", "input": "What is the refund window?"}],
                "repeats": 1,
            },
            **fields,
        }
    )


@pytest.fixture
def initialized_db(tmp_path):
    return db.init_db(str(tmp_path / "panel.db"))


async def test_a_panel_scores_each_case_with_the_mean_of_its_judges(initialized_db):
    judges = {"judge-a": FakeJudgeModel(score=0.9), "judge-b": FakeJudgeModel(score=0.5)}
    built = suite_request(grader={"model_id": "judge-a"}, panel=[{"model_id": "judge-b"}])

    terminal = await evaluate(built, judges)

    assert terminal["status"] == "completed"
    case = terminal["result"]["cases"][0]
    assert case["score"] == pytest.approx(0.7)
    assert case["judge_spread"] == pytest.approx(0.4)
    assert "[bedrock:judge-a]" in case["reasoning"] and "[bedrock:judge-b]" in case["reasoning"]
    assert terminal["result"]["judge"]["panel"] == ["bedrock:judge-b"]
    assert all(judge.calls for judge in judges.values())  # every judge graded the answer


async def test_a_calibrated_suite_keeps_its_calibration_under_a_panel(initialized_db):
    judges = {"judge-a": FakeJudgeModel(score=0.9), "judge-b": FakeJudgeModel(score=0.5)}
    built = suite_request(grader={"model_id": "judge-a"}, panel=[{"model_id": "judge-b"}])
    built.suite.calibrate = True
    built.suite.cases[0].expected = "The window is 30 days."

    terminal = await evaluate(built, judges)

    calibration = terminal["result"]["calibration"]
    assert calibration["cases"] == 1  # not lost when the verdicts were merged
    assert calibration["reference_mean"] == pytest.approx(0.7)
    assert calibration["empty_mean"] == pytest.approx(0.7)
    assert terminal["result"]["cases"][0]["calibration"]["reference"] == pytest.approx(0.7)


async def test_a_suite_without_a_panel_reports_no_panel_fields(initialized_db):
    judges = {"judge-a": FakeJudgeModel(score=0.9)}

    terminal = await evaluate(suite_request(grader={"model_id": "judge-a"}), judges)

    case = terminal["result"]["cases"][0]
    assert "judge_spread" not in case
    assert "panel" not in terminal["result"]["judge"]
    assert "[bedrock:" not in case["reasoning"]  # not prefixed when one judge speaks


async def test_a_panel_judge_without_a_system_prompt_uses_the_primarys(initialized_db):
    judges = {"judge-a": FakeJudgeModel(), "judge-b": FakeJudgeModel()}
    built = suite_request(
        grader={"model_id": "judge-a", "system_prompt": "Be strict."},
        panel=[{"model_id": "judge-b"}],
    )

    await evaluate(built, judges)

    assert judges["judge-a"].system_prompts == ["Be strict."]
    assert judges["judge-b"].system_prompts == ["Be strict."]


async def test_a_panel_member_keeps_its_own_system_prompt(initialized_db):
    judges = {"judge-a": FakeJudgeModel(), "judge-b": FakeJudgeModel()}
    built = suite_request(
        grader={"model_id": "judge-a", "system_prompt": "Be strict."},
        panel=[{"model_id": "judge-b", "system_prompt": "Be kind."}],
    )

    await evaluate(built, judges)

    assert judges["judge-b"].system_prompts == ["Be kind."]


async def test_a_panel_judge_that_fails_does_not_sink_the_case(initialized_db):
    failing = FakeJudgeModel(script=[Text("this is not a structured verdict")])
    judges = {"judge-a": FakeJudgeModel(score=0.8), "judge-b": failing}
    built = suite_request(grader={"model_id": "judge-a"}, panel=[{"model_id": "judge-b"}])

    terminal = await evaluate(built, judges)

    case = terminal["result"]["cases"][0]
    assert case["status"] in ("passed", "failed")  # scored by the judge that worked
    assert case["score"] == pytest.approx(0.8)
    assert "judge error on repeat 0" in case["reasoning"]


async def test_the_judge_never_sees_the_model_it_is_grading(initialized_db):
    """Blind grading: nothing about which model answered reaches any judge's prompt.

    This is what makes a comparison across models fair to the judge; it holds
    for every judge in a panel too. The prompt does contain the case input, so
    an empty prompt cannot pass this vacuously.
    """
    judges = {"judge-a": FakeJudgeModel(), "judge-b": FakeJudgeModel()}
    built = suite_request(grader={"model_id": "judge-a"}, panel=[{"model_id": "judge-b"}])

    await evaluate(built, judges)

    for judge in judges.values():
        seen = "\n".join(judge.prompts + [str(prompt) for prompt in judge.system_prompts])
        assert "What is the refund window?" in seen
        assert RUN_MODEL not in seen
