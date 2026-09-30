"""Fallback readiness: the paired statistics and the verdict rules."""

from __future__ import annotations

import math

import pytest

from nimbus.evals import readiness
from nimbus.evals.readiness import (
    ReadinessBar,
    assess,
    cases_needed,
    pair,
    sign_test_p,
    upper_bound,
)

# --------------------------------------------------------------------------- #
# Pairing
# --------------------------------------------------------------------------- #


def test_pairing_finds_the_cases_the_candidate_broke_and_fixed():
    baseline = {"a": True, "b": True, "c": False, "d": False}
    candidate = {"a": True, "b": False, "c": True, "d": False}

    paired = pair(baseline, candidate)

    assert paired.regressions == ["b"]
    assert paired.improvements == ["c"]
    assert (paired.both_pass, paired.both_fail) == (1, 1)
    assert paired.baseline_passed == 2  # a and b: where a regression can happen


def test_a_case_the_candidate_never_answered_counts_as_a_regression():
    paired = pair({"a": True, "b": True}, {"a": True})

    assert paired.regressions == ["b"]


def test_a_case_only_the_candidate_has_is_an_improvement_only_if_it_passed():
    assert pair({"a": True}, {"a": True, "extra": True}).improvements == ["extra"]
    assert pair({"a": True}, {"a": True, "extra": False}).improvements == []


def test_pairing_nothing_finds_nothing():
    paired = pair({}, {})

    assert (paired.regressions, paired.improvements, paired.baseline_passed) == ([], [], 0)


# --------------------------------------------------------------------------- #
# The bound on the regression rate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("population", [1, 5, 10, 29, 100])
def test_with_no_regressions_the_bound_has_a_closed_form(population):
    # P(no regressions | rate p) = (1 - p)^n, so the bound solves (1 - p)^n = 0.05.
    assert upper_bound(0, population) == pytest.approx(1 - 0.05 ** (1 / population), abs=1e-9)


@pytest.mark.parametrize(("regressions", "population"), [(0, 10), (1, 10), (3, 20), (7, 40)])
def test_the_bound_is_where_seeing_this_few_regressions_becomes_a_5_percent_event(
    regressions, population
):
    bound = upper_bound(regressions, population)

    assert readiness._binomial_cdf(regressions, population, bound) == pytest.approx(0.05, abs=1e-9)


def test_the_bound_is_never_below_what_was_seen():
    for regressions in range(0, 11):
        assert upper_bound(regressions, 10) >= regressions / 10


def test_more_regressions_raise_the_bound_and_more_cases_lower_it():
    assert upper_bound(0, 20) < upper_bound(1, 20) < upper_bound(2, 20)
    assert upper_bound(1, 100) < upper_bound(1, 20) < upper_bound(1, 5)


def test_a_small_suite_cannot_rule_much_out():
    # Ten clean cases still allow a true regression rate of about a quarter.
    assert upper_bound(0, 10) == pytest.approx(0.2589, abs=1e-4)


@pytest.mark.parametrize(("regressions", "population"), [(0, 0), (5, 5), (9, 4)])
def test_with_nothing_to_learn_from_or_everything_broken_the_bound_is_one(regressions, population):
    assert upper_bound(regressions, population) == 1.0


def test_cases_needed_is_the_smallest_suite_that_certifies_the_limit():
    needed = cases_needed(0.10)

    assert needed == 29
    assert upper_bound(0, needed) <= 0.10 < upper_bound(0, needed - 1)


@pytest.mark.parametrize("limit", [0.02, 0.05, 0.2, 0.5])
def test_cases_needed_is_minimal_for_any_limit(limit):
    needed = cases_needed(limit)

    assert needed is not None
    assert upper_bound(0, needed) <= limit
    assert needed == 1 or upper_bound(0, needed - 1) > limit


def test_a_limit_of_zero_can_never_be_certified_and_one_needs_nothing():
    assert cases_needed(0.0) is None
    assert cases_needed(1.0) == 1


# --------------------------------------------------------------------------- #
# The sign test
# --------------------------------------------------------------------------- #


def test_no_differing_cases_means_nothing_to_tell_them_apart():
    assert sign_test_p(0, 0) == 1.0


def test_an_even_split_is_no_evidence_of_a_difference():
    assert sign_test_p(3, 3) == 1.0


@pytest.mark.parametrize(
    ("regressions", "improvements", "expected"),
    [(0, 5, 2 / 32), (0, 8, 2 / 256), (1, 9, 2 * (1 + 10) / 1024), (5, 0, 2 / 32)],
)
def test_the_sign_test_is_the_exact_two_sided_binomial_p(regressions, improvements, expected):
    assert sign_test_p(regressions, improvements) == pytest.approx(expected)


def test_the_sign_test_is_symmetric_and_capped_at_one():
    assert sign_test_p(2, 7) == sign_test_p(7, 2)
    assert sign_test_p(1, 1) == 1.0


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #

BAR = ReadinessBar(max_regression_rate=0.10)


def paired_with(regressions: int, keepers: int, improvements: int = 0):
    """A pairing with this many regressions, cases both pass, and improvements."""
    baseline = {f"r{i}": True for i in range(regressions)}
    baseline |= {f"k{i}": True for i in range(keepers)}
    baseline |= {f"i{i}": False for i in range(improvements)}
    candidate = {f"r{i}": False for i in range(regressions)}
    candidate |= {f"k{i}": True for i in range(keepers)}
    candidate |= {f"i{i}": True for i in range(improvements)}
    return pair(baseline, candidate)


def test_a_large_clean_suite_certifies_the_candidate():
    result = assess(paired_with(0, 40), BAR)

    assert result.status == "ready"
    assert result.regression_rate == 0.0
    assert result.regression_upper_bound <= 0.10


def test_a_small_clean_suite_is_inconclusive_and_says_how_many_cases_would_settle_it():
    result = assess(paired_with(0, 10), BAR)

    assert result.status == "inconclusive"
    assert result.regression_rate == 0.0  # the point estimate is perfect...
    assert result.regression_upper_bound > 0.10  # ...but 10 cases cannot certify it
    assert "about 29 such cases" in result.reasons[0]


def test_exactly_enough_clean_cases_is_ready_and_one_fewer_is_not():
    assert assess(paired_with(0, 29), BAR).status == "ready"
    assert assess(paired_with(0, 28), BAR).status == "inconclusive"


def test_regressions_over_the_limit_fail_the_candidate_outright():
    result = assess(paired_with(4, 16), BAR)  # 4 of 20 = 20%

    assert result.status == "not_ready"
    assert "4 of the 20 cases the baseline passes now fail (20%)" in result.reasons[0]
    assert result.regressions == ["r0", "r1", "r2", "r3"]


def test_the_limit_is_inclusive():
    result = assess(paired_with(3, 27), ReadinessBar(max_regression_rate=0.10))  # exactly 10%

    assert result.status != "not_ready"


def test_a_few_regressions_in_a_big_suite_can_still_be_inconclusive():
    # 2 of 60 is 3%, under the limit, but the bound (about 10.7%) does not clear it.
    result = assess(paired_with(2, 58), BAR)

    assert result.status == "inconclusive"
    assert result.regression_upper_bound > 0.10


def test_improvements_do_not_offset_regressions():
    result = assess(paired_with(4, 16, improvements=10), BAR)

    assert result.status == "not_ready"
    assert len(result.improvements) == 10


def test_a_critical_regression_fails_even_a_tiny_rate():
    result = assess(paired_with(1, 99), BAR, critical={"r0"})

    assert result.status == "not_ready"
    assert result.critical_regressions == ["r0"]
    assert "critical case(s) regressed: r0" in result.reasons[0]


def test_a_critical_case_that_did_not_regress_changes_nothing():
    result = assess(paired_with(0, 40), BAR, critical={"k3"})

    assert result.status == "ready"
    assert result.critical_regressions == []


def test_a_candidate_that_did_not_complete_is_never_ready():
    result = assess(paired_with(0, 100), BAR, completed=False, status_text="cancelled")

    assert result.status == "not_ready"
    assert "did not complete (cancelled)" in result.reasons[0]


def test_slower_than_the_limit_fails_and_within_it_passes():
    bar = ReadinessBar(max_regression_rate=0.10, max_latency_ratio=1.5)

    slow = assess(paired_with(0, 40), bar, latency_ratio=2.3)
    fine = assess(paired_with(0, 40), bar, latency_ratio=1.5)

    assert slow.status == "not_ready"
    assert "p95 latency is 2.3x the baseline's, over the 1.5x limit" in slow.reasons[0]
    assert fine.status == "ready"


def test_dearer_than_the_limit_fails():
    bar = ReadinessBar(max_regression_rate=0.10, max_cost_ratio=2.0)

    result = assess(paired_with(0, 40), bar, cost_ratio=3.1)

    assert result.status == "not_ready"
    assert "estimated cost is 3.1x the baseline's, over the 2x limit" in result.reasons[0]


def test_a_limit_that_cannot_be_checked_is_inconclusive_not_passed():
    bar = ReadinessBar(max_regression_rate=0.10, max_latency_ratio=1.5, max_cost_ratio=2.0)

    result = assess(paired_with(0, 40), bar, latency_ratio=None, cost_ratio=None)

    assert result.status == "inconclusive"
    assert any("no latency data" in reason for reason in result.reasons)
    assert any("no price" in reason for reason in result.reasons)


def test_limits_that_are_not_set_are_not_checked():
    result = assess(paired_with(0, 40), BAR, latency_ratio=99.0, cost_ratio=99.0)

    assert result.status == "ready"


def test_when_the_baseline_passes_nothing_there_is_nothing_to_certify():
    result = assess(pair({"a": False}, {"a": True}), BAR)

    assert result.status == "inconclusive"
    assert result.regression_rate is None
    assert "baseline passes no cases" in result.reasons[0]


def test_a_failure_outranks_an_inconclusive_finding():
    bar = ReadinessBar(max_regression_rate=0.10, max_latency_ratio=1.5)

    result = assess(paired_with(0, 10), bar, latency_ratio=3.0)  # too few cases AND too slow

    assert result.status == "not_ready"


def test_every_failure_is_reported_not_just_the_first():
    bar = ReadinessBar(max_regression_rate=0.10, max_latency_ratio=1.5, max_cost_ratio=2.0)

    result = assess(paired_with(5, 15), bar, latency_ratio=2.0, cost_ratio=3.0, critical={"r1"})

    assert len(result.reasons) == 4


def test_the_sign_test_rides_along_with_the_verdict():
    result = assess(paired_with(0, 5, improvements=6), BAR)

    assert result.sign_test_p == pytest.approx(2 / 64)


def test_a_zero_limit_can_only_ever_fail_or_be_inconclusive():
    strict = ReadinessBar(max_regression_rate=0.0)

    assert assess(paired_with(0, 500), strict).status == "inconclusive"
    assert assess(paired_with(1, 499), strict).status == "not_ready"


def test_the_default_bar_allows_a_tenth():
    assert ReadinessBar().max_regression_rate == 0.10
    assert ReadinessBar().max_latency_ratio is None


@pytest.mark.parametrize("bad", [{"max_regression_rate": 1.5}, {"max_latency_ratio": 0}, {"x": 1}])
def test_a_nonsense_bar_is_refused(bad):
    with pytest.raises(ValueError):
        ReadinessBar(**bad)


def test_the_bound_math_agrees_with_a_brute_force_search():
    """An independent check on the bisection: scan rates instead of bisecting."""
    regressions, population = 2, 30
    scan = next(
        p / 100_000
        for p in range(1, 100_000)
        if readiness._binomial_cdf(regressions, population, p / 100_000) <= 0.05
    )

    assert upper_bound(regressions, population) == pytest.approx(scan, abs=2e-5)
    assert math.isfinite(scan)
