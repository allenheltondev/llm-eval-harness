"""The request fields that describe a suite's place in a comparison, and the latency it reports."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from nimbus.errors import BadRequestError
from nimbus.evals import engine as evals_engine
from nimbus.evals.cloud import worker_payload
from nimbus.evals.outcomes import RunOutcome
from nimbus.evals.readiness import ReadinessBar
from nimbus.evals.schemas import ArmTag, EvaluationRequest, SuiteCase

SUITE = {
    "run_config": {"model_id": "m"},
    "cases": [{"id": "c1", "input": "q1"}, {"id": "c2", "input": "q2", "critical": True}],
}


def request(**fields: Any) -> EvaluationRequest:
    return EvaluationRequest.model_validate({"kind": "suite", "suite": SUITE, **fields})


# --------------------------------------------------------------------------- #
# critical cases
# --------------------------------------------------------------------------- #


def test_a_case_is_not_critical_unless_marked():
    assert SuiteCase.model_validate({"id": "a", "input": "q"}).critical is False
    assert SuiteCase.model_validate({"id": "a", "input": "q", "critical": True}).critical is True


def test_an_unmarked_case_serializes_exactly_as_it_did_before_criticality_existed():
    """A CLI can send suites to a stack running older code, which forbids unknown fields."""
    dumped = SuiteCase.model_validate({"id": "a", "input": "q"}).model_dump()

    assert "critical" not in dumped


def test_a_critical_case_keeps_its_mark_through_a_round_trip():
    stored = request().suite.model_dump()

    assert [case.get("critical") for case in stored["cases"]] == [None, True]
    assert SuiteCase.model_validate(stored["cases"][1]).critical is True


# --------------------------------------------------------------------------- #
# arm tags
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["primary", "sonnet/terse", "bedrock:m", "a.b-c_d+e", "A1"])
def test_reasonable_arm_names_are_accepted(name):
    assert ArmTag(name=name).name == name


@pytest.mark.parametrize("name", ["", " lead", "has space", "-dash", "x" * 101, "a\nb"])
def test_unreasonable_arm_names_are_refused(name):
    with pytest.raises(ValidationError):
        ArmTag(name=name)


def test_a_tag_stores_only_what_is_set():
    assert ArmTag(name="p").stored() == {"name": "p"}
    assert ArmTag(name="p", baseline=True, axes={"model": "m", "prompt": "t"}).stored() == {
        "name": "p",
        "baseline": True,
        "axes": {"model": "m", "prompt": "t"},
    }


def test_a_tag_may_not_carry_more_than_four_axes_or_unknown_fields():
    with pytest.raises(ValidationError):
        ArmTag(name="p", axes={str(n): "v" for n in range(5)})
    with pytest.raises(ValidationError):
        ArmTag.model_validate({"name": "p", "colour": "red"})


# --------------------------------------------------------------------------- #
# On the request
# --------------------------------------------------------------------------- #


def test_arm_and_readiness_are_stored_with_the_evaluation():
    built = request(
        arm={"name": "fallback", "axes": {"model": "nova"}},
        readiness={"max_regression_rate": 0.05, "max_latency_ratio": 1.5},
    )

    config = built.stored_config()

    assert config["arm"] == {"name": "fallback", "axes": {"model": "nova"}}
    assert config["readiness"] == {"max_regression_rate": 0.05, "max_latency_ratio": 1.5}


def test_a_suite_without_them_stores_nothing_extra():
    config = request().stored_config()

    assert "arm" not in config and "readiness" not in config


def test_a_bar_stores_its_defaults_but_not_the_limits_it_leaves_unset():
    config = request(readiness={}).stored_config()

    assert config["readiness"] == {"max_regression_rate": 0.1}


@pytest.mark.parametrize("field", [{"arm": {"name": "p"}}, {"readiness": {}}])
def test_only_suites_have_arms_and_bars(field):
    with pytest.raises(BadRequestError) as caught:
        EvaluationRequest.model_validate(
            {
                "kind": "determinism",
                "run_config": {"model_id": "m", "user_prompt": "hi"},
                **field,
            }
        )

    assert caught.value.code == "arm_requires_suite"


def test_the_tag_and_bar_survive_the_trip_to_the_cloud_worker():
    built = request(arm={"name": "p", "baseline": True}, readiness={"max_cost_ratio": 2})

    received = EvaluationRequest.model_validate(worker_payload("eval-1", built)["request"])

    assert received.arm == built.arm
    assert received.readiness == ReadinessBar(max_cost_ratio=2)


# --------------------------------------------------------------------------- #
# latency
# --------------------------------------------------------------------------- #


def outcome(duration_ms: int, *, ok: bool = True) -> RunOutcome:
    return RunOutcome(
        index=0,
        run_id="r" if ok else None,
        status="completed" if ok else "error",
        duration_ms=duration_ms,
    )


def test_latency_is_the_nearest_rank_percentile_of_the_runs_that_answered():
    durations = [outcome(ms) for ms in range(100, 2100, 100)]  # 100 .. 2000, twenty runs

    assert evals_engine._latency_metrics(durations) == {"latency_ms": {"p50": 1000, "p95": 1900}}


def test_a_failed_run_does_not_count_toward_latency():
    assert evals_engine._latency_metrics([outcome(100), outcome(90_000, ok=False)]) == {
        "latency_ms": {"p50": 100, "p95": 100}
    }


def test_no_answers_means_no_latency_rather_than_a_zero():
    assert evals_engine._latency_metrics([]) == {}
    assert evals_engine._latency_metrics([outcome(5, ok=False)]) == {}


def test_a_single_run_is_its_own_percentiles():
    assert evals_engine._latency_metrics([outcome(42)]) == {"latency_ms": {"p50": 42, "p95": 42}}
