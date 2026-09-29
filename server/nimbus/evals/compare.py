"""Side-by-side comparison of one suite run as several arms.

An *arm* is a variant of a suite run: a model, the prompt it was given, and its
inference settings. Each arm is an ordinary suite evaluation; nothing about how
it ran, was scored or was stored is special. :func:`compare` only reads their
finished ``result`` documents, so it means the same thing whichever lane
produced them, and it can be pointed at evaluations that were run separately.

Ranking
-------
By what a suite is for, in order: pass rate, then mean judge score, then
estimated cost (cheaper first). Cost only orders arms that are equally good; it
never decides a winner. Two arms level on pass rate and mean score are a tie,
not a win, and only arms whose evaluation completed are ranked: a cancelled
arm's partial numbers are not a fair basis.

Against a baseline
------------------
Ranking answers "which is best". A fallback question is "if the baseline is
unavailable, what breaks?". With a baseline, every other arm is measured against
it (:mod:`nimbus.evals.readiness`): the cases the baseline passes that the arm
fails, whether a suite this size can rule out a regression rate over the limit,
and the latency and cost ratios. The verdict is ``ready``, ``not_ready`` or
``inconclusive``; a suite too small to be sure says so rather than passing.

What the numbers cannot tell you
--------------------------------
Two arms that differ in both model and prompt cannot be told apart by one
comparison: a gain might be either. Each arm therefore lists what it changes
from the baseline, and when the arms form a complete models-by-prompts grid
(``ArmTag.axes``) the comparison reports each axis's *main effect*: the mean pass
rate per value of the axis, and which axis moves it more. It also warns about
the ways a result can mislead: a single repeat, a small suite, a judge from the
same model family as an arm, and tools exercised but never asserted on.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from nimbus.errors import AppError
from nimbus.evals import readiness
from nimbus.evals.readiness import ReadinessBar
from nimbus.providers import PROVIDERS

#: Case statuses that mean the arm answered and was scored, one way or the other.
_DECIDED = ("passed", "failed")

#: Fewer cases than this and a gap of a case or two is within noise.
SMALL_SUITE_CASES = 20

#: One axis moves the pass rate "more" than another only if its spread is this many times larger.
DOMINANCE_RATIO = 1.5

_REGION_PREFIX = re.compile(r"^(global|us-gov|us|eu|apac|jp|au|ca|sa|me|af|mx)\.")
_VENDOR_PREFIXES = (
    "anthropic.",
    "amazon.",
    "meta.",
    "mistral.",
    "cohere.",
    "openai.",
    "ai21.",
    "deepseek.",
    "qwen.",
    "google.",
)


class ComparisonError(AppError):
    """A set of evaluations that cannot be compared, and why."""

    status_code = 400
    code = "compare_failed"


def model_family(model_id: str) -> str:
    """A coarse family name for a model id: ``claude``, ``nova``, ``gpt``, ``llama``…

    Enough to notice that a judge and an arm are cousins. Region prefixes
    (``us.``), vendor prefixes (``anthropic.``) and a leading ``vendor/`` are
    ignored, then the leading letters are the family.
    """
    name = model_id.lower().rsplit("/", 1)[-1]
    name = _REGION_PREFIX.sub("", name)
    for prefix in _VENDOR_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    match = re.match(r"[a-z]+", name)
    return match.group(0) if match else name


def _model_of(judge: str) -> str:
    """The model id in a ``provider:model`` label; a bare id, or one with its own colons, as is.

    Only a *known* provider is stripped: model ids contain colons themselves
    (``llama3.1:8b``, ``amazon.nova-pro-v1:0``).
    """
    provider, separator, rest = judge.partition(":")
    return rest if separator and provider in PROVIDERS else judge


def prompt_id(system_prompt: str | None) -> str | None:
    """A short stable id for a system prompt, so arms with the same prompt are recognisable."""
    if not system_prompt:
        return None
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class Arm:
    """One variant's run of the suite, as :func:`compare` needs to see it."""

    provider: str
    model_id: str
    #: The stored evaluation, when the arm was run through history.
    evaluation_id: str | None
    #: The evaluation's terminal status (``completed``, ``error``, ``cancelled``).
    status: str
    #: The evaluation's ``result``; ``None`` when it produced none.
    result: dict[str, Any] | None
    #: The arm's name (``ArmTag.name``); unnamed arms are labelled by their model.
    name: str | None = None
    #: The arm was tagged as the baseline.
    baseline: bool = False
    #: Where the arm sits in a models-by-prompts grid.
    axes: dict[str, str] = field(default_factory=dict)
    #: The run config the suite ran with, to say what differs between arms.
    run_config: dict[str, Any] = field(default_factory=dict)
    #: The bar stored with the evaluation, if it had one.
    readiness: ReadinessBar | None = None
    #: Ids of the suite's ``critical`` cases.
    critical: frozenset[str] = frozenset()
    #: Model ids (``provider:model``) of the judges that graded the arm.
    judges: tuple[str, ...] = ()
    #: The run config gives the model tools.
    uses_tools: bool = False
    #: Some case asserts something about tool calls.
    asserts_tools: bool = False

    @property
    def label(self) -> str:
        """What names this arm in a comparison: its name, else ``provider:model_id``."""
        return self.name or f"{self.provider}:{self.model_id}"


# --------------------------------------------------------------------------- #
# The comparison
# --------------------------------------------------------------------------- #


def compare(
    arms: list[Arm], *, baseline: str | None = None, bar: ReadinessBar | None = None
) -> dict[str, Any]:
    """Compare finished suite evaluations, one per arm.

    Returns ``arms`` (one summary each, in the order given), ``ranking`` (labels
    best first; only arms whose evaluation completed), ``winner`` (the label, or
    ``None`` on a tie or when nothing is comparable), ``tied``, ``cases`` (one
    row per suite case with every arm's verdict) and ``split_cases`` (the ids the
    arms disagree on).

    With a baseline (``baseline`` names an arm's label; else the arm tagged as
    one) it also returns ``baseline``, ``bar`` and, on each other arm,
    ``vs_baseline``: regressions, improvements, bounds and the verdict. ``effects``
    appears when the arms form a complete models-by-prompts grid, and
    ``warnings`` lists the ways the result could mislead.
    """
    labels = [arm.label for arm in arms]
    if len(set(labels)) != len(labels):
        raise ValueError(
            "every arm must have a different name"
            if any(arm.name for arm in arms)
            else "every arm must be a different model"
        )

    baseline_arm = _resolve_baseline(arms, baseline)
    used_bar, bar_source = _resolve_bar(arms, baseline_arm, bar)

    summaries = [_summarize(arm, baseline_arm) for arm in arms]
    by_label = {summary["label"]: summary for summary in summaries}
    if baseline_arm is not None:
        for arm in arms:
            if arm is not baseline_arm:
                by_label[arm.label]["vs_baseline"] = _against_baseline(
                    baseline_arm, arm, used_bar, by_label
                )

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
    effects = _effects(arms, by_label)
    comparison: dict[str, Any] = {
        "arms": summaries,
        "ranking": ranking,
        "winner": winner,
        "tied": tied,
        "cases": cases,
        "split_cases": [case["id"] for case in cases if case["agreement"] == "split"],
        "baseline": baseline_arm.label if baseline_arm else None,
        "warnings": _warnings(arms, by_label, baseline_arm, effects is not None),
    }
    if baseline_arm is not None:
        comparison["bar"] = {**used_bar.model_dump(), "source": bar_source}
    if effects is not None:
        comparison["effects"] = effects
    return comparison


def _resolve_baseline(arms: list[Arm], requested: str | None) -> Arm | None:
    """The requested arm, else the one tagged as baseline, else none."""
    if requested is not None:
        for arm in arms:
            if requested in (arm.label, arm.evaluation_id):
                return arm
        raise ValueError(f"no arm matches the baseline {requested!r}")
    return next((arm for arm in arms if arm.baseline), None)


def _resolve_bar(
    arms: list[Arm], baseline_arm: Arm | None, requested: ReadinessBar | None
) -> tuple[ReadinessBar, str]:
    """The bar to judge by, and where it came from: the caller, the suite, or the default."""
    if requested is not None:
        return requested, "requested"
    ordered = ([baseline_arm] if baseline_arm else []) + arms
    for arm in ordered:
        if arm.readiness is not None:
            return arm.readiness, "suite"
    return ReadinessBar(), "default"


def _summarize(arm: Arm, baseline_arm: Arm | None) -> dict[str, Any]:
    result = arm.result or {}
    metrics = result.get("metrics") or {}
    cost = result.get("cost") or {}
    latency = metrics.get("latency_ms") or {}
    return {
        "label": arm.label,
        "name": arm.name,
        "provider": arm.provider,
        "model_id": arm.model_id,
        "prompt_id": prompt_id(arm.run_config.get("system_prompt")),
        "evaluation_id": arm.evaluation_id,
        "status": arm.status,
        "baseline": baseline_arm is arm,
        "score": result.get("score"),
        "grade": result.get("grade"),
        "pass_rate": metrics.get("pass_rate"),
        "cases_passed": metrics.get("cases_passed"),
        "cases_total": metrics.get("cases_total"),
        "cases_errored": metrics.get("cases_errored"),
        "assertions_failed": metrics.get("assertions_failed"),
        "repeats": (result.get("suite") or {}).get("repeats"),
        "cost_usd": cost.get("total_usd"),
        "latency_p50_ms": latency.get("p50"),
        "latency_p95_ms": latency.get("p95"),
        "budget_exhausted": bool(result.get("budget_exhausted")),
        "changes": _changes(baseline_arm, arm) if baseline_arm and baseline_arm is not arm else [],
    }


def _quality(summary: dict[str, Any]) -> tuple[float, float]:
    """What makes one arm better than another: pass rate, then mean judge score."""
    return (summary["pass_rate"], summary["score"] if summary["score"] is not None else -1.0)


def _ranking_key(summary: dict[str, Any]) -> tuple[float, float, float]:
    pass_rate, score = _quality(summary)
    cost = summary["cost_usd"]
    return (-pass_rate, -score, math.inf if cost is None else cost)


def _changes(baseline_arm: Arm, arm: Arm) -> list[str]:
    """What this arm changes from the baseline: ``model``, ``prompt``, ``inference``, ``tools``."""
    base, this = baseline_arm.run_config, arm.run_config
    changes: list[str] = []
    if (baseline_arm.provider, baseline_arm.model_id) != (arm.provider, arm.model_id):
        changes.append("model")
    if (base.get("system_prompt") or "") != (this.get("system_prompt") or ""):
        changes.append("prompt")
    if (base.get("inference") or {}) != (this.get("inference") or {}):
        changes.append("inference")
    tool_keys = ("toolset", "mcp_servers", "max_tool_iterations")
    if any(base.get(key) != this.get(key) for key in tool_keys):
        changes.append("tools")
    return changes


# --------------------------------------------------------------------------- #
# Against the baseline
# --------------------------------------------------------------------------- #


def _passed_by_case(arm: Arm) -> dict[str, bool]:
    return {
        case["id"]: case["status"] == "passed" for case in (arm.result or {}).get("cases") or []
    }


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _delta(candidate: float | None, baseline: float | None) -> float | None:
    if candidate is None or baseline is None:
        return None
    return candidate - baseline


def _against_baseline(
    baseline_arm: Arm, arm: Arm, bar: ReadinessBar, summaries: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """One arm measured against the baseline, or ``None`` when the baseline has no result."""
    if baseline_arm.status != "completed" or not baseline_arm.result:
        return None
    base, cand = summaries[baseline_arm.label], summaries[arm.label]
    paired = readiness.pair(_passed_by_case(baseline_arm), _passed_by_case(arm))
    assessment = readiness.assess(
        paired,
        bar,
        completed=arm.status == "completed",
        status_text=arm.status,
        critical=baseline_arm.critical | arm.critical,
        latency_ratio=_ratio(cand["latency_p95_ms"], base["latency_p95_ms"]),
        cost_ratio=_ratio(cand["cost_usd"], base["cost_usd"]),
    )
    return {
        "status": assessment.status,
        "reasons": assessment.reasons,
        "regressions": assessment.regressions,
        "improvements": assessment.improvements,
        "critical_regressions": assessment.critical_regressions,
        "baseline_passed": assessment.baseline_passed,
        "regression_rate": assessment.regression_rate,
        "regression_upper_bound": assessment.regression_upper_bound,
        "cases_needed": assessment.cases_needed,
        "sign_test_p": assessment.sign_test_p,
        "pass_rate_delta": _delta(cand["pass_rate"], base["pass_rate"]),
        "score_delta": _delta(cand["score"], base["score"]),
        "latency_ratio": _ratio(cand["latency_p95_ms"], base["latency_p95_ms"]),
        "cost_ratio": _ratio(cand["cost_usd"], base["cost_usd"]),
        "changes": cand["changes"],
    }


# --------------------------------------------------------------------------- #
# Which axis moves the score
# --------------------------------------------------------------------------- #


def _effects(arms: list[Arm], summaries: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Main effects of each axis, when the arms are a complete grid of completed runs.

    For each axis (``model``, ``prompt``…) and each value on it: the mean pass
    rate over the arms with that value. The *spread* of an axis is the gap
    between its best and worst value, and the axis with the larger spread is
    what moves the pass rate more. Interactions (a prompt that only helps one
    model) are not separated out, and a grid with any arm missing or unfinished
    reports nothing rather than a biased average.
    """
    if len(arms) < 4 or not all(arm.axes for arm in arms):
        return None
    axis_names = sorted(arms[0].axes)
    if any(sorted(arm.axes) != axis_names for arm in arms):
        return None
    values = {name: sorted({arm.axes[name] for arm in arms}) for name in axis_names}
    if any(len(options) < 2 for options in values.values()):
        return None
    cells = {tuple(arm.axes[name] for name in axis_names) for arm in arms}
    expected = set(itertools.product(*(values[name] for name in axis_names)))
    if cells != expected or len(cells) != len(arms):
        return None
    rates = {arm.label: summaries[arm.label]["pass_rate"] for arm in arms}
    if any(
        summaries[arm.label]["status"] != "completed" or rates[arm.label] is None for arm in arms
    ):
        return None

    axes: dict[str, list[dict[str, Any]]] = {}
    spread: dict[str, float] = {}
    for name in axis_names:
        entries = []
        for value in values[name]:
            members = [rates[arm.label] for arm in arms if arm.axes[name] == value]
            entries.append(
                {
                    "value": value,
                    "mean_pass_rate": sum(members) / len(members),
                    "arms": len(members),
                }
            )
        axes[name] = entries
        means = [entry["mean_pass_rate"] for entry in entries]
        spread[name] = max(means) - min(means)

    ordered = sorted(spread.values(), reverse=True)
    dominant: str | None = None
    if ordered[0] > 0 and (len(ordered) == 1 or ordered[0] >= DOMINANCE_RATIO * ordered[1]):
        dominant = max(spread, key=lambda name: spread[name])
    return {"axes": axes, "spread": spread, "dominant": dominant}


# --------------------------------------------------------------------------- #
# Ways a result can mislead
# --------------------------------------------------------------------------- #


def _warnings(
    arms: list[Arm],
    summaries: dict[str, dict[str, Any]],
    baseline_arm: Arm | None,
    has_effects: bool,
) -> list[dict[str, Any]]:
    """Things a reader should know before trusting the comparison."""
    warnings: list[dict[str, Any]] = []

    def warn(code: str, message: str, labels: list[str] | None = None) -> None:
        warnings.append({"code": code, "message": message, "arms": labels or []})

    completed = [arm for arm in arms if summaries[arm.label]["status"] == "completed"]

    once = [arm.label for arm in completed if summaries[arm.label]["repeats"] == 1]
    if once:
        warn(
            "single_repeat",
            "Each case ran once, so run-to-run variance is not measured: a case that passes "
            "only some of the time can look solid. Raise `repeats` to expose it.",
            once,
        )

    totals = [summaries[arm.label]["cases_total"] for arm in completed]
    if totals and min(t for t in totals if t is not None) < SMALL_SUITE_CASES:
        warn(
            "small_suite",
            f"The suite has fewer than {SMALL_SUITE_CASES} cases, so a gap of a case or two is "
            "within noise.",
        )

    overlapping = [
        arm.label
        for arm in arms
        if arm.judges
        and model_family(arm.model_id) in {model_family(_model_of(j)) for j in arm.judges}
    ]
    if overlapping:
        warn(
            "judge_family_overlap",
            "A judge is from the same model family as this arm, and judges tend to favour their "
            "own family's answers. Add a judge from another family to the panel.",
            overlapping,
        )

    unchecked = [arm.label for arm in arms if arm.uses_tools and not arm.asserts_tools]
    if unchecked:
        warn(
            "tools_unchecked",
            "The suite gives the model tools but no case asserts anything about tool calls "
            "(`tool_called`, `tool_sequence`, `no_tool_errors`). A fallback that answers well "
            "but calls tools wrongly would still pass.",
            unchecked,
        )

    if baseline_arm is not None:
        if summaries[baseline_arm.label]["status"] != "completed":
            warn(
                "baseline_incomplete",
                "The baseline's evaluation did not complete, so nothing can be measured "
                "against it.",
                [baseline_arm.label],
            )
        confounded = [
            arm.label
            for arm in arms
            if arm is not baseline_arm
            and {"model", "prompt"} <= set(summaries[arm.label]["changes"])
        ]
        if confounded and not has_effects:
            warn(
                "confounded",
                "This arm changes both the model and the prompt, so a gain or loss cannot be "
                "credited to either. Run a models-by-prompts grid (`matrix:`) to separate them.",
                confounded,
            )
    return warnings


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #


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
        rows.append(
            {
                "id": case_id,
                "results": results,
                "agreement": _agreement(results),
                "critical": any(case_id in arm.critical for arm in arms),
            }
        )
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


# --------------------------------------------------------------------------- #
# From stored evaluations
# --------------------------------------------------------------------------- #


def arm_from_evaluation(evaluation: dict[str, Any]) -> Arm:
    """The :class:`Arm` for a stored suite evaluation (an ``EvaluationDetail`` as a dict).

    Everything is read from the evaluation as it was stored, so the arm names
    what actually ran, not what a later edit of a file says.
    """
    config = evaluation.get("config") or {}
    suite = config.get("suite") or {}
    run_config = suite.get("run_config") or {}
    tag = config.get("arm") or {}
    cases = suite.get("cases") or []
    result = evaluation.get("result") or {}
    judge = result.get("judge") or {}
    judges = (
        [f"{(config.get('grader') or {}).get('provider') or 'bedrock'}:{judge['model_id']}"]
        if judge.get("model_id")
        else []
    )
    judges += list(judge.get("panel") or [])
    bar = config.get("readiness")
    return Arm(
        provider=str(run_config.get("provider") or "bedrock"),
        model_id=str(run_config.get("model_id") or "unknown"),
        evaluation_id=evaluation.get("id"),
        status=str(evaluation.get("status")),
        result=evaluation.get("result"),
        name=tag.get("name"),
        baseline=bool(tag.get("baseline")),
        axes=dict(tag.get("axes") or {}),
        run_config=dict(run_config),
        readiness=ReadinessBar.model_validate(bar) if bar else None,
        critical=frozenset(str(case["id"]) for case in cases if case.get("critical")),
        judges=tuple(judges),
        uses_tools=bool(run_config.get("toolset") or run_config.get("mcp_servers")),
        asserts_tools=any(
            str(check.get("type", "")).startswith("tool_")
            or check.get("type") in ("no_tool_errors", "max_tool_calls")
            for case in cases
            for check in case.get("assert") or []
        ),
    )


def suite_differences(evaluations: list[dict[str, Any]]) -> list[str]:
    """Case ids on which the evaluations' stored suites disagree; empty when they match.

    Two evaluations are the same suite when they have the same cases (by id) with
    the same ``input``. Models and prompts are meant to differ; the questions are not.
    """
    inputs: list[dict[str, str]] = []
    for evaluation in evaluations:
        cases = ((evaluation.get("config") or {}).get("suite") or {}).get("cases") or []
        inputs.append({str(case.get("id")): str(case.get("input")) for case in cases})
    every = {case_id for mapping in inputs for case_id in mapping}
    return sorted(
        case_id for case_id in every if len({mapping.get(case_id) for mapping in inputs}) > 1
    )


def suite_fingerprint(suite: dict[str, Any]) -> str:
    """A short stable id for what a stored suite asks: its cases, not its model or prompt.

    Two suites with the same fingerprint pose the same questions with the same
    checks, so results on one say something about the other. Changing a case,
    its expected answer, its checks or its ``critical`` flag changes it.
    """
    cases = json.dumps(suite.get("cases") or [], sort_keys=True, default=str)
    return hashlib.sha256(cases.encode("utf-8")).hexdigest()[:16]


def build_comparison(
    evaluations: list[dict[str, Any]], *, baseline: str | None = None
) -> dict[str, Any]:
    """Compare stored suite evaluations (``EvaluationDetail`` dicts); the one entry point.

    Used by the API and the CLI so both mean the same thing. Raises
    :class:`ComparisonError`, with a code saying why, for a set that is not all
    suites, did not run the same suite, or repeats an arm.
    """
    not_suites = [str(entry.get("id")) for entry in evaluations if entry.get("kind") != "suite"]
    if not_suites:
        raise ComparisonError(
            "Only suite evaluations can be compared; not a suite: " + ", ".join(not_suites),
            detail={"evaluation_ids": not_suites},
            code="compare_not_suite",
        )
    differing = suite_differences(evaluations)
    if differing:
        raise ComparisonError(
            "These evaluations did not run the same suite; the cases differ on: "
            + ", ".join(differing),
            detail={"cases": differing},
            code="compare_different_suites",
        )
    arms = [arm_from_evaluation(entry) for entry in evaluations]
    try:
        comparison = compare(arms, baseline=baseline)
    except ValueError as exc:
        named = any(arm.name for arm in arms)
        is_baseline = "baseline" in str(exc)
        raise ComparisonError(
            str(exc)[0].upper() + str(exc)[1:],
            code="compare_unknown_baseline"
            if is_baseline
            else ("compare_duplicate_arm" if named else "compare_duplicate_model"),
        ) from None
    suite = (evaluations[0].get("config") or {}).get("suite") or {}
    comparison["suite"] = {
        "name": suite.get("name"),
        "cases": len(suite.get("cases") or []),
        "fingerprint": suite_fingerprint(suite),
    }
    return comparison
