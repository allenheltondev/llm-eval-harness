"""The fallback plan: building it from a comparison and checking it against a suite file."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nimbus import plan
from nimbus.evals.compare import suite_fingerprint
from nimbus.suite_edit import stored_suite

NOW = datetime(2026, 6, 1, tzinfo=UTC)

SUITE = {
    "run_config": {"model_id": "base"},
    "readiness": {"max_regression_rate": 0.1},
    "arms": [
        {"name": "primary", "system_prompt": "A"},
        {"name": "backup", "system_prompt": "B"},
    ],
    "cases": [{"id": "c1", "input": "q", "judge": False, "assert": [{"contains": "x"}]}],
}


def comparison(**overrides):
    def arm(label, baseline, status=None, prompt="aaaa"):
        vs = (
            None
            if baseline
            else {
                "status": status,
                "reasons": [f"because {status}"],
                "regressions": [],
                "baseline_passed": 30,
                "regression_upper_bound": 0.09,
            }
        )
        return {
            "label": label,
            "provider": "bedrock",
            "model_id": "base",
            "prompt_id": prompt,
            "evaluation_id": f"eval-{label}",
            "pass_rate": 1.0,
            "baseline": baseline,
            "vs_baseline": vs,
        }

    base = {
        "baseline": "primary",
        "ranking": ["primary", "b", "a", "c"],
        "arms": [
            arm("primary", True),
            arm("a", False, "not_ready"),
            arm("b", False, "ready"),
            arm("c", False, "inconclusive"),
        ],
        "bar": {"max_regression_rate": 0.1, "source": "suite"},
        "suite": {"name": "s", "cases": 1, "fingerprint": suite_fingerprint(stored_suite(SUITE))},
    }
    return {**base, **overrides}


def test_only_ready_arms_become_fallbacks_and_the_rest_are_evidence():
    built = plan.build(comparison(), now=NOW)

    assert [entry["label"] for entry in built["fallbacks"]] == ["b"]
    assert {entry["label"]: entry["status"] for entry in built["evidence"]} == {
        "a": "not_ready",
        "c": "inconclusive",
    }
    assert built["evidence"][0]["reasons"]


def test_fallbacks_are_in_ranking_order():
    document = comparison()
    document["arms"][1]["vs_baseline"]["status"] = "ready"

    built = plan.build({**document, "ranking": ["primary", "b", "a", "c"]}, now=NOW)

    assert [entry["label"] for entry in built["fallbacks"]] == ["b", "a"]


def test_the_plan_pins_what_the_verdicts_were_about():
    built = plan.build(comparison(), suite_file="suite.yaml", now=NOW, max_age_days=7)

    assert built["created_at"] == "2026-06-01T00:00:00Z"
    assert built["max_age_days"] == 7
    assert built["suite"]["file"] == "suite.yaml"
    assert built["bar"] == {
        "max_regression_rate": 0.1,
        "max_latency_ratio": None,
        "max_cost_ratio": None,
    }
    assert built["baseline"]["evaluation_id"] == "eval-primary"
    assert built["fallbacks"][0]["prompt_id"] == "aaaa"


def test_a_comparison_without_a_baseline_cannot_be_planned():
    with pytest.raises(plan.PlanError, match="needs a baseline"):
        plan.build(comparison(baseline=None), now=NOW)


def test_a_non_positive_age_is_refused():
    with pytest.raises(plan.PlanError, match="at least 1"):
        plan.build(comparison(), now=NOW, max_age_days=0)


@pytest.mark.parametrize("document", [[], {}, {"version": 2}, {"version": 1}])
def test_a_file_that_is_not_a_plan_is_refused(document):
    with pytest.raises(plan.PlanError):
        plan.load(document)


def test_a_plan_without_a_required_section_says_which():
    with pytest.raises(plan.PlanError, match="fallbacks"):
        plan.load({"version": 1, "created_at": "x", "suite": {}, "bar": {}, "baseline": {}})


def test_a_garbled_date_is_stale():
    built = plan.build(comparison(), now=NOW)
    built["created_at"] = "yesterday-ish"

    findings = plan.check(built, None, Path("."), NOW)

    assert findings[0].code == "expired"
    assert findings[0].stale


def test_a_plan_with_no_fallback_is_advice_not_staleness():
    document = comparison()
    document["arms"] = [document["arms"][0], document["arms"][1]]
    built = plan.build(document, now=NOW)
    built["suite"]["fingerprint"] = suite_fingerprint(stored_suite(SUITE))

    findings = plan.check(built, SUITE, Path("."), NOW)

    assert [(f.code, f.stale) for f in findings if f.code == "no_fallback"] == [
        ("no_fallback", False)
    ]


def test_a_suite_without_arms_is_only_partly_verifiable():
    built = plan.build(comparison(), now=NOW)
    spec = {key: value for key, value in SUITE.items() if key != "arms"}

    findings = plan.check(built, spec, Path("."), NOW)

    assert [(f.code, f.stale) for f in findings] == [("unverifiable", False)]


def test_a_plan_at_the_edge_of_its_age_still_holds():
    built = plan.build(comparison(), now=NOW - timedelta(days=30), max_age_days=30)

    assert not any(f.code == "expired" for f in plan.check(built, None, Path("."), NOW))
    later = NOW + timedelta(days=1)
    assert any(f.code == "expired" for f in plan.check(built, None, Path("."), later))
