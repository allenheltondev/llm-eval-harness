"""LLM-as-judge grading, on top of ``strands_evals``.

How the judge is actually invoked
---------------------------------
::

    Experiment(cases=[Case(...), ...], evaluators=[OutputEvaluator(...), ...])
        .run_evaluations_async(task, max_workers=...)  ->  EvaluationReport

* One :class:`~strands_evals.Case` per run. For a determinism batch every case
  carries the *same* ``input`` (the prompt that was repeated) -- N duplicate
  cases, which is how ``strands_evals`` expresses repeats, since ``Case`` has no
  ``repeats`` field.
* The ``task`` handler does **no** model work: the runs already happened, so it
  simply replays each case's stored output/trajectory out of ``Case.metadata``.
  All the LLM traffic in this module is the judge's.
* ``OutputEvaluator(rubric=..., model=..., system_prompt=...)`` is the judge.
  ``model`` is a ``Model`` *instance* (see :mod:`nimbus.evals.judge`), and
  both ``rubric`` and ``system_prompt`` come straight from the request when the
  caller supplied them, and the tests assert both strings reach the judge.
* When any run used tools, a second evaluator -- ``TrajectoryEvaluator`` -- grades
  each run's tool-call sequence against the batch's modal sequence. The purely
  objective counterpart lives in :mod:`nimbus.evals.metrics`. A custom
  ``rubric`` governs the headline output judge; the trajectory judge keeps the
  tool-consistency rubric so that dimension stays comparable across evaluations
  (a custom ``system_prompt``, being about *how* to judge, reaches both).

Determinism without ground truth
--------------------------------
An LLM judge scores one case at a time, so "are these N runs consistent?" is
turned into "does this run agree with the batch's reference run?": the modal
(most common) output becomes each case's ``expected_output`` and the modal
tool-call sequence its ``expected_trajectory``. The reference run trivially
agrees with itself, which is the intended floor -- a perfectly deterministic
batch scores 1.0 across every case.

Failure policy
--------------
``Experiment`` isolates evaluator errors: a judge that raises comes back as a
row with ``score=0`` and ``reason="Evaluator error: ..."`` rather than an
exception. Grading them as an F would be a lie, so those rows are detected and
reported as ``judge_error`` with ``grade``/``score``/``reasoning`` left ``None``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from strands_evals import Case, Experiment
from strands_evals.evaluators.output_evaluator import OutputEvaluator
from strands_evals.evaluators.trajectory_evaluator import TrajectoryEvaluator
from strands_evals.types.evaluation_report import EvaluationReport

from nimbus.evals import rubrics
from nimbus.evals.judge import JudgeFactory, call_judge_factory
from nimbus.evals.metrics import modal_value, tool_signature
from nimbus.evals.outcomes import RunOutcome
from nimbus.evals.schemas import GraderConfig, Suite

logger = logging.getLogger(__name__)

OUTPUT_EVALUATOR_NAME = "OutputEvaluator"
TRAJECTORY_EVALUATOR_NAME = "TrajectoryEvaluator"
#: Set explicitly on the suite's evaluator. Report rows are tagged with the
#: evaluator's *instance* name, falling back to its class name -- so without
#: this a subclass reports as ``CaseCriteriaOutputEvaluator``, a filter on
#: ``OutputEvaluator`` matches nothing, and every suite would read as "the judge
#: produced no results".
SUITE_EVALUATOR_NAME = "SuiteCaseEvaluator"
#: At most this many judge calls at once for a suite. A determinism batch caps
#: out at 25 cases; a suite can be 200, and firing every judge call at once is a
#: throttle, not a speed-up.
MAX_SUITE_JUDGE_CONCURRENCY = 8

_EVALUATOR_ERROR_PREFIX = "Evaluator error:"


@dataclass
class JudgeResult:
    """What the judge contributed to an evaluation's ``result``."""

    grade: str | None = None
    score: int | None = None
    reasoning: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _trajectory(outcome: RunOutcome) -> list[str]:
    """A run's tool calls as the flat ``name(input)`` list a judge can read."""
    return list(tool_signature(outcome.tool_transcript))


def _task(case: Case) -> dict[str, Any]:
    """Replay a case's already-recorded run. No model call happens here."""
    metadata = case.metadata or {}
    return {
        "output": metadata.get("output", ""),
        "trajectory": metadata.get("trajectory", []),
    }


def _rows_for(report: EvaluationReport, evaluator_name: str) -> list[tuple[float, str, str]]:
    """``(score, reason, case_name)`` for one evaluator's rows of a report."""
    rows: list[tuple[float, str, str]] = []
    for index, case in enumerate(report.cases):
        if case.get("evaluator") != evaluator_name:
            continue
        score = report.scores[index] if index < len(report.scores) else 0.0
        reason = report.reasons[index] if index < len(report.reasons) else ""
        rows.append((float(score), reason, str(case.get("name") or f"case-{index + 1}")))
    return rows


def _judge_failure(rows: list[tuple[float, str, str]]) -> str | None:
    """The judge's error message when *every* row is an isolated evaluator error."""
    if not rows:
        return "The judge produced no results"
    if all(reason.startswith(_EVALUATOR_ERROR_PREFIX) for _score, reason, _name in rows):
        return rows[0][1][len(_EVALUATOR_ERROR_PREFIX) :].strip()
    return None


def _build_cases(
    outcomes: list[RunOutcome],
    *,
    reference_output: str | None,
    reference_trajectory: list[str] | None,
) -> list[Case]:
    cases: list[Case] = []
    for outcome in outcomes:
        trajectory = _trajectory(outcome)
        cases.append(
            Case(
                name=f"run-{outcome.index + 1}",
                input=outcome.user_prompt,
                expected_output=reference_output,
                expected_trajectory=reference_trajectory or None,
                metadata={
                    "run_id": outcome.run_id,
                    "index": outcome.index,
                    "output": outcome.output,
                    "trajectory": trajectory,
                },
            )
        )
    return cases


async def judge(
    outcomes: list[RunOutcome],
    *,
    kind: str,
    rubric: str | None,
    grader: GraderConfig,
    judge_factory: JudgeFactory,
) -> JudgeResult:
    """Grade ``outcomes`` with the configured judge; never raises.

    ``kind="determinism"`` grades each run against the batch's modal run;
    ``kind="grade"`` grades each stored run on its own merits.
    """
    if not outcomes:
        return JudgeResult(error="No successful runs to grade")

    determinism = kind == "determinism"
    outputs = [outcome.output for outcome in outcomes]
    reference_output = modal_value(outputs) if determinism else None
    trajectories = [_trajectory(outcome) for outcome in outcomes]
    has_tools = any(trajectories)
    reference_trajectory = (
        list(modal_value([tuple(t) for t in trajectories]) or ()) if determinism else None
    )

    default_system_prompt = (
        rubrics.DETERMINISM_SYSTEM_PROMPT if determinism else rubrics.GRADE_SYSTEM_PROMPT
    )
    default_rubric = rubrics.DETERMINISM_RUBRIC if determinism else rubrics.GRADE_RUBRIC
    system_prompt = grader.system_prompt or default_system_prompt
    effective_rubric = rubric or default_rubric

    try:
        model = call_judge_factory(judge_factory, grader.model_id, grader.provider)
        evaluators: list[Any] = [
            OutputEvaluator(
                rubric=effective_rubric,
                model=model,
                system_prompt=system_prompt,
                include_inputs=True,
            )
        ]
        if determinism and has_tools:
            evaluators.append(
                TrajectoryEvaluator(
                    rubric=rubrics.TOOL_CONSISTENCY_RUBRIC,
                    model=model,
                    # A custom grader prompt governs every judge in the run.
                    system_prompt=grader.system_prompt or rubrics.TOOL_CONSISTENCY_SYSTEM_PROMPT,
                    include_inputs=True,
                )
            )

        cases = _build_cases(
            outcomes,
            reference_output=reference_output,
            reference_trajectory=reference_trajectory,
        )
        experiment = Experiment(cases=cases, evaluators=evaluators)
        report = await experiment.run_evaluations_async(_task, max_workers=len(cases))
    except Exception as exc:  # the judge is best-effort; local metrics still stand
        logger.warning("judge failed: %s", exc, exc_info=True)
        return JudgeResult(error=str(exc) or exc.__class__.__name__)

    output_rows = _rows_for(report, OUTPUT_EVALUATOR_NAME)
    failure = _judge_failure(output_rows)
    if failure is not None:
        logger.warning("judge failed for every case: %s", failure)
        return JudgeResult(error=failure)

    scores = [score for score, _reason, _name in output_rows]
    mean_score = sum(scores) / len(scores)
    score_100 = max(0, min(100, round(mean_score * 100)))

    metrics: dict[str, Any] = {
        "judge_overall_score": mean_score,
        "judge_scores": scores,
        "judge_pass_rate": (
            sum(1 for passed in report.test_passes if passed) / len(report.test_passes)
            if report.test_passes
            else 0.0
        ),
    }

    trajectory_rows = _rows_for(report, TRAJECTORY_EVALUATOR_NAME)
    if trajectory_rows:
        trajectory_scores = [score for score, _reason, _name in trajectory_rows]
        metrics["tool_consistency_judge_score"] = sum(trajectory_scores) / len(trajectory_scores)

    lines = [f"{name}: {reason}" for _score, reason, name in output_rows if reason]
    lines += [f"{name} (tools): {reason}" for _score, reason, name in trajectory_rows if reason]
    reasoning = "\n".join(lines)

    return JudgeResult(
        grade=rubrics.score_to_grade(score_100),
        score=score_100,
        reasoning=reasoning or None,
        metrics=metrics,
    )


# --------------------------------------------------------------------------- #
# Test suites
# --------------------------------------------------------------------------- #


class CaseCriteriaOutputEvaluator(OutputEvaluator):
    """An ``OutputEvaluator`` that also shows the judge this case's own criteria.

    ``OutputEvaluator`` takes one rubric for the whole experiment and ignores
    ``expected_assertion``, but a test suite's cases each carry requirements of
    their own. They travel on the case as ``expected_assertion`` -- the field
    ``strands_evals`` defines for human-authored success assertions -- and are
    appended after the rubric. ``_build_prompt`` is the library's documented
    override point for exactly this.
    """

    def _build_prompt(self, evaluation_case: Any) -> str | list:
        prompt = super()._build_prompt(evaluation_case)
        criteria = evaluation_case.expected_assertion
        if criteria and isinstance(prompt, str):
            prompt += f"\n<CaseCriteria>{criteria}</CaseCriteria>"
        return prompt


@dataclass
class CaseVerdict:
    """What the judge said about each repeat of one case, keyed by run index.

    Per repeat, not pooled: a case's score has to account for every repeat --
    one that failed to run, one the judge never scored -- and a pooled list of
    the scores that happened to come back cannot tell which ones are missing.
    """

    scores: dict[int, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    judge_errors: dict[int, str] = field(default_factory=dict)


@dataclass
class SuiteJudgement:
    """Per-case verdicts, keyed by case id, plus a failure of the judge as a whole."""

    verdicts: dict[str, CaseVerdict] = field(default_factory=dict)
    error: str | None = None


def _suite_case_name(outcome: RunOutcome) -> str:
    # `#` cannot appear in a case id (see SuiteCase.id), so this splits cleanly.
    return f"{outcome.case_id}#{outcome.index}"


async def judge_suite(
    outcomes: list[RunOutcome],
    *,
    suite: Suite,
    rubric: str | None,
    grader: GraderConfig,
    judge_factory: JudgeFactory,
) -> SuiteJudgement:
    """Grade each successful run against *its own case*; never raises.

    Unlike determinism there is no reference run to manufacture: every case
    brings its own ``expected`` answer and ``criteria``. A judge failure on one
    row is that row's ``judge_error``, not an F.
    """
    if not outcomes:
        return SuiteJudgement(error="No successful runs to grade")
    cases_by_id = {case.id: case for case in suite.cases}

    try:
        model = call_judge_factory(judge_factory, grader.model_id, grader.provider)
        evaluator = CaseCriteriaOutputEvaluator(
            rubric=rubric or rubrics.SUITE_RUBRIC,
            model=model,
            system_prompt=grader.system_prompt or rubrics.SUITE_SYSTEM_PROMPT,
            include_inputs=True,
            name=SUITE_EVALUATOR_NAME,
        )
        cases = []
        for outcome in outcomes:
            case = cases_by_id[str(outcome.case_id)]
            cases.append(
                Case(
                    name=_suite_case_name(outcome),
                    input=case.input,
                    expected_output=case.expected,
                    expected_assertion=case.criteria,
                    metadata={
                        "run_id": outcome.run_id,
                        "index": outcome.index,
                        "output": outcome.output,
                        "trajectory": _trajectory(outcome),
                    },
                )
            )
        experiment = Experiment(cases=cases, evaluators=[evaluator])
        report = await experiment.run_evaluations_async(
            _task, max_workers=min(len(cases), MAX_SUITE_JUDGE_CONCURRENCY)
        )
    except Exception as exc:  # the judge is best-effort; the runs still stand
        logger.warning("suite judge failed: %s", exc, exc_info=True)
        return SuiteJudgement(error=str(exc) or exc.__class__.__name__)

    rows = _rows_for(report, SUITE_EVALUATOR_NAME)
    judgement = SuiteJudgement(error=_judge_failure(rows))
    for score, reason, name in rows:
        case_id, _, index = name.rpartition("#")
        verdict = judgement.verdicts.setdefault(case_id, CaseVerdict())
        if reason.startswith(_EVALUATOR_ERROR_PREFIX):
            verdict.judge_errors[int(index)] = reason[len(_EVALUATOR_ERROR_PREFIX) :].strip()
        else:
            verdict.scores[int(index)] = score
            if reason:
                verdict.reasons.append(reason)
    return judgement
