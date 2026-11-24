"""Compare finished agent jobs by model.

Pure functions over stored jobs: no reads, no model calls. The router reads the
jobs (:class:`nimbus.agentjobs.store.AgentJobStore`) and hands them here.

**What this can claim.** These are real jobs, not a benchmark: different models
got different issues. Two things keep the comparison honest about that:

* Jobs whose model the router picked *because of* the issue (``routing.source
  == "tier"``) are left out by default. Tier routing sends harder or riskier
  work to stronger models on purpose, so counting it compares the work, not
  the models. Round-robin pools and fixed agents are kept: there the issue does
  not choose the model.
* Every rate carries a 95% Wilson interval, and each arm is tested against the
  leader with a Newcombe interval for the difference. An arm is only reported
  as behind the leader when that interval excludes zero -- at ~100 jobs a month
  most gaps will not, and saying so is the point.

**Which jobs count as attempts.** Updraft's outcome vocabulary is open-ended
(the broker accepts any ``[a-z_]`` string and treats what it does not know as a
failure), so this module does not: it counts an outcome only when it knows what
it means.

A job carries two outcomes, and both are read (#66):

* ``outcome`` is the microVM runner's. It is ``succeeded`` whenever ``updraft
  run-event`` exits 0 -- which it also does when no agent did any work.
* ``updraft_outcome`` is ``updraft run-event``'s own (``RunEventSummary``):
  whether the job's agent actually ran and how that went.

So, in order:

1. A runner outcome that says nothing about the agent -- a launch failure, a
   throttle, a superseded, admin-released or stopped job, a lost microVM, a
   runner or state-sync error -- is ``not_attributable``.
2. An Updraft outcome where no agent work was judged -- ``nothing-to-do``,
   ``canceled``, ``superseded``, ``deferred``, ``not-started``,
   ``state-pending``, ``error`` (the command itself failed) -- or where the
   agent succeeded and only its publication failed (``publish-failed``) is
   ``not_attributable`` too, whatever the runner said.
3. ``failed`` or ``timed_out`` from the runner, or ``succeeded`` from the runner
   with ``failed`` from Updraft, is the agent's ``failure``.
4. ``succeeded`` from *both* is a ``success``.
5. Anything else -- a missing outcome on either side (identity-only events, a
   runner success whose summary was lost), Updraft's ``unknown`` /
   ``unconfirmed``, a value added later -- is ``unresolved``. Counting it either
   way would credit or charge the model for what nobody knows.

Only successes and failures are the success rate's denominator; the others are
counted in ``excluded.not_attributable`` and ``excluded.unresolved_outcome``.

**One kind at a time.** Implement, revision and review jobs succeed by different
standards, so by default only ``implement`` jobs are compared; another kind is
asked for by name, and mixing kinds (``kind=None``) is an explicit opt-in that
carries a warning.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from nimbus.agentjobs.schemas import is_synthetic
from nimbus.agentjobs.store import primary_step

GroupBy = Literal["model", "model_effort", "agent", "adapter"]

OutcomeClass = Literal["success", "failure", "not_attributable", "unresolved"]

#: The outcome that counts as success, as Updraft's broker records it.
SUCCEEDED = "succeeded"
#: The runner's failures (deploy/aws-microvm/image/runner/job.mjs): updraft
#: exited non-zero, or the job ran out of its time budget.
FAILURE_OUTCOMES = frozenset({"failed", "timed_out"})
#: ``updraft run-event``'s outcome when its agent's work failed.
UPDRAFT_FAILED = "failed"
#: ``updraft run-event`` outcomes under which no agent work was judged, or the
#: agent's work succeeded and only publishing it failed (src/commands/run-event.ts,
#: ``RunEventSummary['outcome']``). Under each, the runner may still say
#: ``succeeded``: the command exited 0.
UPDRAFT_NOT_ATTRIBUTABLE_OUTCOMES = frozenset(
    {
        "nothing-to-do",
        "canceled",
        "superseded",
        "deferred",
        "not-started",
        "state-pending",
        "error",
        "publish-failed",
    }
)
#: Outcomes that say nothing about the agent: the job never got a fair run, was
#: stopped by someone, or the infrastructure around it failed (cloud/server/src
#: launch.ts, broker.ts, reconcile.ts, admin.ts; runner/job.mjs).
NOT_ATTRIBUTABLE_OUTCOMES = frozenset(
    {
        "launch_failed",
        "throttled",
        "superseded",
        "microvm_gone",
        "released_by_admin",
        "terminated",
        "runner_error",
        "state_not_saved",
    }
)
#: The default kind compared, and the value that asks for every kind mixed.
DEFAULT_KIND = "implement"
#: Routing sources whose model choice depended on the issue.
BIASED_ROUTING = frozenset({"tier"})
#: Below this many jobs an arm is flagged, matching Updraft's own insights.
MIN_SAMPLE = 20
#: The label of the arm for jobs whose events did not say which model ran.
UNKNOWN = "unknown"

_Z = 1.959963984540054  # two-sided 95%


def wilson(successes: int, trials: int, z: float = _Z) -> tuple[float, float] | None:
    """The Wilson score interval for a proportion, or ``None`` with no trials."""
    if trials <= 0:
        return None
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def newcombe(
    successes_a: int, trials_a: int, successes_b: int, trials_b: int
) -> tuple[float, float] | None:
    """Newcombe's hybrid score interval for ``rate(a) - rate(b)`` (method 10)."""
    interval_a = wilson(successes_a, trials_a)
    interval_b = wilson(successes_b, trials_b)
    if interval_a is None or interval_b is None:
        return None
    p_a, p_b = successes_a / trials_a, successes_b / trials_b
    (low_a, high_a), (low_b, high_b) = interval_a, interval_b
    difference = p_a - p_b
    lower = difference - math.sqrt((p_a - low_a) ** 2 + (high_b - p_b) ** 2)
    upper = difference + math.sqrt((high_a - p_a) ** 2 + (p_b - low_b) ** 2)
    return lower, upper


def outcome_class(job: Mapping[str, Any]) -> OutcomeClass:
    """What a job's two outcomes say about its agent (see the module docstring)."""
    outcome = job.get("outcome")
    updraft = job.get("updraft_outcome")
    if outcome in NOT_ATTRIBUTABLE_OUTCOMES or updraft in UPDRAFT_NOT_ATTRIBUTABLE_OUTCOMES:
        return "not_attributable"
    if outcome in FAILURE_OUTCOMES or (outcome == SUCCEEDED and updraft == UPDRAFT_FAILED):
        return "failure"
    if outcome == SUCCEEDED and updraft == SUCCEEDED:
        return "success"
    return "unresolved"


def _succeeded(job: Mapping[str, Any]) -> bool:
    return outcome_class(job) == "success"


def arm_label(job: Mapping[str, Any], group_by: GroupBy) -> str:
    """The arm a job belongs to, from the step it is credited to."""
    step = primary_step(job) or {}
    model = step.get("model")
    if group_by == "agent":
        label = step.get("agent")
    elif group_by == "adapter":
        label = step.get("adapter")
    elif group_by == "model_effort":
        effort = step.get("effort")
        label = f"{model} @ {effort}" if model and effort else model
    else:
        label = model
    return label or UNKNOWN


#: The step attributes an arm describes itself by.
ARM_ATTRIBUTES = ("model", "effort", "agent", "adapter")


def _describe(label: str, jobs: list[Mapping[str, Any]]) -> dict[str, Any]:
    """An arm's identity, from *every* member rather than the first.

    An attribute is reported only when *every* job in the arm reports it, and
    they all report the same value. Otherwise it is ``None``, and:

    * ``mixed`` lists the values that were reported -- several for an adapter
      arm holding two models, or one when some members reported it and others
      did not;
    * ``unreported`` counts the members that did not report it at all.

    A job that did not say which model ran is not evidence that it was the
    model its arm-mates reported, so one reported value among silent members is
    never promoted to the arm's identity.
    """
    steps = [primary_step(job) or {} for job in jobs]
    described: dict[str, Any] = {"arm": label}
    mixed: dict[str, list[str]] = {}
    unreported: dict[str, int] = {}
    for attribute in ARM_ATTRIBUTES:
        reported = [str(v) for step in steps if (v := step.get(attribute)) is not None]
        values = sorted(set(reported))
        missing = len(steps) - len(reported)
        unanimous = len(values) == 1 and missing == 0
        described[attribute] = values[0] if unanimous else None
        if values and not unanimous:
            mixed[attribute] = values
        if missing:
            unreported[attribute] = missing
    described["mixed"] = mixed
    described["unreported"] = unreported
    return described


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


UsageRows = list[tuple[Mapping[str, Any], Mapping[str, Any]]]


def _reported(rows: UsageRows, key: str) -> list[float]:
    """The values of ``key`` that were reported, never a missing one as zero."""
    return [float(usage[key]) for _, usage in rows if usage.get(key) is not None]


def _cost(rows: UsageRows) -> dict[str, Any]:
    """Cost over the jobs that reported one.

    Per success counts only those jobs on both sides: what every costed attempt
    spent, divided by the successes among them. A partly-reported arm is not
    made to look cheap by spreading its cost over jobs that sent none.
    """
    costs = _reported(rows, "cost_usd")
    total = sum(costs) if costs else None
    costed_successes = sum(
        1 for job, usage in rows if usage.get("cost_usd") is not None and _succeeded(job)
    )
    return {
        "reported": len(costs),
        "total_usd": total,
        "per_job_usd": _mean(costs),
        "per_success_usd": total / costed_successes
        if total is not None and costed_successes
        else None,
    }


def _model_sources(jobs: list[Mapping[str, Any]]) -> dict[str, int]:
    """How each credited step's model was known.

    ``response`` or ``configured`` as Updraft reported it; ``unreported`` for a
    model with no source; ``no_model`` when the step named no model at all.

    Updraft's own insights trust ``response`` over ``configured``: a configured
    model is what was asked for, which a fallback may not have served.
    """
    counts: dict[str, int] = {}
    for job in jobs:
        step = primary_step(job) or {}
        source = step.get("model_source")
        key = "no_model" if not step.get("model") else str(source) if source else "unreported"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _summarize(label: str, jobs: list[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(jobs)
    succeeded = sum(1 for job in jobs if _succeeded(job))
    verified = [
        job["verification_passed"] for job in jobs if job.get("verification_passed") is not None
    ]
    opened = sum(1 for job in jobs if (job.get("pr") or {}).get("url") or job.get("pull_request"))
    merged_known = [
        (job.get("loop") or {}).get("merged")
        for job in jobs
        if (job.get("loop") or {}).get("merged") is not None
    ]
    rounds = [
        float(job["loop"]["rounds"])
        for job in jobs
        if (job.get("loop") or {}).get("rounds") is not None
    ]
    durations = [
        float(job["duration_seconds"]) for job in jobs if job.get("duration_seconds") is not None
    ]
    interval = wilson(succeeded, n)
    step_usage = [(job, (primary_step(job) or {}).get("usage") or {}) for job in jobs]
    job_usage = [(job, job.get("telemetry") or {}) for job in jobs]
    return {
        **_describe(label, jobs),
        "jobs": n,
        "succeeded": succeeded,
        "success_rate": _rate(succeeded, n),
        "success_interval": list(interval) if interval else None,
        "verification_reported": len(verified),
        "verification_pass_rate": _rate(sum(verified), len(verified)),
        "pr_opened_rate": _rate(opened, n),
        "merge_reported": len(merged_known),
        "merged_rate": _rate(sum(1 for m in merged_known if m), len(merged_known)),
        "mean_revision_rounds": _mean(rounds),
        "median_duration_seconds": statistics.median(durations) if durations else None,
        # The credited step's own usage: what *this arm's model* spent (#67).
        "cost": _cost(step_usage),
        "mean_input_tokens": _mean(_reported(step_usage, "input_tokens")),
        "mean_output_tokens": _mean(_reported(step_usage, "output_tokens")),
        # Every step summed, reviewers and revisions included: what the job cost.
        "job_cost": _cost(job_usage),
        "model_source": _model_sources(jobs),
        "small_sample": n < MIN_SAMPLE,
    }


def _ranking_key(summary: Mapping[str, Any]) -> tuple[float, float, float, float]:
    """Best first: success rate, then merge rate, then cheaper, then more jobs."""
    cost = summary["cost"]["per_job_usd"]
    return (
        -(summary["success_rate"] or 0.0),
        -(summary["merged_rate"] or 0.0),
        cost if cost is not None else math.inf,
        -summary["jobs"],
    )


def compare_jobs(
    jobs: Iterable[Mapping[str, Any]],
    *,
    group_by: GroupBy = "model",
    include_routed: bool = False,
    include_benchmarks: bool = False,
    repository: str | None = None,
    tenant_id: str | None = None,
    stack: str | None = None,
    kind: str | None = DEFAULT_KIND,
) -> dict[str, Any]:
    """Group finished jobs into arms and compare them.

    Args:
        jobs: Stored jobs (:func:`nimbus.agentjobs.store.record_of` shape).
        group_by: What an arm is -- the model, model and effort, the agent
            profile, or the adapter.
        include_routed: Keep tier-routed jobs (biased; see the module docstring).
        include_benchmarks: Keep jobs a benchmark dispatched. They are a
            different population -- chosen tasks, not real work -- and belong in
            a benchmark's own comparison.
        kind: The job kind compared (default ``implement``). ``None`` mixes
            every kind into the same arms -- an explicit opt-in, warned about,
            because the kinds succeed by different standards.
        repository, tenant_id, stack: Keep only jobs that match.

    Only jobs whose outcome is a known success or agent failure are compared;
    every other job is counted in ``excluded`` under the reason it was left out.
    """
    excluded = {
        "synthetic": 0,
        "filtered": 0,
        "not_attributable": 0,
        "unresolved_outcome": 0,
        "routed": 0,
        "benchmark": 0,
    }
    groups: dict[str, list[Mapping[str, Any]]] = {}
    unknown_model = 0
    total = 0
    for job in jobs:
        total += 1
        # Smoke-test items are never real work, whatever filters were asked for.
        if is_synthetic(job.get("stack")):
            excluded["synthetic"] += 1
            continue
        if (
            (repository and job.get("repository") != repository)
            or (tenant_id and job.get("tenant_id") != tenant_id)
            or (stack and job.get("stack") != stack)
            or (kind is not None and job.get("kind") != kind)
        ):
            excluded["filtered"] += 1
            continue
        verdict = outcome_class(job)
        if verdict == "not_attributable":
            excluded["not_attributable"] += 1
            continue
        if verdict == "unresolved":
            excluded["unresolved_outcome"] += 1
            continue
        if not include_routed and (job.get("routing") or {}).get("source") in BIASED_ROUTING:
            excluded["routed"] += 1
            continue
        if not include_benchmarks and job.get("benchmark"):
            excluded["benchmark"] += 1
            continue
        label = arm_label(job, group_by)
        if label == UNKNOWN:
            unknown_model += 1
        groups.setdefault(label, []).append(job)

    summaries = sorted(
        (_summarize(label, members) for label, members in groups.items()), key=_ranking_key
    )
    # The leader is the best-ranked arm whose model is known: crediting a lead to
    # "unknown" would tell nobody which model to pick.
    leader = next((s for s in summaries if s["arm"] != UNKNOWN), None)
    for summary in summaries:
        summary["versus_leader"] = _versus(summary, leader)

    return {
        "basis": "observational",
        "group_by": group_by,
        "kind": kind,
        "jobs_read": total,
        "jobs_compared": sum(s["jobs"] for s in summaries),
        "excluded": excluded,
        "leader": leader["arm"] if leader else None,
        "arms": summaries,
        "warnings": _warnings(summaries, excluded, unknown_model, include_routed, kind),
    }


def _versus(summary: Mapping[str, Any], leader: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if leader is None or summary is leader or summary["arm"] == UNKNOWN:
        return None
    interval = newcombe(summary["succeeded"], summary["jobs"], leader["succeeded"], leader["jobs"])
    difference = (summary["success_rate"] or 0.0) - (leader["success_rate"] or 0.0)
    return {
        "arm": leader["arm"],
        "success_rate_difference": difference,
        "difference_interval": list(interval) if interval else None,
        # Only "behind" when the whole interval is below zero. Ranking still puts
        # it second either way; this says whether the data can tell them apart.
        "distinguishable": bool(interval and (interval[1] < 0 or interval[0] > 0)),
    }


def _warnings(
    summaries: list[Mapping[str, Any]],
    excluded: Mapping[str, int],
    unknown_model: int,
    include_routed: bool,
    kind: str | None,
) -> list[str]:
    warnings: list[str] = []
    if kind is None:
        warnings.append(
            "Job kinds are mixed: implement, revision and review jobs succeed by different "
            "standards, so an arm's rate blends them. Compare one kind for a ranking."
        )
    if excluded.get("unresolved_outcome"):
        warnings.append(
            f"{excluded['unresolved_outcome']} job(s) had a missing or unrecognized outcome "
            "and were left out rather than counted as failures."
        )
    if not summaries:
        warnings.append("No jobs to compare in this window.")
        return warnings
    small = [s["arm"] for s in summaries if s["small_sample"]]
    if small:
        warnings.append(
            f"Fewer than {MIN_SAMPLE} jobs: {', '.join(small)}. "
            "Treat those rates as anecdotes, not measurements."
        )
    if unknown_model:
        warnings.append(
            f"{unknown_model} job(s) did not report a model (events from before "
            "software-factory-cli #111); they are grouped as 'unknown'."
        )
    if include_routed:
        warnings.append(
            "Tier-routed jobs are included: those models were chosen for their issues, "
            "so differences partly reflect the work, not the model."
        )
    elif excluded.get("routed"):
        warnings.append(f"{excluded['routed']} tier-routed job(s) left out as a biased sample.")
    contenders = [s for s in summaries if s.get("versus_leader")]
    if contenders and not any(s["versus_leader"]["distinguishable"] for s in contenders):
        warnings.append(
            "No arm's success rate is distinguishable from the leader's at 95%; "
            "the ranking is not yet evidence. A paired benchmark settles this faster."
        )
    return warnings
