"""Suite-case assertions (``assert:``): the schema, each check, and scoring.

The unit tests drive :func:`nimbus.evals.assertions.evaluate` directly. The
tool-call checks are driven the way a suite really runs them: ``FakeModel``
``ToolUseStep`` scripts through the real run engine with the real
``fraud-detection`` tools, so the transcript they read is the one the runner
records. The scoring tests run whole suites through
``execute_evaluation_with_seam``, like ``test_evals_suite.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from nimbus.config import Settings
from nimbus.engine.fake_model import FakeModel, Text, ToolUseStep
from nimbus.errors import BadRequestError
from nimbus.evals import assertions, grader
from nimbus.evals import engine as evals_engine
from nimbus.evals.schemas import (
    MAX_SUITE_ASSERTION_CHECKS,
    MAX_SUITE_CASES,
    MAX_SUITE_RUNS,
    EvaluationRequest,
    SuiteCase,
)
from nimbus.store import db
from tests.test_evals_seam import Recorder, RecordingStore
from tests.test_evals_suite import AnswerBook, RoutingJudge, run_suite, suite_request

FREEZE_INPUT = {
    "account_id": "A1234",
    "transaction_ids": ["T1", "T2"],
    "reason": "velocity spike",
    "severity": "high",
    "freeze_duration": "temporary",
}
ALERT_INPUT = {
    "alert_type": "account_takeover",
    "priority": "high",
    "affected_accounts": ["A1234"],
    "description": "Several logins from new devices",
    "estimated_loss": 1200.0,
}


@pytest.fixture
def initialized_db(tmp_path):
    return db.init_db(str(tmp_path / "assertions.db"))


@pytest.fixture(autouse=True)
def instant_retries(monkeypatch):
    monkeypatch.setattr(evals_engine, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))


def checks(*entries: dict[str, Any]) -> list[Any]:
    """Validated assertion models, exactly as a suite file's `assert:` produces them."""
    return SuiteCase.model_validate({"id": "c", "input": "x", "assert": list(entries)}).assert_


def check(entry: dict[str, Any], output: str = "", **run: Any) -> dict[str, Any]:
    """The verdict of one check against one answer."""
    return assertions.evaluate(checks(entry), output=output, **run)[0]


# --------------------------------------------------------------------------- #
# The schema: syntax, shorthand, and rejection at submission
# --------------------------------------------------------------------------- #


def test_the_shorthand_and_the_canonical_form_are_the_same_check():
    short = checks(
        {"contains": "refund", "case_sensitive": False},
        {"json_valid": True},
        {"json_schema": {"type": "object", "required": ["a"]}},
        {"tool_called": {"name": "freeze_account", "times": 1}},
        {"tool_called": "create_fraud_alert"},
        {"tool_sequence": ["a", "b"], "mode": "exact"},
        {"max_tool_calls": 3},
        {"no_tool_errors": True},
    )
    canonical = checks(
        {"type": "contains", "value": "refund", "case_sensitive": False},
        {"type": "json_valid"},
        {"type": "json_schema", "schema": {"type": "object", "required": ["a"]}},
        {"type": "tool_called", "name": "freeze_account", "times": 1},
        {"type": "tool_called", "name": "create_fraud_alert"},
        {"type": "tool_sequence", "tools": ["a", "b"], "mode": "exact"},
        {"type": "max_tool_calls", "value": 3},
        {"type": "no_tool_errors"},
    )
    assert short == canonical


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"regex": "("}, "invalid regex '('"),
        ({"type": "regex", "pattern": "a", "flags": "q"}, "String should match pattern"),
        ({"json_schema": {"type": "nope"}}, "invalid JSON Schema"),
        ({"tool_called": {"name": "x", "args": {"a": [{"regex": "["}]}}}, "invalid regex '['"),
        ({"tool_called": {"name": "x", "times": 1, "min_times": 1}}, "not both"),
        ({"tool_called": {"name": "x", "min_times": 3, "max_times": 1}}, "greater than"),
        ({"bogus": 1}, "each assertion needs a `type`"),
        ({"contains": "a", "regex": "b"}, "found contains, regex"),
        ({"json_valid": False}, "takes no value"),
        ({"type": "contains"}, "Field required"),
        ({"type": "contains", "value": "a", "extra": 1}, "Extra inputs are not permitted"),
        ({"type": "teleport"}, "does not match any of the expected tags"),
        ({"max_length": -1}, "greater than or equal to 0"),
        ({"tool_sequence": []}, "at least 1 item"),
    ],
)
def test_a_bad_assertion_is_rejected_when_the_suite_is_submitted(entry, message):
    with pytest.raises(ValidationError) as caught:
        EvaluationRequest.model_validate(
            {
                "kind": "suite",
                "suite": {
                    "run_config": {"model_id": "m"},
                    "cases": [{"id": "a", "input": "hi", "assert": [entry]}],
                },
            }
        )
    assert message in str(caught.value)


def test_an_assertion_that_is_not_a_mapping_is_rejected():
    with pytest.raises(ValidationError):
        checks("contains refund")  # type: ignore[arg-type]


def test_assert_must_be_a_list():
    with pytest.raises(ValidationError, match="Input should be a valid list"):
        SuiteCase.model_validate({"id": "a", "input": "hi", "assert": {"contains": "x"}})


def test_a_case_that_skips_the_judge_needs_an_assertion():
    with pytest.raises(ValidationError, match="needs at least one `assert` check"):
        SuiteCase.model_validate({"id": "a", "input": "hi", "judge": False})


def test_a_case_may_have_at_most_ten_assertions():
    too_many = [{"contains": str(n)} for n in range(assertions.MAX_CASE_ASSERTIONS + 1)]
    with pytest.raises(ValidationError, match="at most 10 items"):
        SuiteCase.model_validate({"id": "a", "input": "hi", "assert": too_many})


def test_a_suite_may_record_a_bounded_number_of_assertion_verdicts():
    """Checks x repeats is bounded like runs are: rejected, never clamped."""
    cases = [{"id": f"c{n}", "input": "x", "assert": [{"contains": "a"}] * 10} for n in range(11)]
    body = {"kind": "suite", "suite": {"run_config": {"model_id": "m"}, "cases": cases}}
    with pytest.raises(BadRequestError) as caught:
        EvaluationRequest.model_validate({**body, "suite": {**body["suite"], "repeats": 10}})
    assert caught.value.code == "suite_too_large"
    assert caught.value.detail == {"checks": 1_100, "max_checks": MAX_SUITE_ASSERTION_CHECKS}
    # Exactly at the limit is fine.
    cases[-1]["assert"] = []
    assert (
        EvaluationRequest.model_validate(
            {**body, "suite": {**body["suite"], "cases": cases, "repeats": 10}}
        ).suite.planned_checks
        == MAX_SUITE_ASSERTION_CHECKS
    )


def test_a_case_without_assertions_serializes_exactly_as_before():
    case = SuiteCase.model_validate({"id": "a", "input": "hi"})
    assert case.model_dump() == {"id": "a", "input": "hi", "expected": None, "criteria": None}


def test_assertions_serialize_under_their_yaml_names_and_read_back():
    case = SuiteCase.model_validate(
        {
            "id": "a",
            "input": "hi",
            "judge": False,
            "assert": [{"json_schema": {"type": "object"}}, {"contains": "x"}],
        }
    )
    dumped = case.model_dump(mode="json")
    assert dumped["judge"] is False
    assert dumped["assert"] == [
        {"type": "json_schema", "schema": {"type": "object"}},
        {"type": "contains", "value": "x", "case_sensitive": True},
    ]
    assert SuiteCase.model_validate(dumped) == case


def test_assertions_survive_the_round_trip_to_the_cloud_worker():
    from nimbus.evals import cloud
    from nimbus.worker import interfaces

    request = EvaluationRequest.model_validate(
        {
            "kind": "suite",
            "execution": "cloud",
            "suite": {
                "run_config": {"model_id": "m"},
                "cases": [{"id": "a", "input": "hi", "judge": False, "assert": [{"regex": "^h"}]}],
            },
        }
    )
    wire = json.loads(cloud.encode_payload(cloud.worker_payload("e" * 32, request)))

    received = interfaces.parse_request(wire["request"])
    assert received.suite.cases[0] == request.suite.cases[0]
    # And through the stored config, as `GET /evaluations/{id}` returns it.
    stored = request.stored_config()["suite"]
    assert stored["cases"][0]["assert"] == [{"type": "regex", "pattern": "^h", "flags": ""}]


# --------------------------------------------------------------------------- #
# Output checks
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("entry", "output", "passed", "detail"),
    [
        ({"contains": "30 days"}, "Within 30 days.", True, 'found "30 days" at char 7'),
        ({"contains": "30 Days"}, "Within 30 days.", False, 'output does not contain "30 Days"'),
        ({"contains": "30 DAYS", "case_sensitive": False}, "within 30 days", True, None),
        ({"not_contains": "refund"}, "Store credit only.", True, '"refund" not found'),
        ({"not_contains": "refund"}, "A refund!", False, 'output contains "refund" at char 2'),
        ({"not_contains": "REFUND", "case_sensitive": False}, "A refund!", False, None),
        ({"regex": r"\b10\s?am\b"}, "We open at 10am.", True, 'matched "10am" at char 11'),
        ({"regex": r"^10am$"}, "We open at 10am.", False, r'no match for "/^10am$/"'),
        ({"type": "regex", "pattern": "^OPEN", "flags": "im"}, "x\nopen", True, None),
        ({"equals": "Yes"}, "  Yes\n", True, "output equals the expected value"),
        ({"equals": "Yes"}, "Yes, sure", False, 'expected "Yes", got "Yes, sure"'),
        ({"equals": "yes", "case_sensitive": False}, "YES", True, None),
        ({"equals": "Yes", "strip": False}, "Yes\n", False, None),
        ({"json_valid": True}, ' {"a": 1} ', True, "valid JSON"),
        ({"json_valid": True}, "{a: 1}", False, "not valid JSON: Expecting property name"),
        ({"max_length": 5}, "hello", True, "5 chars <= 5"),
        ({"max_length": 4}, "hello", False, "5 chars > 4"),
        ({"max_length": 2, "unit": "words"}, "one two three", False, "3 words > 2"),
        ({"min_length": 3, "unit": "words"}, "one two three", True, "3 words >= 3"),
        ({"min_length": 10}, "short", False, "5 chars < 10"),
    ],
)
def test_output_checks(entry, output, passed, detail):
    verdict = check(entry, output)

    assert verdict["type"] == (entry.get("type") or next(iter(entry)))
    assert verdict["passed"] is passed
    if detail is not None:
        assert verdict["detail"].startswith(detail), verdict["detail"]


SCHEMA = {
    "type": "object",
    "required": ["decision", "amount"],
    "properties": {
        "decision": {"enum": ["approve", "deny"]},
        "amount": {"type": "number"},
        "items": {"type": "array", "items": {"type": "string"}},
    },
}


@pytest.mark.parametrize(
    ("output", "passed", "detail"),
    [
        ('{"decision": "deny", "amount": 12.5}', True, "matches the schema"),
        ('{"decision": "maybe", "amount": 1}', False, "at $.decision: 'maybe' is not one of"),
        ('{"decision": "deny"}', False, "at $: 'amount' is a required property"),
        ('{"decision": "deny", "amount": 1, "items": ["a", 2]}', False, "at $.items[1]: 2 is"),
        ("Sure! Here it is.", False, "not valid JSON"),
    ],
)
def test_json_schema(output, passed, detail):
    verdict = check({"json_schema": SCHEMA}, output)

    assert verdict["passed"] is passed
    assert verdict["detail"].startswith(detail), verdict["detail"]


def test_a_schema_names_its_own_draft():
    draft7 = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "integer"}
    assert check({"json_schema": draft7}, "3")["passed"] is True
    assert check({"json_schema": draft7}, "3.5")["passed"] is False


def test_max_latency_reads_the_runs_wall_clock():
    assert check({"max_latency_ms": 500}, duration_ms=500)["detail"] == "took 500 ms <= 500"
    failed = check({"max_latency_ms": 500}, duration_ms=501)
    assert (failed["passed"], failed["detail"]) == (False, "took 501 ms > 500")


def test_long_values_are_elided_in_the_detail():
    verdict = check({"contains": "x" * 500}, "nothing")

    assert len(verdict["detail"]) < 120
    assert "…" in verdict["detail"]


def test_evaluate_keeps_the_order_of_the_list():
    found = assertions.evaluate(
        checks({"contains": "a"}, {"max_tool_calls": 0}, {"json_valid": True}), output="a"
    )
    assert [v["type"] for v in found] == ["contains", "max_tool_calls", "json_valid"]
    assert [v["passed"] for v in found] == [True, True, False]


# --------------------------------------------------------------------------- #
# Partial argument matching
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("expected", "actual", "matches"),
    [
        ({"a": 1}, {"a": 1, "b": 2}, True),
        ({"a": 1}, {"a": 2}, False),
        ({"a": 1}, {"b": 1}, False),
        ({"a": {"b": "x"}}, {"a": {"b": "x", "c": 1}}, True),
        ({"a": ["x", "y"]}, {"a": ["x", "y"]}, True),
        ({"a": ["x"]}, {"a": ["x", "y"]}, False),
        ({"a": ["x"]}, {"a": "x"}, False),
        ({"a": {"regex": "^A\\d+$"}}, {"a": "A1234"}, True),
        ({"a": {"regex": "^A\\d+$"}}, {"a": "B1234"}, False),
        ({"a": {"regex": "1"}}, {"a": 1}, False),
        ({"a": [{"regex": "^T"}]}, {"a": ["T1"]}, True),
        ({"a": True}, {"a": 1}, False),
        ({"a": 1}, {"a": True}, False),
        ({"a": True}, {"a": True}, True),
        ({"a": 1}, {"a": 1.0}, True),
        ({"a": {"b": 1}}, {"a": "b"}, False),
        ({"a": 1}, "not a mapping", False),
    ],
)
def test_args_match_partially(expected, actual, matches):
    assert assertions.args_match(expected, actual) is matches


# --------------------------------------------------------------------------- #
# Tool-call checks on hand-written transcripts: the edges
# --------------------------------------------------------------------------- #


def call(name: str, error: Any = None, **tool_input: Any) -> dict[str, Any]:
    return {"tool_use_id": "t", "name": name, "input": tool_input, "error": error}


TRANSCRIPT = [
    call("github_search_issues", q="bug"),
    call("freeze_account", account_id="A1"),
    call("github_get_issue", number=7),
    call("freeze_account", account_id="A2"),
]


@pytest.mark.parametrize(
    ("entry", "passed", "detail"),
    [
        (
            {"tool_called": "freeze_account"},
            True,
            "freeze_account called 2 times (expected at least 1)",
        ),
        ({"tool_called": {"name": "freeze_account", "times": 2}}, True, None),
        ({"tool_called": {"name": "freeze_account", "times": 1}}, False, None),
        (
            {"tool_called": {"name": "freeze_account", "max_times": 1}},
            False,
            "(expected at most 1)",
        ),
        (
            {"tool_called": {"name": "freeze_account", "min_times": 3}},
            False,
            "(expected at least 3)",
        ),
        (
            {"tool_called": {"name": "github_*", "min_times": 1, "max_times": 2}},
            True,
            "(expected 1-2)",
        ),
        (
            {"tool_called": {"name": "freeze_account", "args": {"account_id": "A2"}, "times": 1}},
            True,
            "freeze_account called 2 times, 1 with matching args (expected exactly 1)",
        ),
        ({"tool_called": {"name": "delete_*", "times": 0}}, True, "delete_* called 0 times"),
        ({"tool_called": "create_fraud_alert"}, False, "create_fraud_alert called 0 times"),
        ({"tool_not_called": "delete_*"}, True, "delete_* was not called"),
        (
            {"tool_not_called": "github_*"},
            False,
            "github_* was called: github_get_issue x1, github_search_issues x1",
        ),
        ({"tool_sequence": ["github_*", "freeze_account"]}, True, None),
        ({"tool_sequence": ["freeze_account", "github_search_issues"]}, False, None),
        ({"tool_sequence": ["freeze_account", "freeze_account"]}, True, None),
        ({"tool_sequence": ["freeze_account"] * 3}, False, None),
        (
            {"tool_sequence": ["github_search_issues", "freeze_account"], "mode": "exact"},
            False,
            "expected github_search_issues -> freeze_account (exact), got github_search_issues, "
            "freeze_account, github_get_issue, freeze_account",
        ),
        (
            {
                "tool_sequence": ["github_*", "freeze_account", "github_*", "freeze_*"],
                "mode": "exact",
            },
            True,
            None,
        ),
        ({"max_tool_calls": 4}, True, "4 tool calls <= 4"),
        ({"max_tool_calls": 3}, False, "4 tool calls > 3"),
        ({"no_tool_errors": True}, True, "4 tool calls, none errored"),
    ],
)
def test_tool_checks(entry, passed, detail):
    verdict = check(entry, tool_transcript=TRANSCRIPT)

    assert verdict["passed"] is passed, verdict["detail"]
    if detail is not None:
        assert detail in verdict["detail"]


def test_tool_errors_name_the_first_failure():
    transcript = [
        call("a"),
        call("b", error={"type": "tool_error", "message": "account not found"}),
        call("c", error="plain string error"),
    ]
    verdict = check({"no_tool_errors": True}, tool_transcript=transcript)

    assert verdict["passed"] is False
    assert verdict["detail"] == "2 of 3 tool calls errored; first: b: account not found"
    plain = check({"no_tool_errors": True}, tool_transcript=[call("c", error="boom")])
    assert plain["detail"].endswith("first: c: boom")


def test_a_run_without_tools_has_an_empty_sequence():
    verdict = check({"tool_sequence": ["a"]}, tool_transcript=None)
    assert verdict["passed"] is False
    assert verdict["detail"].endswith("got no tools")


def test_malformed_transcript_records_are_nameless_calls_not_crashes():
    verdict = check({"max_tool_calls": 0}, tool_transcript=["garbage"])
    assert verdict["passed"] is False
    assert check({"tool_called": "x"}, tool_transcript=["garbage"])["passed"] is False
    assert check({"no_tool_errors": True}, tool_transcript=["garbage"])["passed"] is True


# --------------------------------------------------------------------------- #
# Tool-call checks through the real runner: FakeModel scripts, real tools
# --------------------------------------------------------------------------- #


class ScriptBook:
    """The agent under test: a scripted tool-using answer per case input."""

    def __init__(self, scripts: dict[str, list[Any]]) -> None:
        self.scripts = scripts

    def __call__(self, request) -> FakeModel:
        return FakeModel(script=list(self.scripts[request.user_prompt]))


def no_judge(_model_id, _provider="bedrock"):
    raise AssertionError("an assertion-only suite must never build a judge")


async def run_agent_suite(
    cases: list[dict[str, Any]], scripts: dict[str, list[Any]], judge: Any = no_judge, **suite
) -> dict[str, Any]:
    request = EvaluationRequest.model_validate(
        {
            "kind": "suite",
            "suite": {
                "name": "fraud-agent",
                "run_config": {"model_id": "fake.model", "toolset": "fraud-detection"},
                "cases": cases,
                **suite,
            },
        }
    )
    recorder = Recorder()
    terminal = await evals_engine.execute_evaluation_with_seam(
        request,
        recorder.emit,
        RecordingStore("eval-agent"),
        recorder.cancelled,
        deps=evals_engine.EvalDeps(
            settings=Settings(), model_factory=ScriptBook(scripts), judge_factory=judge
        ),
    )
    assert terminal["status"] == "completed", terminal
    return {case["id"]: case for case in terminal["result"]["cases"]} | {
        "_result": terminal["result"]
    }


def verdicts_of(case: dict[str, Any], repeat: int = 0) -> list[tuple[str, bool]]:
    return [(v["type"], v["passed"]) for v in case["repeats"][repeat]["assertions"]]


FREEZE_THEN_ALERT = [
    ToolUseStep("freeze_account", FREEZE_INPUT),
    Text("Frozen. Raising an alert."),
    ToolUseStep("create_fraud_alert", ALERT_INPUT),
    Text("Frozen A1234 and raised an alert."),
]
BROKEN_FREEZE = [ToolUseStep("freeze_account", {"account_id": "A1234"}), Text("Could not freeze.")]
JUST_TALK = [Text("I would freeze the account.")]


async def test_tool_assertions_read_the_transcript_the_runner_recorded(initialized_db):
    tool_checks = [
        {
            "tool_called": {
                "name": "freeze_account",
                "args": {"account_id": "A1234", "transaction_ids": [{"regex": "^T"}, "T2"]},
                "times": 1,
            }
        },
        {"tool_not_called": "update_*"},
        {"tool_sequence": ["freeze_account", "create_fraud_alert"], "mode": "exact"},
        {"max_tool_calls": 2},
        {"no_tool_errors": True},
    ]
    cases = [
        {"id": "freeze", "input": "freeze", "judge": False, "assert": tool_checks},
        {"id": "broken", "input": "broken", "judge": False, "assert": tool_checks},
        {"id": "talk", "input": "talk", "judge": False, "assert": tool_checks},
    ]
    scripts = {"freeze": FREEZE_THEN_ALERT, "broken": BROKEN_FREEZE, "talk": JUST_TALK}

    by_id = await run_agent_suite(cases, scripts)

    assert verdicts_of(by_id["freeze"]) == [
        ("tool_called", True),
        ("tool_not_called", True),
        ("tool_sequence", True),
        ("max_tool_calls", True),
        ("no_tool_errors", True),
    ]
    assert by_id["freeze"]["status"] == "passed"
    assert by_id["freeze"]["score"] == 1.0

    # The real tool rejected the incomplete input: the call happened, with an
    # error, and its args do not match.
    assert verdicts_of(by_id["broken"]) == [
        ("tool_called", False),
        ("tool_not_called", True),
        ("tool_sequence", False),
        ("max_tool_calls", True),
        ("no_tool_errors", False),
    ]
    broken = by_id["broken"]["repeats"][0]["assertions"]
    assert broken[0]["detail"] == (
        "freeze_account called once, 0 with matching args (expected exactly 1)"
    )
    assert broken[4]["detail"].startswith("1 of 1 tool calls errored; first: freeze_account:")

    assert verdicts_of(by_id["talk"])[0] == ("tool_called", False)
    assert by_id["talk"]["repeats"][0]["assertions"][2]["detail"].endswith("got no tools")
    assert by_id["talk"]["status"] == "failed"
    assert by_id["talk"]["score"] == 0.0


async def test_every_repeat_is_checked_on_its_own(initialized_db):
    class Alternating(ScriptBook):
        calls = 0

        def __call__(self, request):
            Alternating.calls += 1
            return FakeModel(script=list(FREEZE_THEN_ALERT if self.calls % 2 else JUST_TALK))

    request = suite_request(
        [{"id": "flaky", "input": "go", "judge": False, "assert": [{"tool_called": "freeze_*"}]}],
        repeats=2,
    )
    recorder = Recorder()
    terminal = await evals_engine.execute_evaluation_with_seam(
        request.model_copy(
            update={
                "suite": request.suite.model_copy(
                    update={
                        "run_config": request.suite.run_config.model_copy(
                            update={"toolset": "fraud-detection"}
                        )
                    }
                )
            }
        ),
        recorder.emit,
        RecordingStore("eval-flaky"),
        deps=evals_engine.EvalDeps(
            settings=Settings(), model_factory=Alternating({}), judge_factory=no_judge
        ),
    )

    case = terminal["result"]["cases"][0]
    assert [r["assertions_passed"] for r in case["repeats"]] == [True, False]
    assert case["scores"] == [1.0, 0.0]
    assert case["score"] == 0.5
    assert case["status"] == "failed"
    assert case["assertions"] == {"total": 2, "passed": 1, "failed": 1}


# --------------------------------------------------------------------------- #
# Scoring: assertions and the judge together
# --------------------------------------------------------------------------- #


REFUND = {
    "id": "refund-window",
    "input": "Can I return shoes after 45 days?",
    "expected": "No. Returns are accepted within 30 days of purchase.",
}


async def test_a_failed_assertion_fails_a_case_the_judge_liked(initialized_db):
    answers = AnswerBook({REFUND["input"]: "No, returns are accepted within 30 days."})
    judge = RoutingJudge([], default=0.95)
    case = {**REFUND, "assert": [{"contains": "30 days"}, {"max_length": 5, "unit": "words"}]}

    terminal, _ = await run_suite(suite_request([case]), answers, judge)

    result = terminal["result"]
    entry = result["cases"][0]
    assert entry["status"] == "failed"
    assert entry["passed"] is False
    assert entry["score"] == pytest.approx(0.95)  # the judge's score stands...
    assert entry["judged"] is True
    assert entry["assertions"] == {"total": 2, "passed": 1, "failed": 1}
    repeat = entry["repeats"][0]
    assert repeat["assertions_passed"] is False
    assert repeat["assertions"] == [
        {"type": "contains", "passed": True, "detail": 'found "30 days" at char 32'},
        {"type": "max_length", "passed": False, "detail": "7 words > 5"},
    ]
    assert result["metrics"]["assertions_total"] == 2
    assert result["metrics"]["assertions_failed"] == 1
    assert "failed: refund-window" in result["reasoning"]


async def test_a_case_passes_only_when_its_assertions_and_its_judge_both_do(initialized_db):
    answers = AnswerBook({REFUND["input"]: "No, returns are accepted within 30 days."})
    passing = {**REFUND, "assert": [{"contains": "30 days"}, {"not_contains": "refund"}]}

    terminal, _ = await run_suite(suite_request([passing]), answers, RoutingJudge([], default=0.9))
    assert terminal["result"]["cases"][0]["status"] == "passed"

    terminal, _ = await run_suite(suite_request([passing]), answers, RoutingJudge([], default=0.3))
    entry = terminal["result"]["cases"][0]
    assert entry["status"] == "failed"
    assert entry["assertions"]["failed"] == 0


async def test_the_judge_only_sees_the_cases_that_want_it(initialized_db):
    only_checks = {
        "id": "format",
        "input": "Reply with JSON",
        "judge": False,
        "assert": [{"json_valid": True}],
    }
    answers = AnswerBook({REFUND["input"]: "Within 30 days.", only_checks["input"]: '{"ok": true}'})
    judge = RoutingJudge([], default=0.9)

    terminal, _ = await run_suite(suite_request([REFUND, only_checks]), answers, judge)

    assert len(judge.seen) == 1
    assert "Reply with JSON" not in judge.seen[0]
    by_id = {case["id"]: case for case in terminal["result"]["cases"]}
    assert by_id["format"]["judged"] is False
    assert by_id["format"]["reasoning"] is None
    assert by_id["format"]["status"] == "passed"
    assert by_id["format"]["score"] == 1.0
    assert by_id["refund-window"]["status"] == "passed"
    # A case without assertions carries none of the new fields.
    assert "assertions" not in by_id["refund-window"]
    assert "judged" not in by_id["refund-window"]
    assert "assertions" not in by_id["refund-window"]["repeats"][0]


async def test_a_suite_of_assertion_only_cases_runs_without_a_judge(initialized_db):
    case = {"id": "a", "input": "hi", "judge": False, "assert": [{"contains": "[fake"}]}
    answers = AnswerBook({"hi": "[fake] hello"})
    recorder = Recorder()

    terminal = await evals_engine.execute_evaluation_with_seam(
        suite_request([case]),
        recorder.emit,
        RecordingStore("eval-nojudge"),
        deps=evals_engine.EvalDeps(
            settings=Settings(), model_factory=answers, judge_factory=no_judge
        ),
    )

    result = terminal["result"]
    assert "judge_error" not in result
    assert result["cases"][0]["status"] == "passed"
    assert result["grade"] == "A"
    assert result["metrics"]["pass_rate"] == 1.0


async def test_a_failed_assertion_is_a_failure_even_when_the_judge_is_down(initialized_db):
    """What the judge would have said cannot rescue a case a check already failed."""
    answers = AnswerBook({REFUND["input"]: "Maybe."})

    def broken_judge(_model_id, _provider="bedrock"):
        raise RuntimeError("no judge credentials")

    cases = [
        {**REFUND, "assert": [{"contains": "30 days"}]},
        {"id": "ok", "input": "ok?", "assert": [{"contains": "know"}]},
    ]
    recorder = Recorder()
    terminal = await evals_engine.execute_evaluation_with_seam(
        suite_request(cases),
        recorder.emit,
        RecordingStore("eval-judge-down"),
        deps=evals_engine.EvalDeps(
            settings=Settings(), model_factory=answers, judge_factory=broken_judge
        ),
    )

    by_id = {case["id"]: case for case in terminal["result"]["cases"]}
    assert by_id["refund-window"]["status"] == "failed"
    assert by_id["refund-window"]["score"] is None
    assert by_id["refund-window"]["error"] == {
        "code": "judge_error",
        "message": "no judge credentials",
    }
    # Assertions passed, judge missing: still inconclusive, as before.
    assert by_id["ok"]["status"] == "judge_error"


async def test_a_repeat_that_did_not_run_has_no_assertion_verdicts(initialized_db):
    class FailsOnce(AnswerBook):
        failures = 1

        def __call__(self, request):
            if self.failures:
                self.failures -= 1
                raise RuntimeError("model unavailable")
            return super().__call__(request)

    case = {"id": "a", "input": "hi", "judge": False, "assert": [{"contains": "hello"}]}

    terminal, _ = await run_suite(
        suite_request([case], repeats=4), FailsOnce({"hi": "hello"}), RoutingJudge([])
    )

    entry = terminal["result"]["cases"][0]
    assert [r["assertions_passed"] for r in entry["repeats"]] == [None, True, True, True]
    assert entry["repeats"][0]["assertions"] is None
    assert entry["scores"] == [0.0, 1.0, 1.0, 1.0]
    assert entry["assertions"] == {"total": 3, "passed": 3, "failed": 0}
    # 0.75 over the threshold, and every repeat that answered passed its checks.
    assert entry["status"] == "passed"


async def test_a_case_whose_every_run_failed_is_still_an_error(initialized_db):
    class Broken(AnswerBook):
        def __call__(self, request):
            raise RuntimeError("model unavailable")

    case = {"id": "a", "input": "hi", "judge": False, "assert": [{"contains": "x"}]}
    terminal, _ = await run_suite(suite_request([case]), Broken({}), RoutingJudge([]))

    assert terminal["status"] == "error"
    entry = terminal["result"]["cases"][0]
    assert entry["status"] == "error"
    assert entry["assertions"] == {"total": 0, "passed": 0, "failed": 0}


async def test_max_latency_uses_the_repeats_duration(initialized_db):
    case = {"id": "a", "input": "hi", "judge": False, "assert": [{"max_latency_ms": 60_000}]}
    terminal, _ = await run_suite(suite_request([case]), AnswerBook({}), RoutingJudge([]))

    verdict = terminal["result"]["cases"][0]["repeats"][0]["assertions"][0]
    assert verdict["passed"] is True
    assert verdict["detail"].startswith("took ") and verdict["detail"].endswith(" ms <= 60000")


# --------------------------------------------------------------------------- #
# The result's byte budget
# --------------------------------------------------------------------------- #


def test_a_long_detail_is_clipped_where_it_is_recorded():
    outcome = evals_engine.RunOutcome(
        index=0, run_id="r", status="completed", case_id="c", output="語" * 5_000
    )
    entry = evals_engine._suite_case_result(
        "c",
        [outcome],
        grader.CaseVerdict(),
        None,
        0.7,
        checks=checks({"equals": "語" * 5_000 + "x"}),
        judged=False,
    )

    detail = entry["repeats"][0]["assertions"][0]["detail"]
    assert len(json.dumps(detail)) - 2 <= evals_engine.MAX_ASSERTION_DETAIL_BYTES


@pytest.mark.parametrize("char", ["x", "語"])
def test_the_largest_suite_result_with_assertions_fits_its_byte_budget(char):
    """Every limit at its maximum, the assertion budget spent, every detail huge."""
    repeats = MAX_SUITE_RUNS // MAX_SUITE_CASES
    per_case = MAX_SUITE_ASSERTION_CHECKS // MAX_SUITE_RUNS
    ids = [f"{index:03d}" + "x" * 97 for index in range(MAX_SUITE_CASES)]
    long_name = char * 150
    request = EvaluationRequest.model_validate(
        {
            "kind": "suite",
            "suite": {
                "run_config": {"model_id": "m"},
                "repeats": repeats,
                "cases": [
                    {
                        "id": case_id,
                        "input": "hi",
                        "assert": [{"tool_not_called": long_name + "*"}] * per_case,
                    }
                    for case_id in ids
                ],
            },
        }
    )
    outcomes, verdicts = [], {}
    for number, case_id in enumerate(ids):
        verdict = grader.CaseVerdict()
        for repeat in range(repeats):
            index = number * repeats + repeat
            outcomes.append(
                evals_engine.RunOutcome(
                    index=index,
                    run_id=f"{index:032x}",
                    status="completed",
                    case_id=case_id,
                    tool_transcript=[{"name": long_name + "!"}],
                )
            )
            verdict.scores[index] = 0.9
            verdict.reasons.append(char * 10_000)
        verdicts[case_id] = verdict

    result = evals_engine._build_suite_result(
        outcomes, grader.SuiteJudgement(verdicts=verdicts, error=char * 10_000), request
    )

    assert len(json.dumps(result, default=str)) <= evals_engine.MAX_RESULT_BYTES
    assert result["truncated"] is True
    # The verdicts survive, even where their prose did not.
    assert result["metrics"]["assertions_total"] == MAX_SUITE_ASSERTION_CHECKS
    assert result["metrics"]["assertions_failed"] == MAX_SUITE_ASSERTION_CHECKS
    assert all(case["status"] == "failed" for case in result["cases"])
    first = result["cases"][0]["repeats"][0]["assertions"][0]
    assert (first["type"], first["passed"]) == ("tool_not_called", False)
