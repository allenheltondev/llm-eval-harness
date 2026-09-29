"""Comparing arms against a baseline: what a fallback breaks, and whether it is ready.

Built from stored-evaluation dicts, the shape the API and the CLI both hand to
:func:`build_comparison`, so these go through the one real entry point.
"""

from __future__ import annotations

from typing import Any

import pytest

from nimbus.evals.compare import (
    Arm,
    ComparisonError,
    arm_from_evaluation,
    build_comparison,
    compare,
    model_family,
    prompt_id,
)
from nimbus.evals.readiness import ReadinessBar

PASS, FAIL = "passed", "failed"


def evaluation(
    name: str | None,
    verdicts: dict[str, str],
    *,
    model: str = "model-a",
    provider: str | None = None,
    prompt: str | None = None,
    inference: dict | None = None,
    status: str = "completed",
    baseline: bool = False,
    axes: dict[str, str] | None = None,
    bar: dict | None = None,
    critical: tuple[str, ...] = (),
    p95: int | None = 1000,
    cost: float | None = 1.0,
    repeats: int = 3,
    judge: str = "amazon.nova-pro-v1:0",
    panel: list[str] | None = None,
    toolset: str | None = None,
    assertions: list[dict] | None = None,
    score: int = 80,
    kind: str = "suite",
    inputs: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A stored suite evaluation whose cases have these verdicts."""
    passed = sum(1 for verdict in verdicts.values() if verdict == PASS)
    run_config: dict[str, Any] = {"model_id": model}
    if provider:
        run_config["provider"] = provider
    if prompt:
        run_config["system_prompt"] = prompt
    if inference:
        run_config["inference"] = inference
    if toolset:
        run_config["toolset"] = toolset
    config: dict[str, Any] = {
        "kind": kind,
        "grader": {"model_id": judge},
        "suite": {
            "name": "support",
            "run_config": run_config,
            "cases": [
                {
                    "id": case_id,
                    "input": (inputs or {}).get(case_id, f"question {case_id}"),
                    **({"critical": True} if case_id in critical else {}),
                    **({"assert": assertions} if assertions else {}),
                }
                for case_id in verdicts
            ],
        },
    }
    if name:
        config["arm"] = {
            "name": name,
            **({"baseline": True} if baseline else {}),
            **({"axes": axes} if axes else {}),
        }
    if bar is not None:
        config["readiness"] = bar
    metrics: dict[str, Any] = {
        "pass_rate": passed / len(verdicts),
        "cases_passed": passed,
        "cases_total": len(verdicts),
        "cases_errored": 0,
    }
    if p95 is not None:
        metrics["latency_ms"] = {"p50": p95 // 2, "p95": p95}
    return {
        "id": f"eval-{name or model}",
        "kind": kind,
        "status": status,
        "config": config,
        "result": {
            "score": score,
            "grade": "B",
            "metrics": metrics,
            "suite": {"repeats": repeats},
            "cost": {"total_usd": cost},
            "judge": {"model_id": judge, **({"panel": panel} if panel else {})},
            "cases": [
                {"id": case_id, "status": verdict, "score": 1.0 if verdict == PASS else 0.0}
                for case_id, verdict in verdicts.items()
            ],
        },
    }


def all_pass(count: int) -> dict[str, str]:
    return {f"c{n:02d}": PASS for n in range(count)}


def breaking(count: int, broken: tuple[str, ...]) -> dict[str, str]:
    return {case: (FAIL if case in broken else PASS) for case in all_pass(count)}


def build(*evaluations: dict[str, Any], **options: Any) -> dict[str, Any]:
    return build_comparison(list(evaluations), **options)


def summary(comparison: dict[str, Any], label: str) -> dict[str, Any]:
    return next(arm for arm in comparison["arms"] if arm["label"] == label)


# --------------------------------------------------------------------------- #
# Naming and finding the baseline
# --------------------------------------------------------------------------- #


def test_an_arm_is_labelled_by_its_name_else_by_its_model():
    named = arm_from_evaluation(evaluation("fallback", all_pass(2)))
    unnamed = arm_from_evaluation(evaluation(None, all_pass(2), provider="openai", model="gpt-4o"))

    assert named.label == "fallback"
    assert unnamed.label == "openai:gpt-4o"


def test_two_arms_may_share_a_model_when_their_prompts_give_them_different_names():
    comparison = build(
        evaluation("terse", all_pass(3), prompt="Be terse."),
        evaluation("chatty", all_pass(3), prompt="Be friendly."),
    )

    assert [arm["label"] for arm in comparison["arms"]] == ["terse", "chatty"]


def test_the_same_name_twice_is_refused_and_says_to_rename():
    with pytest.raises(ComparisonError) as caught:
        build(evaluation("same", all_pass(2)), evaluation("same", all_pass(2), model="other"))

    assert caught.value.code == "compare_duplicate_arm"
    assert "different name" in caught.value.message


def test_the_same_unnamed_model_twice_still_says_model():
    with pytest.raises(ComparisonError) as caught:
        build(evaluation(None, all_pass(2)), evaluation(None, all_pass(2)))

    assert caught.value.code == "compare_duplicate_model"


def test_without_a_baseline_nothing_is_measured_against_one():
    comparison = build(evaluation("a", all_pass(3)), evaluation("b", all_pass(3), model="m2"))

    assert comparison["baseline"] is None
    assert "bar" not in comparison
    assert all("vs_baseline" not in arm for arm in comparison["arms"])


def test_the_arm_tagged_as_baseline_is_the_baseline():
    comparison = build(
        evaluation("fallback", all_pass(3), model="m2"),
        evaluation("primary", all_pass(3), baseline=True),
    )

    assert comparison["baseline"] == "primary"
    assert summary(comparison, "primary")["baseline"] is True
    assert "vs_baseline" not in summary(comparison, "primary")
    assert "vs_baseline" in summary(comparison, "fallback")


def test_a_baseline_can_be_chosen_by_label_or_by_evaluation_id():
    one, two = evaluation("a", all_pass(3)), evaluation("b", all_pass(3), model="m2")

    assert build(one, two, baseline="b")["baseline"] == "b"
    assert build(one, two, baseline="eval-a")["baseline"] == "a"


def test_a_requested_baseline_overrides_the_tagged_one():
    comparison = build(
        evaluation("a", all_pass(3), baseline=True),
        evaluation("b", all_pass(3), model="m2"),
        baseline="b",
    )

    assert comparison["baseline"] == "b"


def test_an_unknown_baseline_is_refused():
    with pytest.raises(ComparisonError) as caught:
        build(evaluation("a", all_pass(3)), evaluation("b", all_pass(3), model="m2"), baseline="x")

    assert caught.value.code == "compare_unknown_baseline"


# --------------------------------------------------------------------------- #
# What a candidate breaks
# --------------------------------------------------------------------------- #


def test_the_cases_the_baseline_passes_and_the_candidate_fails_are_listed():
    base = evaluation("primary", breaking(40, ()), baseline=True)
    cand = evaluation("fallback", breaking(40, ("c03", "c07")), model="m2")

    comparison = build(base, cand)
    vs = summary(comparison, "fallback")["vs_baseline"]

    assert vs["regressions"] == ["c03", "c07"]
    assert vs["improvements"] == []
    assert vs["baseline_passed"] == 40
    assert vs["regression_rate"] == pytest.approx(0.05)
    assert vs["pass_rate_delta"] == pytest.approx(-0.05)


def test_improvements_are_listed_separately_from_regressions():
    base_cases = breaking(40, ("c01", "c02"))
    cand_cases = breaking(40, ("c09",))
    base = evaluation("primary", base_cases, baseline=True)
    cand = evaluation("fallback", cand_cases, model="m2")

    vs = summary(build(base, cand), "fallback")["vs_baseline"]

    assert vs["regressions"] == ["c09"]
    assert vs["improvements"] == ["c01", "c02"]


def test_a_clean_candidate_on_a_big_suite_is_ready():
    base = evaluation("primary", all_pass(40), baseline=True)
    cand = evaluation("fallback", all_pass(40), model="m2")

    vs = summary(build(base, cand), "fallback")["vs_baseline"]

    assert vs["status"] == "ready"


def test_a_clean_candidate_on_a_small_suite_is_only_inconclusive():
    base = evaluation("primary", all_pass(10), baseline=True)
    cand = evaluation("fallback", all_pass(10), model="m2")

    vs = summary(build(base, cand), "fallback")["vs_baseline"]

    assert vs["status"] == "inconclusive"
    assert vs["cases_needed"] == 29
    assert vs["regression_upper_bound"] > 0.10


def test_a_candidate_that_breaks_too_much_is_not_ready():
    base = evaluation("primary", all_pass(40), baseline=True)
    cand = evaluation("fallback", breaking(40, tuple(f"c{n:02d}" for n in range(8))), model="m2")

    vs = summary(build(base, cand), "fallback")["vs_baseline"]

    assert vs["status"] == "not_ready"
    assert "8 of the 40 cases" in vs["reasons"][0]


def test_a_regression_on_a_critical_case_is_not_ready_however_rare():
    base = evaluation("primary", all_pass(100), baseline=True, critical=("c05",))
    cand = evaluation("fallback", breaking(100, ("c05",)), model="m2")

    vs = summary(build(base, cand), "fallback")["vs_baseline"]

    assert vs["status"] == "not_ready"
    assert vs["critical_regressions"] == ["c05"]


def test_a_case_critical_only_in_the_candidates_suite_still_counts():
    base = evaluation("primary", all_pass(100), baseline=True)
    cand = evaluation("fallback", breaking(100, ("c05",)), model="m2", critical=("c05",))

    assert summary(build(base, cand), "fallback")["vs_baseline"]["status"] == "not_ready"


def test_a_candidate_that_did_not_complete_is_not_ready():
    base = evaluation("primary", all_pass(40), baseline=True)
    cand = evaluation("fallback", all_pass(40), model="m2", status="cancelled")

    vs = summary(build(base, cand), "fallback")["vs_baseline"]

    assert vs["status"] == "not_ready"
    assert "cancelled" in vs["reasons"][0]


def test_when_the_baseline_did_not_complete_nothing_can_be_measured():
    base = evaluation("primary", all_pass(40), baseline=True, status="error")
    cand = evaluation("fallback", all_pass(40), model="m2")

    comparison = build(base, cand)

    assert summary(comparison, "fallback")["vs_baseline"] is None
    assert "baseline_incomplete" in {w["code"] for w in comparison["warnings"]}


def test_latency_and_cost_ratios_come_from_the_results():
    base = evaluation("primary", all_pass(40), baseline=True, p95=1000, cost=2.0)
    cand = evaluation("fallback", all_pass(40), model="m2", p95=1800, cost=5.0)

    vs = summary(build(base, cand), "fallback")["vs_baseline"]

    assert vs["latency_ratio"] == pytest.approx(1.8)
    assert vs["cost_ratio"] == pytest.approx(2.5)


def test_the_suites_own_bar_is_used_and_can_fail_a_slow_fallback():
    bar = {"max_regression_rate": 0.10, "max_latency_ratio": 1.5}
    base = evaluation("primary", all_pass(40), baseline=True, bar=bar, p95=1000)
    cand = evaluation("fallback", all_pass(40), model="m2", bar=bar, p95=2000)

    comparison = build(base, cand)

    assert comparison["bar"]["source"] == "suite"
    assert comparison["bar"]["max_latency_ratio"] == 1.5
    vs = summary(comparison, "fallback")["vs_baseline"]
    assert vs["status"] == "not_ready"
    assert "p95 latency is 2.0x" in vs["reasons"][0]


def test_with_no_bar_stored_the_default_applies_and_says_so():
    base = evaluation("primary", all_pass(40), baseline=True)
    cand = evaluation("fallback", all_pass(40), model="m2")

    comparison = build(base, cand)

    assert comparison["bar"] == {
        "max_regression_rate": 0.1,
        "max_latency_ratio": None,
        "max_cost_ratio": None,
        "source": "default",
    }


def test_a_bar_passed_in_overrides_the_stored_one():
    stored = {"max_regression_rate": 0.5}
    arms = [
        arm_from_evaluation(evaluation("primary", all_pass(40), baseline=True, bar=stored)),
        arm_from_evaluation(evaluation("fallback", all_pass(40), model="m2", bar=stored)),
    ]

    comparison = compare(arms, bar=ReadinessBar(max_regression_rate=0.01))

    assert comparison["bar"]["source"] == "requested"
    assert comparison["bar"]["max_regression_rate"] == 0.01


# --------------------------------------------------------------------------- #
# What differs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ({"model": "m2"}, ["model"]),
        ({"provider": "openai"}, ["model"]),
        ({"prompt": "Be brief."}, ["prompt"]),
        ({"inference": {"temperature": 0.2}}, ["inference"]),
        ({"toolset": "fraud-detection"}, ["tools"]),
        ({"model": "m2", "prompt": "Be brief."}, ["model", "prompt"]),
        ({}, []),
    ],
)
def test_each_candidate_says_what_it_changes_from_the_baseline(candidate, expected):
    base = evaluation("primary", all_pass(4), baseline=True, prompt="Be thorough.")
    options = {"prompt": "Be thorough."} | candidate
    cand = evaluation("fallback", all_pass(4), **options)

    assert summary(build(base, cand), "fallback")["changes"] == expected


def test_a_missing_prompt_and_an_empty_one_are_the_same_prompt():
    base = evaluation("primary", all_pass(4), baseline=True)
    cand = evaluation("fallback", all_pass(4), prompt="")

    assert summary(build(base, cand), "fallback")["changes"] == []


def test_arms_with_the_same_prompt_share_a_prompt_id_and_different_ones_do_not():
    assert prompt_id("Be terse.") == prompt_id("Be terse.")
    assert prompt_id("Be terse.") != prompt_id("Be chatty.")
    assert prompt_id(None) is None and prompt_id("") is None
    assert len(prompt_id("x")) == 8


# --------------------------------------------------------------------------- #
# Which axis moves the score
# --------------------------------------------------------------------------- #


def grid(rates: dict[tuple[str, str], int], *, statuses: dict | None = None):
    """A models-by-prompts grid of arms; each cell's value is how many of 20 cases pass."""
    evaluations = []
    for (model, prompt), passing in rates.items():
        verdicts = {f"c{n:02d}": (PASS if n < passing else FAIL) for n in range(20)}
        evaluations.append(
            evaluation(
                f"{model}/{prompt}",
                verdicts,
                model=model,
                prompt=prompt,
                axes={"model": model, "prompt": prompt},
                baseline=(model, prompt) == next(iter(rates)),
                status=(statuses or {}).get((model, prompt), "completed"),
            )
        )
    return evaluations


def test_a_complete_grid_reports_each_axis_mean_and_which_one_dominates():
    # The prompt swings the pass rate by 40 points; the model by 10.
    cells = {("a", "good"): 19, ("b", "good"): 17, ("a", "bad"): 11, ("b", "bad"): 9}

    effects = build(*grid(cells))["effects"]

    prompt = {entry["value"]: entry["mean_pass_rate"] for entry in effects["axes"]["prompt"]}
    model = {entry["value"]: entry["mean_pass_rate"] for entry in effects["axes"]["model"]}
    assert prompt == {"bad": pytest.approx(0.5), "good": pytest.approx(0.9)}
    assert model == {"a": pytest.approx(0.75), "b": pytest.approx(0.65)}
    assert effects["spread"] == {"model": pytest.approx(0.10), "prompt": pytest.approx(0.40)}
    assert effects["dominant"] == "prompt"


def test_the_dominant_axis_can_be_the_model():
    cells = {("a", "p"): 19, ("b", "p"): 9, ("a", "q"): 18, ("b", "q"): 8}

    assert build(*grid(cells))["effects"]["dominant"] == "model"


def test_neither_axis_dominates_when_they_move_the_score_about_equally():
    cells = {("a", "p"): 18, ("b", "p"): 12, ("a", "q"): 12, ("b", "q"): 6}

    effects = build(*grid(cells))["effects"]

    assert effects["spread"]["model"] == pytest.approx(effects["spread"]["prompt"])
    assert effects["dominant"] is None


def test_an_incomplete_grid_reports_no_effects_rather_than_a_biased_average():
    cells = {("a", "p"): 18, ("b", "p"): 12, ("a", "q"): 12}  # no b/q

    assert "effects" not in build(*grid(cells))


def test_a_grid_with_an_unfinished_cell_reports_no_effects():
    cells = {("a", "p"): 18, ("b", "p"): 12, ("a", "q"): 12, ("b", "q"): 6}

    comparison = build(*grid(cells, statuses={("b", "q"): "cancelled"}))

    assert "effects" not in comparison


def test_arms_without_axes_have_no_effects():
    comparison = build(
        evaluation("a", all_pass(4)),
        evaluation("b", all_pass(4), model="m2"),
        evaluation("c", all_pass(4), model="m3"),
        evaluation("d", all_pass(4), model="m4"),
    )

    assert "effects" not in comparison


def test_mismatched_axes_have_no_effects():
    evaluations = grid({("a", "p"): 18, ("b", "p"): 12, ("a", "q"): 12, ("b", "q"): 6})
    evaluations[0]["config"]["arm"]["axes"] = {"model": "a", "temperature": "low"}

    assert "effects" not in build(*evaluations)


def test_a_single_valued_axis_is_not_an_experiment():
    evaluations = [
        evaluation(f"m{n}", all_pass(4), model=f"m{n}", axes={"model": f"m{n}", "prompt": "p"})
        for n in range(4)
    ]

    assert "effects" not in build(*evaluations)


# --------------------------------------------------------------------------- #
# Ways it could mislead
# --------------------------------------------------------------------------- #


def codes(comparison: dict[str, Any]) -> set[str]:
    return {warning["code"] for warning in comparison["warnings"]}


def test_a_clean_large_repeated_comparison_has_no_warnings():
    comparison = build(
        evaluation("a", all_pass(40), baseline=True),
        evaluation("b", all_pass(40), model="m2", judge="gpt-4o"),
    )

    assert comparison["warnings"] == []


def test_a_single_repeat_is_flagged_for_the_arms_that_ran_once():
    comparison = build(
        evaluation("a", all_pass(40), repeats=1), evaluation("b", all_pass(40), model="m2")
    )

    warning = next(w for w in comparison["warnings"] if w["code"] == "single_repeat")
    assert warning["arms"] == ["a"]


def test_a_small_suite_is_flagged():
    comparison = build(evaluation("a", all_pass(19)), evaluation("b", all_pass(19), model="m2"))

    assert "small_suite" in codes(comparison)
    big = build(evaluation("a", all_pass(20)), evaluation("b", all_pass(20), model="m2"))
    assert "small_suite" not in codes(big)


@pytest.mark.parametrize(
    ("model", "judge", "overlaps"),
    [
        ("claude-sonnet-4-5", "anthropic.claude-3-haiku-v1:0", True),
        ("us.amazon.nova-lite-v1:0", "amazon.nova-pro-v1:0", True),
        ("gpt-4o", "gpt-4o-mini", True),
        ("llama3.1:8b", "meta.llama3-1-70b-instruct-v1:0", True),
        ("claude-sonnet-4-5", "amazon.nova-pro-v1:0", False),
        ("gpt-4o", "amazon.nova-pro-v1:0", False),
    ],
)
def test_a_judge_from_the_same_family_as_an_arm_is_flagged(model, judge, overlaps):
    comparison = build(
        evaluation("a", all_pass(40), model=model, judge=judge),
        evaluation("b", all_pass(40), model="something-else-entirely", judge=judge),
    )

    warned = "judge_family_overlap" in codes(comparison)
    assert warned is overlaps


def test_a_panel_judge_counts_too():
    comparison = build(
        evaluation(
            "a",
            all_pass(40),
            model="claude-sonnet-4-5",
            panel=["openai:gpt-4o", "bedrock:claude-3"],
        ),
        evaluation("b", all_pass(40), model="gpt-4o-mini", judge="amazon.nova-pro-v1:0"),
    )

    warning = next(w for w in comparison["warnings"] if w["code"] == "judge_family_overlap")
    assert warning["arms"] == ["a"]


def test_tools_given_but_never_asserted_on_is_flagged():
    comparison = build(
        evaluation("a", all_pass(40), toolset="fraud-detection"),
        evaluation("b", all_pass(40), model="m2", toolset="fraud-detection"),
    )

    warning = next(w for w in comparison["warnings"] if w["code"] == "tools_unchecked")
    assert warning["arms"] == ["a", "b"]


@pytest.mark.parametrize(
    "assertion",
    [{"type": "tool_called", "name": "x"}, {"type": "no_tool_errors"}, {"type": "max_tool_calls"}],
)
def test_asserting_on_tool_calls_silences_that_warning(assertion):
    comparison = build(
        evaluation("a", all_pass(40), toolset="t", assertions=[assertion]),
        evaluation("b", all_pass(40), model="m2", toolset="t", assertions=[assertion]),
    )

    assert "tools_unchecked" not in codes(comparison)


def test_no_tools_means_nothing_to_check():
    comparison = build(evaluation("a", all_pass(40)), evaluation("b", all_pass(40), model="m2"))

    assert "tools_unchecked" not in codes(comparison)


def test_changing_model_and_prompt_together_is_flagged_as_confounded():
    base = evaluation("primary", all_pass(40), baseline=True, prompt="Be thorough.")
    cand = evaluation("fallback", all_pass(40), model="m2", prompt="Be brief.")

    warning = next(w for w in build(base, cand)["warnings"] if w["code"] == "confounded")

    assert warning["arms"] == ["fallback"]


def test_changing_only_one_thing_is_not_confounded():
    base = evaluation("primary", all_pass(40), baseline=True)

    assert "confounded" not in codes(build(base, evaluation("f", all_pass(40), model="m2")))
    assert "confounded" not in codes(build(base, evaluation("f", all_pass(40), prompt="x")))


def test_a_grid_lifts_the_confounding_warning_because_it_can_separate_the_axes():
    cells = {("a", "p"): 18, ("b", "p"): 12, ("a", "q"): 12, ("b", "q"): 6}

    comparison = build(*grid(cells))

    assert "effects" in comparison
    assert "confounded" not in codes(comparison)


# --------------------------------------------------------------------------- #
# Reading a stored evaluation
# --------------------------------------------------------------------------- #


def test_an_arm_carries_everything_the_comparison_needs_from_its_stored_evaluation():
    stored = evaluation(
        "fallback",
        all_pass(3),
        model="m2",
        provider="openai",
        prompt="Be brief.",
        baseline=True,
        axes={"model": "m2", "prompt": "brief"},
        bar={"max_regression_rate": 0.02},
        critical=("c01",),
        judge="j1",
        panel=["openai:j2"],
        toolset="t",
        assertions=[{"type": "tool_called", "name": "x"}],
    )

    arm = arm_from_evaluation(stored)

    assert (arm.name, arm.baseline, arm.provider, arm.model_id) == (
        "fallback",
        True,
        "openai",
        "m2",
    )
    assert arm.axes == {"model": "m2", "prompt": "brief"}
    assert arm.run_config["system_prompt"] == "Be brief."
    assert arm.readiness == ReadinessBar(max_regression_rate=0.02)
    assert arm.critical == frozenset({"c01"})
    assert arm.judges == ("bedrock:j1", "openai:j2")
    assert arm.uses_tools and arm.asserts_tools


def test_an_old_evaluation_with_none_of_the_new_fields_still_reads():
    stored = evaluation(None, all_pass(3))
    for key in ("arm", "readiness"):
        stored["config"].pop(key, None)

    arm = arm_from_evaluation(stored)

    assert (arm.name, arm.baseline, arm.axes, arm.readiness, arm.critical) == (
        None,
        False,
        {},
        None,
        frozenset(),
    )


def test_an_evaluation_with_no_result_at_all_still_reads():
    stored = evaluation("a", all_pass(3), status="error")
    stored["result"] = None

    arm = arm_from_evaluation(stored)

    assert arm.result is None and arm.judges == ()


# --------------------------------------------------------------------------- #
# The entry point's refusals
# --------------------------------------------------------------------------- #


def test_a_set_that_is_not_all_suites_is_refused():
    with pytest.raises(ComparisonError) as caught:
        build(
            evaluation("a", all_pass(3)),
            evaluation("b", all_pass(3), model="m", kind="determinism"),
        )

    assert caught.value.code == "compare_not_suite"


def test_arms_that_ran_different_questions_are_refused():
    other = {"c01": "a DIFFERENT question"}

    with pytest.raises(ComparisonError) as caught:
        build(evaluation("a", all_pass(3)), evaluation("b", all_pass(3), model="m2", inputs=other))

    assert caught.value.code == "compare_different_suites"
    assert caught.value.detail == {"cases": ["c01"]}


def test_the_suite_header_names_the_suite_and_its_size():
    comparison = build(evaluation("a", all_pass(7)), evaluation("b", all_pass(7), model="m2"))

    assert comparison["suite"] == {"name": "support", "cases": 7}


# --------------------------------------------------------------------------- #
# Model families
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("model_id", "family"),
    [
        ("claude-sonnet-4-5", "claude"),
        ("anthropic.claude-3-sonnet-20240229-v1:0", "claude"),
        ("us.anthropic.claude-3-5-haiku-20241022-v1:0", "claude"),
        ("global.anthropic.claude-sonnet-4-5", "claude"),
        ("amazon.nova-pro-v1:0", "nova"),
        ("us.amazon.nova-lite-v1:0", "nova"),
        ("gpt-4o", "gpt"),
        ("openai/gpt-4o-mini", "gpt"),
        ("meta.llama3-1-70b-instruct-v1:0", "llama"),
        ("llama3.1:8b", "llama"),
        ("mistral.mistral-large-2402-v1:0", "mistral"),
    ],
)
def test_a_model_id_reduces_to_its_family(model_id, family):
    assert model_family(model_id) == family


def test_an_id_with_no_letters_is_its_own_family():
    assert model_family("1234") == "1234"


# --------------------------------------------------------------------------- #
# Existing behaviour, unchanged
# --------------------------------------------------------------------------- #


def test_ranking_still_ignores_incomplete_arms_and_prefers_cheaper_when_level():
    comparison = build(
        evaluation("dear", all_pass(40), cost=9.0),
        evaluation("cheap", all_pass(40), model="m2", cost=1.0),
        evaluation("cut", all_pass(40), model="m3", status="cancelled"),
    )

    assert comparison["ranking"] == ["cheap", "dear"]
    assert comparison["winner"] is None
    assert sorted(comparison["tied"]) == ["cheap", "dear"]


def test_the_arm_dataclass_still_takes_five_positional_fields():
    arm = Arm("bedrock", "m", "e1", "completed", None)

    assert arm.label == "bedrock:m" and arm.axes == {} and arm.critical == frozenset()


@pytest.mark.parametrize(
    ("label", "model"),
    [
        ("bedrock:anthropic.claude-3-haiku-v1:0", "anthropic.claude-3-haiku-v1:0"),
        ("openai:gpt-4o", "gpt-4o"),
        ("ollama:llama3.1:8b", "llama3.1:8b"),
        ("amazon.nova-pro-v1:0", "amazon.nova-pro-v1:0"),  # a bare id keeps its own colon
        ("llama3.1:8b", "llama3.1:8b"),  # "llama3.1" is not a provider
    ],
)
def test_only_a_known_provider_prefix_is_stripped_from_a_judge_label(label, model):
    from nimbus.evals.compare import _model_of

    assert _model_of(label) == model


# --------------------------------------------------------------------------- #
# The real producer: arms run through the engine, compared from what it stored
# --------------------------------------------------------------------------- #

ANSWERS = {
    "Answer fully.": "The refund window is 30 days, and we open on Sunday.",
    "Be terse.": "30 days.",
}


async def run_stored_arm(tmp_path, name: str, prompt: str, *, baseline: bool = False):
    """Run the suite as one arm and return it as the stored evaluation the API would serve."""
    from nimbus.config import Settings
    from nimbus.engine.fake_model import FakeModel, Text
    from nimbus.evals import engine as evals_engine
    from nimbus.evals.judge import FakeJudgeModel
    from nimbus.evals.schemas import EvaluationRequest
    from nimbus.store import db
    from tests.test_evals_seam import Recorder, RecordingStore

    db.init_db(str(tmp_path / f"{name}.db"))
    request = EvaluationRequest.model_validate(
        {
            "kind": "suite",
            "suite": {
                "run_config": {"model_id": "claude-x", "system_prompt": prompt},
                "cases": [
                    {
                        "id": "refund",
                        "input": "Refund window?",
                        "judge": False,
                        "critical": True,
                        "assert": [{"contains": "30 days"}],
                    },
                    {
                        "id": "hours",
                        "input": "Sunday hours?",
                        "judge": False,
                        "assert": [{"contains": "Sunday"}],
                    },
                ],
            },
            "arm": {"name": name, "baseline": baseline},
            "readiness": {"max_regression_rate": 0.10},
        }
    )
    terminal = await evals_engine.execute_evaluation_with_seam(
        request,
        Recorder().emit,
        RecordingStore(f"eval-{name}"),
        deps=evals_engine.EvalDeps(
            settings=Settings(_env_file=None),
            model_factory=lambda req: FakeModel(script=[Text(ANSWERS[req.system_prompt])]),
            judge_factory=lambda _id, _provider="bedrock": FakeJudgeModel(),
        ),
    )
    return {
        "id": f"eval-{name}",
        "kind": "suite",
        "status": terminal["status"],
        "config": request.stored_config(),
        "result": terminal["result"],
    }


async def test_real_arms_are_compared_from_what_the_engine_stored(tmp_path):
    primary = await run_stored_arm(tmp_path, "primary", "Answer fully.", baseline=True)
    terse = await run_stored_arm(tmp_path, "terse", "Be terse.")

    comparison = build(primary, terse)

    assert comparison["baseline"] == "primary"
    assert comparison["bar"]["source"] == "suite"
    fallback = summary(comparison, "terse")
    assert fallback["changes"] == ["prompt"]  # same model, different prompt: attributable
    assert fallback["prompt_id"] != summary(comparison, "primary")["prompt_id"]
    vs = fallback["vs_baseline"]
    assert vs["regressions"] == ["hours"]  # the terse prompt drops the Sunday hours
    assert vs["improvements"] == []
    assert vs["status"] == "not_ready"  # 1 of 2 baseline-passing cases is far over 10%
    # The producer's own fields, not a test's guess at them.
    assert fallback["latency_p95_ms"] is not None
    assert summary(comparison, "primary")["pass_rate"] == 1.0
    assert comparison["split_cases"] == ["hours"]


async def test_a_critical_case_the_engine_marked_stays_critical_through_storage(tmp_path):
    primary = await run_stored_arm(tmp_path, "primary", "Answer fully.", baseline=True)

    arm = arm_from_evaluation(primary)

    assert arm.critical == frozenset({"refund"})
    assert arm.readiness == ReadinessBar(max_regression_rate=0.10)
