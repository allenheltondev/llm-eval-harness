"""Side-by-side comparison of one suite run against several models.

Each *arm* is an ordinary suite evaluation whose ``run_config`` names a
different model; nothing about how it ran, was scored or was stored is special.
:func:`compare` only reads their finished ``result`` documents, so it means the
same thing whichever lane produced them, and it can be pointed at evaluations
that were run separately.

Ranking is by what a suite is for, in order: pass rate, then mean judge score,
then estimated cost (cheaper first). Cost only orders arms that are equally
good; it never decides a winner. Two arms level on pass rate and mean score are
reported as a tie rather than a win, and a result is only as decisive as the
suite is large: with few cases or one repeat, a one-case gap is noise, which is
why every arm carries the suite's size next to its rate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

#: Case statuses that mean the arm answered and was scored, one way or the other.
_DECIDED = ("passed", "failed")


@dataclass(frozen=True)
class Arm:
    """One model's run of the suite, as :func:`compare` needs to see it."""

    provider: str
    model_id: str
    #: The stored evaluation, when the arm was run through history.
    evaluation_id: str | None
    #: The evaluation's terminal status (``completed``, ``error``, ``cancelled``).
    status: str
    #: The evaluation's ``result``; ``None`` when it produced none.
    result: dict[str, Any] | None

    @property
    def label(self) -> str:
        """What names this arm in a comparison: ``provider:model_id``."""
        return f"{self.provider}:{self.model_id}"


def compare(arms: list[Arm]) -> dict[str, Any]:
    """Compare finished suite evaluations, one per model.

    Returns ``arms`` (one summary each, in the order given), ``ranking`` (labels
    best first; only arms whose evaluation completed are ranked, because a
    cancelled or errored arm's partial numbers are not a fair basis), ``winner`` (the label, or
    ``None`` on a tie or when nothing is comparable), ``tied`` (the labels level
    at the top when there is no single winner), ``cases`` (one row per suite
    case with every arm's verdict) and ``split_cases`` (the ids the arms
    disagree on: the cases worth reading).
    """
    labels = [arm.label for arm in arms]
    if len(set(labels)) != len(labels):
        raise ValueError("every arm must be a different model")

    summaries = [_summarize(arm) for arm in arms]
    scored = [
        summary
        for summary in summaries
        if summary["status"] == "completed" and summary["pass_rate"] is not None
    ]
    scored.sort(key=_ranking_key)
    ranking = [summary["label"] for summary in scored]

    winner: str | None = None
    tied: list[str] = []
    if scored:
        top = _quality(scored[0])
        tied = [summary["label"] for summary in scored if _quality(summary) == top]
        if len(tied) == 1:
            winner, tied = tied[0], []

    cases = _case_rows(arms)
    return {
        "arms": summaries,
        "ranking": ranking,
        "winner": winner,
        "tied": tied,
        "cases": cases,
        "split_cases": [case["id"] for case in cases if case["agreement"] == "split"],
    }


def _summarize(arm: Arm) -> dict[str, Any]:
    result = arm.result or {}
    metrics = result.get("metrics") or {}
    cost = result.get("cost") or {}
    return {
        "label": arm.label,
        "provider": arm.provider,
        "model_id": arm.model_id,
        "evaluation_id": arm.evaluation_id,
        "status": arm.status,
        "score": result.get("score"),
        "grade": result.get("grade"),
        "pass_rate": metrics.get("pass_rate"),
        "cases_passed": metrics.get("cases_passed"),
        "cases_total": metrics.get("cases_total"),
        "cases_errored": metrics.get("cases_errored"),
        "repeats": (result.get("suite") or {}).get("repeats"),
        "cost_usd": cost.get("total_usd"),
        "budget_exhausted": bool(result.get("budget_exhausted")),
    }


def _quality(summary: dict[str, Any]) -> tuple[float, float]:
    """What makes one arm better than another: pass rate, then mean judge score."""
    return (summary["pass_rate"], summary["score"] if summary["score"] is not None else -1.0)


def _ranking_key(summary: dict[str, Any]) -> tuple[float, float, float]:
    pass_rate, score = _quality(summary)
    cost = summary["cost_usd"]
    return (-pass_rate, -score, math.inf if cost is None else cost)


def _case_rows(arms: list[Arm]) -> list[dict[str, Any]]:
    """Every case any arm ran, in the first arm's order, with each arm's verdict."""
    by_arm: dict[str, dict[str, dict[str, Any]]] = {
        arm.label: {case["id"]: case for case in (arm.result or {}).get("cases") or []}
        for arm in arms
    }
    order: list[str] = []
    for arm in arms:
        for case in (arm.result or {}).get("cases") or []:
            if case["id"] not in order:
                order.append(case["id"])

    rows: list[dict[str, Any]] = []
    for case_id in order:
        results: dict[str, dict[str, Any] | None] = {}
        for label, cases in by_arm.items():
            case = cases.get(case_id)
            results[label] = (
                None if case is None else {"status": case["status"], "score": case["score"]}
            )
        rows.append({"id": case_id, "results": results, "agreement": _agreement(results)})
    return rows


def _agreement(results: dict[str, dict[str, Any] | None]) -> str:
    """``all_passed`` / ``all_failed`` / ``split``, or ``incomplete`` when an arm is undecided."""
    statuses = [None if result is None else result["status"] for result in results.values()]
    if any(status not in _DECIDED for status in statuses):
        return "incomplete"
    if all(status == "passed" for status in statuses):
        return "all_passed"
    if all(status == "failed" for status in statuses):
        return "all_failed"
    return "split"


def arm_from_evaluation(evaluation: dict[str, Any]) -> Arm:
    """The :class:`Arm` for a stored suite evaluation (an ``EvaluationDetail`` as a dict).

    The model is read from the suite as it was stored, so the arm names what
    actually ran, not what a later edit of a file says.
    """
    suite = (evaluation.get("config") or {}).get("suite") or {}
    run_config = suite.get("run_config") or {}
    return Arm(
        provider=str(run_config.get("provider") or "bedrock"),
        model_id=str(run_config.get("model_id") or "unknown"),
        evaluation_id=evaluation.get("id"),
        status=str(evaluation.get("status")),
        result=evaluation.get("result"),
    )


def suite_differences(evaluations: list[dict[str, Any]]) -> list[str]:
    """Case ids on which the evaluations' stored suites disagree; empty when they match.

    Two evaluations are the same suite when they have the same cases (by id) with
    the same ``input``. Models are meant to differ; the questions are not.
    """
    inputs: list[dict[str, str]] = []
    for evaluation in evaluations:
        cases = ((evaluation.get("config") or {}).get("suite") or {}).get("cases") or []
        inputs.append({str(case.get("id")): str(case.get("input")) for case in cases})
    every = {case_id for mapping in inputs for case_id in mapping}
    return sorted(
        case_id for case_id in every if len({mapping.get(case_id) for mapping in inputs}) > 1
    )
