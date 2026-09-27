"""CI gating: did an evaluation that *ran* meet the bar it was given?

Exit codes ``0``/``1``/``2``/``130`` say whether the harness did its job. A
gate adds one more answer, ``3``: the harness worked, and the result is not
good enough to ship. Everything here is a pure function of the evaluation's
``result`` document -- the same dict a local run returns and a stack serves --
so a gate means the same thing wherever the evaluation ran, and it respects
whatever the engine decides makes a suite case pass or fail.

The fields read are deliberately few:

* ``result["score"]`` -- the headline score, 0-100 (``None`` when nothing was
  scored).
* ``result["metrics"]["pass_rate"]`` -- the fraction of suite cases passed.
* ``result["cases"][*]["passed"]`` / ``["status"]`` / ``["id"]`` -- each case's
  verdict, plus ``score``, ``reasoning``, ``error`` and (when present)
  ``assertions`` for the JUnit report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

#: Ran, but did not meet the bar a gate flag set.
EXIT_GATE_FAILED = 3


@dataclass(frozen=True)
class Gate:
    """The bar an evaluation must meet; every part is optional."""

    #: Fail when the overall score (0-100) is below this.
    fail_under: float | None = None
    #: Fail when any suite case did not pass.
    fail_on_case_failure: bool = False
    #: Fail when the suite's pass rate is below this (``--gate``: the suite
    #: file's ``min_pass_rate``, or 1.0 -- every case -- when it sets none).
    min_pass_rate: float | None = None

    @property
    def active(self) -> bool:
        return (
            self.fail_under is not None
            or self.fail_on_case_failure
            or self.min_pass_rate is not None
        )


def failed_cases(result: dict[str, Any]) -> list[str]:
    """The ids of the suite cases that did not pass, in suite order."""
    return [str(case.get("id")) for case in result.get("cases") or [] if not case.get("passed")]


def check(gate: Gate, result: dict[str, Any] | None) -> list[str]:
    """Why ``result`` misses the bar: one sentence per miss, empty when it passes.

    A result with no score cannot show it cleared ``fail_under``, so it does
    not; the same goes for a missing pass rate against ``min_pass_rate``.
    """
    result = result or {}
    reasons: list[str] = []
    if gate.fail_under is not None:
        score = result.get("score")
        if score is None:
            reasons.append(f"no overall score to compare with --fail-under {gate.fail_under:g}")
        elif score < gate.fail_under:
            reasons.append(f"score {score:g} is under --fail-under {gate.fail_under:g}")
    if gate.min_pass_rate is not None:
        rate = (result.get("metrics") or {}).get("pass_rate")
        if rate is None:
            reasons.append("no pass rate to compare with the suite's min_pass_rate")
        elif rate < gate.min_pass_rate:
            bar = gate.min_pass_rate
            reasons.append(f"pass rate {rate:.0%} is under the suite's bar of {bar:.0%}")
    if gate.fail_on_case_failure:
        failed = failed_cases(result)
        if failed:
            reasons.append(f"{len(failed)} case(s) did not pass: {', '.join(failed)}")
    return reasons


# --------------------------------------------------------------------------- #
# JUnit XML
# --------------------------------------------------------------------------- #


def _failure_text(case: dict[str, Any]) -> str:
    """The body of a failed case's ``<failure>``: score, reasoning, failed assertions."""
    lines = []
    if case.get("score") is not None:
        lines.append(f"score {case['score']}")
    for assertion in case.get("assertions") or []:
        if isinstance(assertion, dict) and assertion.get("passed") is False:
            label = assertion.get("message") or assertion.get("type") or "assertion"
            lines.append(f"assertion failed: {label}")
    if case.get("reasoning"):
        lines.append(str(case["reasoning"]))
    return "\n".join(lines)


def _error_message(error: Any, default: str) -> str:
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    if isinstance(error, str) and error:
        return error
    return default


def junit_xml(terminal: dict[str, Any], *, name: str | None = None) -> str:
    """A JUnit report for one evaluation: one ``<testcase>`` per suite case.

    ``terminal`` is the evaluation's final state (``status``, ``result``,
    ``error``). A case that ran and scored too low is a ``<failure>``; one that
    never ran or went unjudged is an ``<error>``. An evaluation that ended
    without a result (it errored or was cancelled) is reported as a single
    errored testcase, so CI still shows *something* went wrong.
    """
    result = terminal.get("result") or {}
    suite_name = name or (result.get("suite") or {}).get("name") or "nimbus"
    classname = f"nimbus.{suite_name}"
    root = ET.Element("testsuites")
    testsuite = ET.SubElement(root, "testsuite", name=suite_name)
    if terminal.get("evaluation_id"):
        properties = ET.SubElement(testsuite, "properties")
        ET.SubElement(
            properties, "property", name="evaluation_id", value=str(terminal["evaluation_id"])
        )

    counts = {"tests": 0, "failures": 0, "errors": 0}
    cases = result.get("cases")
    if terminal.get("status") != "completed" or not cases:
        counts["tests"] = 1
        testcase = ET.SubElement(
            testsuite, "testcase", name="evaluation", classname=classname, time="0"
        )
        if terminal.get("status") != "completed":
            counts["errors"] = 1
            message = _error_message(
                terminal.get("error"), f"evaluation {terminal.get('status') or 'did not finish'}"
            )
            ET.SubElement(testcase, "error", message=message, type=str(terminal.get("status")))
    else:
        for case in cases:
            counts["tests"] += 1
            testcase = ET.SubElement(
                testsuite, "testcase", name=str(case.get("id")), classname=classname, time="0"
            )
            if case.get("passed"):
                continue
            status = str(case.get("status") or "failed")
            if status == "failed":
                counts["failures"] += 1
                element = ET.SubElement(
                    testcase, "failure", message=f"case {case.get('id')} failed", type=status
                )
                element.text = _failure_text(case)
            else:
                counts["errors"] += 1
                message = _error_message(case.get("error"), f"case {case.get('id')}: {status}")
                ET.SubElement(testcase, "error", message=message, type=status)

    for key, value in counts.items():
        testsuite.set(key, str(value))
        root.set(key, str(value))
    testsuite.set("skipped", "0")
    testsuite.set("time", "0")
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
