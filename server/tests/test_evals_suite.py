"""Test suites (``kind="suite"``), driven through the real engine.

Every test here runs ``execute_evaluation_with_seam`` itself -- real run engine,
real SQLite, real ``strands_evals`` Experiment -- with the fake model standing
in for the application and a fake judge standing in for the grader. The doubles
are the only fakes: the per-case wiring, the judge prompt and the aggregation
are all the real thing, because that is where a suite can quietly go wrong.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from evalharness.config import Settings
from evalharness.engine.fake_model import FakeModel, Text
from evalharness.errors import BadRequestError
from evalharness.evals import engine as evals_engine
from evalharness.evals import grader, rubrics
from evalharness.evals.judge import FakeJudgeModel
from evalharness.evals.schemas import (
    MAX_SUITE_CASES,
    MAX_SUITE_RUNS,
    EvaluationRequest,
)
from evalharness.store import db
from tests.test_evals_seam import Recorder, RecordingStore

# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #


class AnswerBook:
    """The application under test: answers each case's input from a table.

    A model factory is handed the run's ``RunRequest``, so the answer can depend
    on which case is running -- which is the whole point of a suite.
    """

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.prompts: list[str] = []

    def __call__(self, request) -> FakeModel:
        self.prompts.append(request.user_prompt)
        return FakeModel(script=[Text(self.answers.get(request.user_prompt, "I don't know."))])


class RoutingJudge(FakeJudgeModel):
    """A judge whose verdict depends on what it is shown.

    Routes each judge call to the first scripted verdict whose needle appears in
    the prompt, so a test can make the right answer pass and the wrong one fail.
    """

    def __init__(self, routes: list[tuple[str, float, str]], default: float = 0.5) -> None:
        super().__init__(score=default, reason="[judge] default verdict")
        self._routes = [
            (needle, FakeJudgeModel(score=score, test_pass=score >= 0.7, reason=reason))
            for needle, score, reason in routes
        ]
        self.seen: list[str] = []
        self.system_prompts_seen: list[str | None] = []

    def stream(self, messages, *args, **kwargs):  # type: ignore[override]
        text = "".join(
            block.get("text", "")
            for message in messages
            if message.get("role") == "user"
            for block in message.get("content", [])
        )
        self.seen.append(text)
        # stream(messages, tool_specs, system_prompt, ...): positional or keyword.
        positional = args[1] if len(args) > 1 else None
        self.system_prompts_seen.append(kwargs.get("system_prompt") or positional)
        for needle, judge in self._routes:
            if needle in text:
                return judge.stream(messages, *args, **kwargs)
        return super().stream(messages, *args, **kwargs)


@pytest.fixture
def initialized_db(tmp_path):
    return db.init_db(str(tmp_path / "suite.db"))


@pytest.fixture(autouse=True)
def instant_retries(monkeypatch):
    monkeypatch.setattr(evals_engine, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))


def suite_request(cases: list[dict[str, Any]], **suite_fields: Any) -> EvaluationRequest:
    return EvaluationRequest.model_validate(
        {
            "kind": "suite",
            "suite": {
                "name": "support",
                "run_config": {"model_id": "fake.model", "system_prompt": "You are support."},
                "cases": cases,
                **suite_fields,
            },
        }
    )


async def run_suite(
    request: EvaluationRequest, answers: AnswerBook, judge: FakeJudgeModel
) -> tuple[dict[str, Any], Recorder]:
    recorder = Recorder()
    terminal = await evals_engine.execute_evaluation_with_seam(
        request,
        recorder.emit,
        RecordingStore("eval-suite"),
        recorder.cancelled,
        deps=evals_engine.EvalDeps(
            settings=Settings(),
            model_factory=answers,
            judge_factory=lambda _model_id, _provider="bedrock": judge,
        ),
    )
    return terminal, recorder


REFUND = {
    "id": "refund-window",
    "input": "Can I return shoes after 45 days?",
    "expected": "No. Returns are accepted within 30 days of purchase.",
    "criteria": "Must state the 30-day window.",
}
HOURS = {
    "id": "store-hours",
    "input": "When do you open on Sunday?",
    "expected": "10am on Sundays.",
}


# --------------------------------------------------------------------------- #
# The happy path, per case
# --------------------------------------------------------------------------- #


async def test_each_case_is_run_and_judged_on_its_own_terms(initialized_db):
    answers = AnswerBook(
        {
            REFUND["input"]: "Sorry, returns are only accepted within 30 days.",
            HOURS["input"]: "We open at 9am.",  # wrong
        }
    )
    judge = RoutingJudge(
        [
            ("within 30 days", 0.95, "states the 30-day window"),
            ("We open at 9am", 0.2, "says 9am; the reference says 10am"),
        ]
    )

    terminal, recorder = await run_suite(suite_request([REFUND, HOURS]), answers, judge)

    assert terminal["status"] == "completed"
    result = terminal["result"]
    by_id = {case["id"]: case for case in result["cases"]}
    # In suite order, whatever order the runs finished in.
    assert [case["id"] for case in result["cases"]] == ["refund-window", "store-hours"]
    assert by_id["refund-window"]["status"] == "passed"
    assert by_id["refund-window"]["score"] == pytest.approx(0.95)
    assert by_id["refund-window"]["reasoning"] == "states the 30-day window"
    assert by_id["store-hours"]["status"] == "failed"
    assert by_id["store-hours"]["passed"] is False
    assert by_id["store-hours"]["score"] == pytest.approx(0.2)

    # The application really was asked each case's own question.
    assert sorted(answers.prompts) == sorted([REFUND["input"], HOURS["input"]])

    metrics = result["metrics"]
    assert metrics["cases_total"] == 2
    assert metrics["cases_passed"] == 1
    assert metrics["cases_failed"] == 1
    assert metrics["pass_rate"] == pytest.approx(0.5)
    assert result["score"] == round((0.95 + 0.2) / 2 * 100)
    assert result["grade"] == rubrics.score_to_grade(result["score"])
    assert result["reasoning"] == "1/2 cases passed (threshold 0.70); failed: store-hours"
    assert result["suite"] == {"name": "support", "repeats": 1, "pass_threshold": 0.7}
    assert recorder.types[-1] == "eval_complete"


async def test_the_judge_sees_the_reference_the_criteria_and_the_suite_rubric(initialized_db):
    answers = AnswerBook({REFUND["input"]: "Within 30 days only.", HOURS["input"]: "10am."})
    judge = RoutingJudge([], default=0.9)

    await run_suite(suite_request([REFUND, HOURS]), answers, judge)

    refund_prompt = next(p for p in judge.seen if REFUND["input"] in p)
    hours_prompt = next(p for p in judge.seen if HOURS["input"] in p)
    assert f"<ExpectedOutput>{REFUND['expected']}</ExpectedOutput>" in refund_prompt
    assert f"<CaseCriteria>{REFUND['criteria']}</CaseCriteria>" in refund_prompt
    assert f"<Rubric>{rubrics.SUITE_RUBRIC}</Rubric>" in refund_prompt
    # Criteria are per case: the case without any gets none.
    assert "<CaseCriteria>" not in hours_prompt
    assert rubrics.SUITE_SYSTEM_PROMPT in judge.system_prompts_seen


async def test_a_custom_rubric_and_judge_system_prompt_replace_the_defaults(initialized_db):
    answers = AnswerBook({REFUND["input"]: "30 days."})
    judge = RoutingJudge([], default=0.9)
    request = suite_request([REFUND])
    request = request.model_copy(
        update={
            "rubric": "Be terse.",
            "grader": request.grader.model_copy(update={"system_prompt": "Judge harshly."}),
        }
    )

    terminal, _ = await run_suite(request, answers, judge)

    assert "<Rubric>Be terse.</Rubric>" in judge.seen[0]
    assert "Judge harshly." in judge.system_prompts_seen
    assert terminal["result"]["judge"]["rubric_used"] is True


# --------------------------------------------------------------------------- #
# Repeats, thresholds, events
# --------------------------------------------------------------------------- #


async def test_repeats_run_each_case_that_many_times_and_average(initialized_db):
    answers = AnswerBook({REFUND["input"]: "Within 30 days."})
    judge = RoutingJudge([], default=0.8)

    terminal, _ = await run_suite(suite_request([REFUND], repeats=3), answers, judge)

    case = terminal["result"]["cases"][0]
    assert case["runs"] == {"total": 3, "succeeded": 3}
    assert case["scores"] == [0.8, 0.8, 0.8]
    assert len(case["run_ids"]) == 3
    assert answers.prompts == [REFUND["input"]] * 3


async def test_the_pass_threshold_is_the_suites_to_set(initialized_db):
    answers = AnswerBook({REFUND["input"]: "Within 30 days."})
    judge = RoutingJudge([], default=0.8)

    terminal, _ = await run_suite(suite_request([REFUND], pass_threshold=0.9), answers, judge)

    case = terminal["result"]["cases"][0]
    assert case["score"] == pytest.approx(0.8)
    assert case["status"] == "failed"


async def test_a_score_exactly_at_the_threshold_passes(initialized_db):
    answers = AnswerBook({REFUND["input"]: "Within 30 days."})
    judge = RoutingJudge([], default=0.75)

    terminal, _ = await run_suite(suite_request([REFUND], pass_threshold=0.75), answers, judge)

    assert terminal["result"]["cases"][0]["status"] == "passed"


async def test_run_events_name_their_case(initialized_db):
    answers = AnswerBook({REFUND["input"]: "30 days.", HOURS["input"]: "10am."})
    terminal, recorder = await run_suite(
        suite_request([REFUND, HOURS]), answers, RoutingJudge([], default=0.9)
    )

    run_events = [e for e in recorder.events if e["type"] in ("run_started", "run_completed")]
    assert {e["case_id"] for e in run_events} == {"refund-window", "store-hours"}
    start = next(e for e in recorder.events if e["type"] == "eval_start")
    assert start["kind"] == "suite"
    assert start["n"] == 2


# --------------------------------------------------------------------------- #
# Failure is reported per case, never as a silent pass
# --------------------------------------------------------------------------- #


async def test_a_case_whose_run_failed_is_an_error_not_a_pass(initialized_db):
    class Failing(AnswerBook):
        def __call__(self, request):
            if request.user_prompt == HOURS["input"]:
                raise RuntimeError("model unavailable")
            return super().__call__(request)

    answers = Failing({REFUND["input"]: "Within 30 days."})
    terminal, recorder = await run_suite(
        suite_request([REFUND, HOURS]), answers, RoutingJudge([], default=0.9)
    )

    result = terminal["result"]
    hours = next(case for case in result["cases"] if case["id"] == "store-hours")
    assert hours["status"] == "error"
    assert hours["passed"] is False
    assert hours["score"] is None
    assert hours["error"]["message"]
    assert result["metrics"]["cases_errored"] == 1
    assert result["metrics"]["pass_rate"] == pytest.approx(0.5)
    # The headline score is over judged cases only; pass_rate is the strict number.
    assert result["score"] == 90
    assert "did not run: store-hours" in result["reasoning"]
    assert [f["case_id"] for f in result["failed_runs"]] == ["store-hours"]
    failed = next(e for e in recorder.events if e["type"] == "run_failed")
    assert failed["case_id"] == "store-hours"


async def test_a_case_the_judge_could_not_score_is_a_judge_error(initialized_db, monkeypatch):
    """A row the judge errored on is unknown, not an F -- and not a pass."""
    original = grader._rows_for

    def one_row_errors(report, name):
        rows = original(report, name)
        return [
            (0.0, "Evaluator error: rate limited", name) if name.startswith("store-hours") else row
            for row in rows
            for name in [row[2]]
        ]

    monkeypatch.setattr(grader, "_rows_for", one_row_errors)
    answers = AnswerBook({REFUND["input"]: "30 days.", HOURS["input"]: "10am."})

    terminal, _ = await run_suite(
        suite_request([REFUND, HOURS]), answers, RoutingJudge([], default=0.9)
    )

    hours = next(case for case in terminal["result"]["cases"] if case["id"] == "store-hours")
    assert hours["status"] == "judge_error"
    assert hours["passed"] is False
    assert hours["error"] == {"code": "judge_error", "message": "rate limited"}
    assert "not judged: store-hours" in terminal["result"]["reasoning"]


async def test_a_judge_that_cannot_be_built_leaves_every_case_unjudged(initialized_db):
    answers = AnswerBook({REFUND["input"]: "30 days."})

    def no_judge(_model_id, _provider="bedrock"):
        raise RuntimeError("no credentials for the judge")

    recorder = Recorder()
    terminal = await evals_engine.execute_evaluation_with_seam(
        suite_request([REFUND]),
        recorder.emit,
        RecordingStore("eval-suite"),
        deps=evals_engine.EvalDeps(
            settings=Settings(), model_factory=answers, judge_factory=no_judge
        ),
    )

    result = terminal["result"]
    assert result["judge_error"] == "no credentials for the judge"
    assert result["cases"][0]["status"] == "judge_error"
    assert result["grade"] is None
    assert result["score"] is None


async def test_every_run_failing_is_an_errored_evaluation(initialized_db):
    class Broken(AnswerBook):
        def __call__(self, request):
            raise RuntimeError("model unavailable")

    terminal, _ = await run_suite(suite_request([REFUND]), Broken({}), RoutingJudge([]))

    assert terminal["status"] == "error"
    assert terminal["error"]["code"] == "no_successful_runs"


def test_long_judge_reasoning_is_clipped_per_case():
    verdict = grader.CaseVerdict(scores={0: 0.9}, reasons=["x" * 5_000])
    outcome = evals_engine.RunOutcome(index=0, run_id="r", status="completed", case_id="c")

    entry = evals_engine._suite_case_result("c", [outcome], verdict, None, 0.7)

    stored = _stored_bytes(entry["reasoning"]) - 2  # the quotes are the key's, not the text's
    limit = evals_engine.MAX_CASE_REASONING_BYTES
    assert limit - 10 < stored <= limit
    assert entry["reasoning"].endswith("…")


def _stored_bytes(value: Any) -> int:
    """Bytes as the store writes them: plain ``json.dumps``, non-ASCII escaped."""
    return len(json.dumps(value, default=str))


@pytest.mark.parametrize("char", ["x", "語", "😀"])
def test_reasoning_is_clipped_by_the_bytes_it_is_stored_as(char):
    verdict = grader.CaseVerdict(scores={0: 0.9}, reasons=[char * 5_000])
    outcome = evals_engine.RunOutcome(index=0, run_id="r", status="completed", case_id="c")

    entry = evals_engine._suite_case_result("c", [outcome], verdict, None, 0.7)

    # 1,000 characters of CJK would be ~6 KB stored; the limit is on that.
    assert _stored_bytes(entry["reasoning"]) - 2 <= evals_engine.MAX_CASE_REASONING_BYTES
    assert entry["reasoning"].rstrip("…").strip(char) == ""


def _largest_suite_result(
    reason: str, message: str
) -> tuple[dict[str, Any], list[evals_engine.RunOutcome]]:
    """The biggest result a suite can produce: every limit at its maximum.

    100 cases with 100-character ids, the run cap spent on repeats, every run
    judged with ``reason`` -- plus, for half the cases, a failed repeat whose
    error message is ``message``.
    """
    repeats = MAX_SUITE_RUNS // MAX_SUITE_CASES
    ids = [f"{index:03d}" + "x" * 97 for index in range(MAX_SUITE_CASES)]
    request = EvaluationRequest.model_validate(
        _suite_body(cases=[{"id": case_id, "input": "hi"} for case_id in ids], repeats=repeats)
    )
    outcomes, verdicts = [], {}
    for number, case_id in enumerate(ids):
        verdict = grader.CaseVerdict()
        for repeat in range(repeats):
            index = number * repeats + repeat
            outcome = evals_engine.RunOutcome(
                index=index, run_id=f"{index:032x}", status="completed", case_id=case_id
            )
            if number % 2 and repeat == 0:
                outcome.status = "error"
                outcome.error = {"code": "provider_error", "message": message, "retryable": False}
            else:
                verdict.scores[index] = 0.5
                verdict.reasons.append(reason)
            outcomes.append(outcome)
        verdicts[case_id] = verdict
    judged = grader.SuiteJudgement(verdicts=verdicts, error=message)
    return evals_engine._build_suite_result(outcomes, judged, request), outcomes


@pytest.mark.parametrize("char", ["x", "語", "😀"])
def test_the_largest_suite_result_fits_its_byte_budget(char):
    result, _ = _largest_suite_result(reason=char * 10_000, message=char * 10_000)

    assert _stored_bytes(result) <= evals_engine.MAX_RESULT_BYTES
    assert result["truncated"] is True
    # The prose gave way; the verdicts did not.
    assert len(result["cases"]) == MAX_SUITE_CASES
    assert all(case["status"] in ("failed", "error") for case in result["cases"])
    assert result["metrics"]["cases_total"] == MAX_SUITE_CASES


def test_fitting_the_budget_leaves_the_run_outcomes_untouched():
    result, outcomes = _largest_suite_result(reason="語" * 10_000, message="語" * 10_000)

    assert len(result["failed_runs"][0]["error"]["message"]) < 10_000  # clipped here...
    failed = [outcome for outcome in outcomes if not outcome.succeeded]
    assert all(len(outcome.error["message"]) == 10_000 for outcome in failed)  # ...not there


def test_a_result_within_budget_is_not_marked_truncated():
    result, _ = _largest_suite_result(reason="fine", message="boom")

    assert "truncated" not in result
    assert result["judge_error"] == "boom"


# --------------------------------------------------------------------------- #
# The request schema
# --------------------------------------------------------------------------- #


def _suite_body(**suite: Any) -> dict[str, Any]:
    base = {"run_config": {"model_id": "m"}, "cases": [{"id": "a", "input": "hi"}]}
    return {"kind": "suite", "suite": {**base, **suite}}


def test_a_suite_is_required_for_kind_suite():
    with pytest.raises(BadRequestError, match="suite is required"):
        EvaluationRequest.model_validate({"kind": "suite"})


def test_the_shared_run_config_may_not_carry_a_prompt():
    with pytest.raises(BadRequestError) as caught:
        EvaluationRequest.model_validate(
            _suite_body(run_config={"model_id": "m", "user_prompt": "ignored?"})
        )
    assert caught.value.code == "suite_user_prompt"


def test_case_ids_must_be_unique():
    with pytest.raises(BadRequestError) as caught:
        EvaluationRequest.model_validate(
            _suite_body(cases=[{"id": "a", "input": "x"}, {"id": "a", "input": "y"}])
        )
    assert caught.value.code == "suite_duplicate_case"
    assert caught.value.detail == {"duplicates": ["a"]}


def test_too_many_cases_is_rejected_not_truncated():
    cases = [{"id": f"c{i}", "input": "x"} for i in range(MAX_SUITE_CASES + 1)]
    with pytest.raises(BadRequestError) as caught:
        EvaluationRequest.model_validate(_suite_body(cases=cases))
    assert caught.value.code == "suite_too_large"


def test_too_many_runs_is_rejected():
    cases = [{"id": f"c{i}", "input": "x"} for i in range(MAX_SUITE_RUNS // 10 + 1)]
    with pytest.raises(BadRequestError) as caught:
        EvaluationRequest.model_validate(_suite_body(cases=cases, repeats=10))
    assert caught.value.detail["max_runs"] == MAX_SUITE_RUNS


def test_exactly_the_run_limit_is_accepted():
    cases = [{"id": f"c{i}", "input": "x"} for i in range(MAX_SUITE_RUNS // 10)]
    request = EvaluationRequest.model_validate(_suite_body(cases=cases, repeats=10))
    assert request.planned_runs == MAX_SUITE_RUNS


@pytest.mark.parametrize("bad_id", ["", "has space", "#hash", "-leading-dash", "a" * 101])
def test_case_ids_are_restricted_to_a_safe_alphabet(bad_id):
    """`#` in particular is reserved: the judge's row names are `<case>#<index>`."""
    with pytest.raises(Exception):  # noqa: B017 - pydantic's ValidationError
        EvaluationRequest.model_validate(_suite_body(cases=[{"id": bad_id, "input": "x"}]))


def test_the_suite_is_stored_with_the_evaluation():
    request = EvaluationRequest.model_validate(
        _suite_body(cases=[{"id": "a", "input": "hi", "expected": "hello"}], repeats=2)
    )
    config = request.stored_config()
    assert config["kind"] == "suite"
    assert config["n"] == 2
    assert config["suite"]["cases"] == [
        {"id": "a", "input": "hi", "expected": "hello", "criteria": None}
    ]
    assert "user_prompt" not in config["suite"]["run_config"]
    assert config["run_config"] == config["suite"]["run_config"]


def test_each_case_runs_with_the_shared_config_and_its_own_input():
    request = EvaluationRequest.model_validate(
        _suite_body(run_config={"model_id": "m", "system_prompt": "sys", "toolset": "t"})
    )
    run = request.suite.run_config.for_case(request.suite.cases[0])
    assert (run.model_id, run.system_prompt, run.toolset) == ("m", "sys", "t")
    assert run.user_prompt == "hi"
    assert run.stream is False


def test_the_shared_config_keeps_run_validation():
    """A subclass, so RunRequest's own rules -- guardrails are Bedrock-only -- still hold."""
    with pytest.raises(BadRequestError) as caught:
        EvaluationRequest.model_validate(
            _suite_body(
                run_config={"model_id": "m", "provider": "openai", "guardrail": {"id": "g"}}
            )
        )
    assert caught.value.code == "guardrail_requires_bedrock"


# --------------------------------------------------------------------------- #
# The wire: what the cloud worker actually receives
# --------------------------------------------------------------------------- #


def test_a_suite_survives_the_round_trip_to_the_cloud_worker():
    """Validate, serialize exactly as the server does, parse exactly as the worker does.

    The first version failed this: `SuiteRunConfig.user_prompt` has a default,
    `model_dump()` includes defaults, so the payload carried `user_prompt: ""`
    -- and the worker's own validation, seeing the field set, rejected every
    cloud suite before it ran. The earlier cloud test missed it by handing the
    worker a hand-written dict instead of what the server sends.
    """
    import json

    from evalharness.evals import cloud
    from evalharness.worker import interfaces

    request = suite_request([REFUND, HOURS], repeats=2)
    request = request.model_copy(update={"execution": "cloud"})
    wire = json.loads(cloud.encode_payload(cloud.worker_payload("e" * 32, request)))

    assert "user_prompt" not in wire["request"]["suite"]["run_config"]
    received = interfaces.parse_request(wire["request"])
    assert received.kind == "suite"
    assert [case.id for case in received.suite.cases] == ["refund-window", "store-hours"]
    assert received.planned_runs == 4


def test_a_stored_suite_can_be_read_back_as_a_request():
    """The same round trip through the stored `config`: what is persisted must re-validate."""
    request = suite_request([REFUND])
    stored = request.stored_config()

    again = EvaluationRequest.model_validate(
        {"kind": "suite", "suite": stored["suite"], "rubric": stored["rubric"]}
    )

    assert again.suite.cases[0].id == "refund-window"


# --------------------------------------------------------------------------- #
# Every repeat counts
# --------------------------------------------------------------------------- #


class FlakyAnswers(AnswerBook):
    """Fails the first `failures` calls, then answers."""

    def __init__(self, answers: dict[str, str], failures: int) -> None:
        super().__init__(answers)
        self.failures = failures

    def __call__(self, request):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("model unavailable")
        return super().__call__(request)


async def test_a_repeat_that_failed_to_run_counts_as_zero(initialized_db):
    """The first version averaged only the repeats that answered, so one good
    answer out of ten attempts scored as a pass -- the exact flakiness repeats
    exist to expose. A repeat with no answer did not pass that time."""
    answers = FlakyAnswers({REFUND["input"]: "Within 30 days."}, failures=1)
    judge = RoutingJudge([], default=0.9)

    terminal, _ = await run_suite(suite_request([REFUND], repeats=2), answers, judge)

    case = terminal["result"]["cases"][0]
    assert sorted(case["scores"]) == [0.0, 0.9]
    assert case["score"] == pytest.approx(0.45)
    assert case["status"] == "failed"
    assert case["runs"] == {"total": 2, "succeeded": 1}


async def test_each_repeat_keeps_its_place_and_its_run(initialized_db):
    """A failed repeat must not renumber the ones after it: the UI labels and
    links runs by repeat, so the n-th entry has to be the n-th repeat."""
    answers = FlakyAnswers({REFUND["input"]: "Within 30 days."}, failures=1)
    judge = RoutingJudge([], default=0.9)

    terminal, _ = await run_suite(suite_request([REFUND], repeats=3), answers, judge)

    case = terminal["result"]["cases"][0]
    repeats = case["repeats"]
    assert len(repeats) == 3
    assert [repeat["score"] for repeat in repeats] == case["scores"]
    failed = [repeat for repeat in repeats if not repeat["ran"]]
    assert len(failed) == 1 and failed[0]["score"] == 0.0
    # The runs that answered are exactly the successful run ids, in order.
    assert [repeat["run_id"] for repeat in repeats if repeat["ran"]] == case["run_ids"]
    assert all(repeat["run_id"] for repeat in repeats if repeat["ran"])


async def test_nine_failed_repeats_and_one_good_one_is_not_a_pass(initialized_db):
    answers = FlakyAnswers({REFUND["input"]: "Within 30 days."}, failures=9)

    terminal, _ = await run_suite(
        suite_request([REFUND], repeats=10), answers, RoutingJudge([], default=0.95)
    )

    case = terminal["result"]["cases"][0]
    assert case["score"] == pytest.approx(0.095)
    assert case["passed"] is False


async def test_one_unjudged_repeat_makes_the_case_inconclusive(initialized_db, monkeypatch):
    """An answer the judge never scored is unknown -- neither a pass nor a zero.

    Counting it as zero would blame the application for the judge's failure;
    ignoring it would pass a case on half its evidence. So the case is
    `judge_error`, and the repeats that were scored are still reported.
    """
    original = grader._rows_for

    def first_repeat_errors(report, name):
        return [
            (0.0, "Evaluator error: throttled", row_name) if row_name.endswith("#0") else row
            for row in original(report, name)
            for row_name in [row[2]]
        ]

    monkeypatch.setattr(grader, "_rows_for", first_repeat_errors)
    answers = AnswerBook({REFUND["input"]: "Within 30 days."})

    terminal, _ = await run_suite(
        suite_request([REFUND], repeats=2), answers, RoutingJudge([], default=0.9)
    )

    case = terminal["result"]["cases"][0]
    assert case["status"] == "judge_error"
    assert case["passed"] is False
    assert case["score"] is None
    assert case["scores"] == [None, 0.9]
    assert case["error"] == {"code": "judge_error", "message": "throttled"}
