"""Is a candidate ready to stand in for a baseline?

Ranking models says which is best. A fallback question is different: if the
primary is unavailable, will this variant break things the primary handles?
That is a paired question. Both ran the same cases, so the evidence is the
cases they differ on:

*regression*
    the baseline passes it and the candidate does not.
*improvement*
    the candidate passes it and the baseline does not.

What is asked of a candidate is the *regression rate*: the fraction of the
cases the baseline passes that it now fails. Two things keep that honest.

**A small suite cannot certify anything.** Zero regressions in 10 cases does
not mean the true rate is zero: it is consistent with a rate of about 26%.
:func:`upper_bound` is the exact one-sided 95% bound on the rate given what was
seen, and a candidate is only ``ready`` when even that bound is inside the
limit. When the point estimate passes but the bound does not, the verdict is
``inconclusive`` and says how many cases would settle it
(:func:`cases_needed`), instead of quietly certifying on thin evidence.

**Some cases are not negotiable.** A ``critical`` case that regresses fails the
candidate outright, whatever the rate.

Everything here is a pure function of pass/fail verdicts, so it means the same
thing for any lane and can be tested exhaustively.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: One-sided confidence of the bound on the regression rate.
CONFIDENCE = 0.95

#: The regression rate allowed when a suite states no bar of its own.
DEFAULT_MAX_REGRESSION_RATE = 0.10

Status = Literal["ready", "not_ready", "inconclusive"]


class ReadinessBar(BaseModel):
    """What a candidate must meet to stand in for the baseline.

    Only ``max_regression_rate`` always applies; the ratios apply when set.
    Ratios are candidate over baseline, so ``1.5`` allows a candidate 50% slower
    (or dearer) than the baseline.
    """

    model_config = ConfigDict(extra="forbid")

    #: Of the cases the baseline passes, the fraction the candidate may fail.
    max_regression_rate: float = Field(default=DEFAULT_MAX_REGRESSION_RATE, ge=0.0, le=1.0)
    #: Largest allowed ratio of the candidate's p95 latency to the baseline's.
    max_latency_ratio: float | None = Field(default=None, gt=0.0)
    #: Largest allowed ratio of the candidate's estimated cost to the baseline's.
    max_cost_ratio: float | None = Field(default=None, gt=0.0)


@dataclass(frozen=True)
class Paired:
    """How a candidate's case verdicts line up with the baseline's."""

    #: The baseline passes, the candidate does not (or did not run it).
    regressions: list[str]
    #: The candidate passes, the baseline does not.
    improvements: list[str]
    #: Both pass.
    both_pass: int
    #: Neither passes.
    both_fail: int

    @property
    def baseline_passed(self) -> int:
        """Cases the baseline passes: the population a regression can happen in."""
        return len(self.regressions) + self.both_pass


def pair(baseline: Mapping[str, bool], candidate: Mapping[str, bool]) -> Paired:
    """Line the candidate's verdicts up against the baseline's, case by case.

    A case the candidate has no verdict for did not pass: an unanswered case is
    a case the fallback failed to handle.
    """
    regressions: list[str] = []
    improvements: list[str] = []
    both_pass = both_fail = 0
    for case_id in dict.fromkeys([*baseline, *candidate]):
        base, cand = baseline.get(case_id, False), candidate.get(case_id, False)
        if base and cand:
            both_pass += 1
        elif base:
            regressions.append(case_id)
        elif cand:
            improvements.append(case_id)
        else:
            both_fail += 1
    return Paired(regressions, improvements, both_pass, both_fail)


def _binomial_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p)."""
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))


def upper_bound(regressions: int, population: int, confidence: float = CONFIDENCE) -> float:
    """The exact one-sided upper confidence bound on a rate seen ``regressions``/``population``.

    The largest true rate still consistent, at ``confidence``, with seeing this
    few regressions: the Clopper-Pearson bound. With nothing to learn from
    (``population`` of 0) or every case regressed, that is ``1.0``.
    """
    if population <= 0 or regressions >= population:
        return 1.0
    alpha = 1.0 - confidence
    low, high = regressions / population, 1.0
    for _ in range(64):
        mid = (low + high) / 2
        if _binomial_cdf(regressions, population, mid) > alpha:
            low = mid
        else:
            high = mid
    return high


def cases_needed(max_rate: float, confidence: float = CONFIDENCE) -> int | None:
    """How many baseline-passing cases certify ``max_rate`` when none regress.

    ``None`` when no finite suite can: a limit of zero can never be certified,
    because a true rate just above zero always remains possible.
    """
    if max_rate <= 0:
        return None
    if max_rate >= 1:
        return 1
    return math.ceil(math.log(1.0 - confidence) / math.log(1.0 - max_rate))


def sign_test_p(regressions: int, improvements: int) -> float:
    """Two-sided exact sign-test p-value for "no difference" between the two.

    Only the cases the two differ on carry information. With none, or an even
    split, there is nothing to distinguish them (``1.0``).
    """
    total = regressions + improvements
    if total == 0:
        return 1.0
    tail = sum(math.comb(total, i) for i in range(min(regressions, improvements) + 1))
    return min(1.0, 2 * tail / 2**total)


@dataclass
class Assessment:
    """A candidate measured against the baseline and the bar."""

    status: Status
    reasons: list[str]
    regressions: list[str]
    improvements: list[str]
    #: Cases the baseline passes.
    baseline_passed: int
    #: Regressions over ``baseline_passed``; ``None`` when the baseline passes nothing.
    regression_rate: float | None
    #: Exact 95% upper bound on the true regression rate.
    regression_upper_bound: float
    #: Baseline-passing cases needed to certify the limit with no regressions.
    cases_needed: int | None
    sign_test_p: float
    #: Critical cases that regressed.
    critical_regressions: list[str] = field(default_factory=list)


def _percent(value: float) -> str:
    return f"{value * 100:.0f}%"


def assess(
    paired: Paired,
    bar: ReadinessBar,
    *,
    completed: bool = True,
    status_text: str = "completed",
    critical: Iterable[str] = (),
    latency_ratio: float | None = None,
    cost_ratio: float | None = None,
) -> Assessment:
    """Judge one candidate: ``not_ready`` beats ``inconclusive`` beats ``ready``.

    A candidate is ``not_ready`` when the evidence already shows it falls short:
    it did not finish, a critical case regressed, or it broke more than the
    limit, ran slower, or cost more than allowed. Otherwise it is ``ready`` only
    if the evidence is strong enough to *rule out* a regression rate over the
    limit; if the suite is too small for that it is ``inconclusive``.
    """
    critical_ids = set(critical)
    critical_regressions = [case for case in paired.regressions if case in critical_ids]
    population = paired.baseline_passed
    rate = len(paired.regressions) / population if population else None
    bound = upper_bound(len(paired.regressions), population)
    needed = cases_needed(bar.max_regression_rate)

    failures: list[str] = []
    if not completed:
        failures.append(f"its evaluation did not complete ({status_text})")
    if critical_regressions:
        failures.append(f"critical case(s) regressed: {', '.join(critical_regressions)}")
    if rate is not None and rate > bar.max_regression_rate:
        failures.append(
            f"{len(paired.regressions)} of the {population} cases the baseline passes now fail "
            f"({_percent(rate)}), over the {_percent(bar.max_regression_rate)} limit"
        )
    if (
        bar.max_latency_ratio is not None
        and latency_ratio is not None
        and latency_ratio > bar.max_latency_ratio
    ):
        failures.append(
            f"p95 latency is {latency_ratio:.1f}x the baseline's, over the "
            f"{bar.max_latency_ratio:g}x limit"
        )
    if (
        bar.max_cost_ratio is not None
        and cost_ratio is not None
        and cost_ratio > bar.max_cost_ratio
    ):
        failures.append(
            f"estimated cost is {cost_ratio:.1f}x the baseline's, over the "
            f"{bar.max_cost_ratio:g}x limit"
        )

    unknowns: list[str] = []
    if bar.max_latency_ratio is not None and latency_ratio is None:
        unknowns.append("no latency data to check the latency limit against")
    if bar.max_cost_ratio is not None and cost_ratio is None:
        unknowns.append("no price for one of the models, so the cost limit cannot be checked")
    if population == 0:
        unknowns.append("the baseline passes no cases, so there is nothing to regress from")
    elif bound > bar.max_regression_rate:
        seen = len(paired.regressions)
        unknowns.append(
            f"{seen} regression(s) in {population} baseline-passing cases can only rule out a "
            f"regression rate above {_percent(bound)}; the limit is "
            f"{_percent(bar.max_regression_rate)}"
            + (f" (about {needed} such cases would settle it)" if needed and seen == 0 else "")
        )

    if failures:
        status: Status = "not_ready"
        reasons = failures
    elif unknowns:
        status, reasons = "inconclusive", unknowns
    else:
        status = "ready"
        reasons = [
            f"{len(paired.regressions)} regression(s) in {population} baseline-passing cases; "
            f"a regression rate above {_percent(bound)} is ruled out"
        ]

    return Assessment(
        status=status,
        reasons=reasons,
        regressions=paired.regressions,
        improvements=paired.improvements,
        baseline_passed=population,
        regression_rate=rate,
        regression_upper_bound=bound,
        cases_needed=needed,
        sign_test_p=sign_test_p(len(paired.regressions), len(paired.improvements)),
        critical_regressions=critical_regressions,
    )
