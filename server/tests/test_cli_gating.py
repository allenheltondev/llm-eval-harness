"""The gate and the JUnit report, as pure functions of an evaluation result.

The CLI tests (``test_cli.py``, ``test_cli_remote.py``) prove the flags reach
these functions and the exit codes come out right; these pin down the rules
themselves, including result shapes a fake judge never produces.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from nimbus.cli.gating import EXIT_GATE_FAILED, Gate, check, failed_cases, junit_xml

MISSING_30_DAYS = {"type": "contains", "passed": False, "detail": "missing '30 days'"}


def _case(case_id: str, status: str, score: float | None = 0.9, **extra) -> dict:
    return {
        "id": case_id,
        "status": status,
        "passed": status == "passed",
        "score": score,
        "reasoning": None,
        "error": None,
        **extra,
    }


def _result(*cases: dict, score: int | None = 80) -> dict:
    passed = sum(1 for case in cases if case["passed"])
    return {
        "score": score,
        "metrics": {"pass_rate": passed / len(cases) if cases else None},
        "suite": {"name": "support"},
        "cases": list(cases),
    }


def test_exit_code_three_is_distinct_from_every_other():
    assert EXIT_GATE_FAILED == 3


class TestCheck:
    def test_no_gate_is_inactive_and_passes_anything(self):
        assert not Gate().active
        assert check(Gate(), None) == []

    @pytest.mark.parametrize(
        ("gate", "active"),
        [
            (Gate(fail_under=0), True),
            (Gate(fail_on_case_failure=True), True),
            (Gate(min_pass_rate=0.0), True),
        ],
    )
    def test_any_part_makes_it_active(self, gate, active):
        assert gate.active is active

    @pytest.mark.parametrize(
        ("score", "bar", "fails"),
        [(80, 80, False), (80, 79.5, False), (80, 80.5, True), (0, 0, False)],
    )
    def test_fail_under_is_strictly_below(self, score, bar, fails):
        reasons = check(Gate(fail_under=bar), _result(_case("a", "passed"), score=score))
        assert bool(reasons) is fails
        if fails:
            assert reasons == [f"score {score:g} is under --fail-under {bar:g}"]

    def test_no_score_cannot_clear_fail_under(self):
        reasons = check(Gate(fail_under=0), {"score": None})
        assert reasons == ["no overall score to compare with --fail-under 0"]

    def test_any_case_not_passed_fails_fail_on_case_failure(self):
        result = _result(
            _case("a", "passed"),
            _case("b", "failed"),
            _case("c", "error"),
            _case("d", "judge_error"),
        )
        assert failed_cases(result) == ["b", "c", "d"]
        assert check(Gate(fail_on_case_failure=True), result) == ["3 case(s) did not pass: b, c, d"]
        assert check(Gate(fail_on_case_failure=True), _result(_case("a", "passed"))) == []

    @pytest.mark.parametrize(
        ("bar", "fails"), [(0.5, False), (0.51, True), (1.0, True), (0, False)]
    )
    def test_min_pass_rate(self, bar, fails):
        result = _result(_case("a", "passed"), _case("b", "failed"))
        reasons = check(Gate(min_pass_rate=bar), result)
        assert bool(reasons) is fails
        if fails:
            assert reasons == [f"pass rate 50% is under the suite's bar of {bar:.0%}"]

    def test_no_pass_rate_cannot_clear_min_pass_rate(self):
        assert check(Gate(min_pass_rate=0.0), {"metrics": {}}) == [
            "no pass rate to compare with the suite's min_pass_rate"
        ]

    def test_every_miss_is_reported(self):
        result = _result(_case("a", "failed"), score=10)
        gate = Gate(fail_under=50, fail_on_case_failure=True, min_pass_rate=1.0)
        assert len(check(gate, result)) == 3


def _parse(xml: str) -> ET.Element:
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    root = ET.fromstring(xml)
    assert root.tag == "testsuites"
    return root


def _assert_common_schema(root: ET.Element) -> None:
    """What the common JUnit schema (and GitHub/GitLab/Jenkins reporters) require."""
    for testsuite in root.findall("testsuite"):
        for attribute in ("name", "tests", "failures", "errors", "skipped", "time"):
            assert attribute in testsuite.attrib, attribute
        testcases = testsuite.findall("testcase")
        assert int(testsuite.get("tests")) == len(testcases)
        assert int(testsuite.get("failures")) == len(testsuite.findall("testcase/failure"))
        assert int(testsuite.get("errors")) == len(testsuite.findall("testcase/error"))
        for testcase in testcases:
            assert set(testcase.attrib) == {"name", "classname", "time"}
            float(testcase.get("time"))
            for child in testcase:
                assert child.tag in ("failure", "error")
                assert "message" in child.attrib and "type" in child.attrib


class TestJUnit:
    def test_one_testcase_per_case_with_failures_and_errors(self):
        terminal = {
            "evaluation_id": "ev-1",
            "status": "completed",
            "result": _result(
                _case("ok", "passed"),
                _case(
                    "wrong",
                    "failed",
                    score=0.4,
                    reasoning="Said 60 days.",
                    # Exactly as PR #33's engine records it: a tally on the
                    # case, and one verdict per check on every repeat.
                    judged=True,
                    assertions={"total": 9, "passed": 5, "failed": 4},
                    repeats=[
                        {
                            "assertions": [
                                MISSING_30_DAYS,
                                {"type": "regex", "passed": True, "detail": "matched"},
                                {"type": "tool_called", "passed": True, "detail": "called once"},
                            ],
                            "assertions_passed": False,
                        },
                        {"assertions": None, "assertions_passed": None},  # did not run
                        {
                            "assertions": [
                                MISSING_30_DAYS,
                                {"type": "regex", "passed": False, "detail": "no match"},
                                {"type": "tool_called", "passed": False},
                            ],
                            "assertions_passed": False,
                        },
                    ],
                ),
                _case("down", "error", score=None, error={"code": "x", "message": "throttled"}),
                _case("unjudged", "judge_error", score=None, error=None),
            ),
        }

        root = _parse(junit_xml(terminal))

        _assert_common_schema(root)
        [suite] = root.findall("testsuite")
        assert suite.get("name") == "support"
        assert (suite.get("tests"), suite.get("failures"), suite.get("errors")) == ("4", "1", "2")
        assert root.get("tests") == "4"
        cases = {case.get("name"): case for case in suite.findall("testcase")}
        assert list(cases) == ["ok", "wrong", "down", "unjudged"]
        assert {case.get("classname") for case in cases.values()} == {"nimbus.support"}
        assert list(cases["ok"]) == []
        failure = cases["wrong"].find("failure")
        assert failure.get("message") == "case wrong failed"
        assert failure.text.splitlines() == [
            "score 0.4",
            "assertion failed: contains (repeats 1, 3 of 3): missing '30 days'",
            "assertion failed: regex (repeat 3 of 3): no match",
            "assertion failed: tool_called (repeat 3 of 3)",
            "Said 60 days.",
        ]
        assert cases["down"].find("error").get("message") == "throttled"
        assert cases["down"].find("error").get("type") == "error"
        assert cases["unjudged"].find("error").get("message") == "case unjudged: judge_error"
        assert suite.find("properties/property").attrib == {
            "name": "evaluation_id",
            "value": "ev-1",
        }

    def test_a_failure_with_nothing_to_say_has_an_empty_body(self):
        terminal = {"status": "completed", "result": _result(_case("a", "failed", score=None))}
        root = _parse(junit_xml(terminal, name="named"))
        assert root.find("testsuite").get("name") == "named"
        assert root.find("testsuite").find("properties") is None
        assert not root.find(".//failure").text

    @pytest.mark.parametrize(
        ("error", "message"),
        [
            ({"message": "the stack broke"}, "the stack broke"),
            ("plain words", "plain words"),
            (None, "evaluation error"),
        ],
    )
    def test_an_evaluation_that_errored_is_one_errored_testcase(self, error, message):
        root = _parse(junit_xml({"status": "error", "result": None, "error": error}))
        _assert_common_schema(root)
        [case] = root.findall("testsuite/testcase")
        assert case.get("name") == "evaluation"
        assert case.find("error").get("message") == message
        assert root.find("testsuite").get("name") == "nimbus"

    def test_a_cancelled_evaluation_is_an_error_too(self):
        root = _parse(junit_xml({"status": "cancelled", "result": None}))
        assert root.find(".//error").attrib == {
            "message": "evaluation cancelled",
            "type": "cancelled",
        }

    def test_a_status_less_terminal_says_it_did_not_finish(self):
        root = _parse(junit_xml({}))
        assert root.find(".//error").get("message") == "evaluation did not finish"

    def test_a_completed_evaluation_without_cases_is_one_passing_testcase(self):
        root = _parse(junit_xml({"status": "completed", "result": {"score": 90}}))
        _assert_common_schema(root)
        [case] = root.findall("testsuite/testcase")
        assert list(case) == []


def test_an_unset_min_pass_rate_is_not_sent_so_older_stacks_accept_the_suite():
    from nimbus.evals.schemas import Suite

    base = {"run_config": {"model_id": "m"}, "cases": [{"id": "c1", "input": "hi"}]}
    unset = Suite.model_validate(base)
    assert "min_pass_rate" not in unset.model_dump(mode="json")
    assert "min_pass_rate" not in unset.model_dump()
    # What an older server would do with it: forbid-extra validation passes.
    assert Suite.model_validate(unset.model_dump(mode="json")) == unset

    gated = Suite.model_validate({**base, "min_pass_rate": 0.9})
    assert gated.model_dump(mode="json")["min_pass_rate"] == 0.9
