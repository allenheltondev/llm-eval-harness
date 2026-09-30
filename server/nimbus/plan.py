"""A fallback plan: which variants were shown ready to stand in, and whether that still holds.

A comparison answers a question about one moment: these arms, this suite, this
bar, these results. A plan is that answer written down so something else can act
on it: a runbook, a deploy check, an on-call engineer picking a model at 3am.
It pins exactly what the verdicts were about:

* the suite, by a fingerprint of its cases;
* each arm's model and prompt;
* the bar the arms were judged by;
* the evaluations the verdicts came from, and when.

Only ``ready`` arms enter ``fallbacks``. The rest stay in ``evidence`` with their
reasons, so the file also says what was tried and why it was left out.

A plan goes stale silently: someone edits a case, rewords the prompt or swaps
the model, and the verdict no longer describes what would run. :func:`check`
compares a plan with the suite file as it is now, offline and at no cost, and
lists every way it has drifted. Re-running the comparison is what makes a stale
plan current again; checking never calls a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nimbus import arms as arms_module
from nimbus.errors import AppError
from nimbus.evals.compare import prompt_id, suite_fingerprint
from nimbus.evals.readiness import ReadinessBar
from nimbus.suite_edit import stored_suite

#: The plan file's format version.
VERSION = 1

#: How old a plan may be before it is stale, unless the plan says otherwise.
DEFAULT_MAX_AGE_DAYS = 30


class PlanError(AppError):
    """A plan file could not be built or read."""

    status_code = 400
    code = "fallback_plan_invalid"


def build(
    comparison: dict[str, Any],
    *,
    suite_file: str | None = None,
    now: datetime | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> dict[str, Any]:
    """The plan a comparison supports.

    ``suite_file`` is where the suite lives, relative to where the plan will be
    written, so :func:`check` can find it again. Raises :class:`PlanError` for a
    comparison with no baseline: readiness is relative to one.
    """
    if not comparison.get("baseline"):
        raise PlanError("a plan needs a baseline: readiness is measured against one")
    if max_age_days < 1:
        raise PlanError("max_age_days must be at least 1")
    arms = {arm["label"]: arm for arm in comparison["arms"]}
    baseline = arms[comparison["baseline"]]
    ranked = {label: index for index, label in enumerate(comparison["ranking"])}
    candidates = sorted(
        (arm for arm in arms.values() if not arm["baseline"]),
        key=lambda arm: ranked.get(arm["label"], len(ranked)),
    )
    fallbacks = [_entry(arm) for arm in candidates if _status(arm) == "ready"]
    evidence = [
        {**_entry(arm), "status": _status(arm), "reasons": arm["vs_baseline"]["reasons"]}
        for arm in candidates
        if _status(arm) != "ready" and arm.get("vs_baseline")
    ]
    return {
        "version": VERSION,
        "created_at": _iso(now or datetime.now(UTC)),
        "max_age_days": max_age_days,
        "suite": {**comparison["suite"], **({"file": suite_file} if suite_file else {})},
        "bar": ReadinessBar.model_validate(
            {key: value for key, value in comparison["bar"].items() if key != "source"}
        ).model_dump(),
        "baseline": _entry(baseline),
        "fallbacks": fallbacks,
        "evidence": evidence,
    }


def load(document: Any) -> dict[str, Any]:
    """A parsed plan file, checked for the shape :func:`check` relies on."""
    if not isinstance(document, dict) or document.get("version") != VERSION:
        raise PlanError(f"not a fallback plan (expected an object with version {VERSION})")
    missing = [
        key
        for key in ("created_at", "suite", "bar", "baseline", "fallbacks")
        if key not in document
    ]
    if missing:
        raise PlanError(f"the fallback plan is missing: {', '.join(missing)}")
    return document


@dataclass(frozen=True)
class Finding:
    """One way a plan no longer matches the world."""

    #: A stable code: ``suite_changed``, ``arm_changed``, ``bar_changed``,
    #: ``expired``, ``no_fallback`` or ``unverifiable``.
    code: str
    message: str
    #: Stale findings mean the plan cannot be trusted; the rest are advice.
    stale: bool = True


def check(
    plan: dict[str, Any], suite_spec: dict[str, Any] | None, base_dir: Path, now: datetime
) -> list[Finding]:
    """Everything that makes ``plan`` untrustworthy now; empty when it still holds.

    ``suite_spec`` is the suite file's parsed contents (``None`` when the file
    could not be found, which is itself a finding). ``base_dir`` is the file's
    directory, where its prompt files are read from.
    """
    findings: list[Finding] = []
    created = _parse_time(plan["created_at"])
    limit = int(plan.get("max_age_days") or DEFAULT_MAX_AGE_DAYS)
    if created is None:
        findings.append(Finding("expired", "the plan's created_at is not a date"))
    elif now - created > timedelta(days=limit):
        age = (now - created).days
        findings.append(
            Finding("expired", f"the plan is {age} days old; it is good for {limit}. Re-run it.")
        )
    if not plan["fallbacks"]:
        findings.append(Finding("no_fallback", "the plan holds no ready fallback", stale=False))

    if suite_spec is None:
        findings.append(
            Finding("unverifiable", "the suite file was not found, so nothing else can be checked")
        )
        return findings

    spec, keys = arms_module.split_file_keys(dict(suite_spec))
    current = stored_suite(dict(suite_spec))
    fingerprint = suite_fingerprint(current)
    if fingerprint != plan["suite"].get("fingerprint"):
        findings.append(
            Finding(
                "suite_changed",
                "the suite's cases changed since the plan was made "
                f"({plan['suite'].get('fingerprint')} -> {fingerprint}); the verdicts are about "
                "different questions",
            )
        )

    bar = arms_module.readiness_bar(keys) or ReadinessBar()
    if bar.model_dump() != plan["bar"]:
        findings.append(
            Finding("bar_changed", "the readiness bar in the suite file is not the one judged by")
        )

    declared = arms_module.expand(keys, base_dir)
    if declared is None:
        findings.append(
            Finding(
                "unverifiable",
                "the suite file declares no `arms:`, so the models and prompts were not rechecked",
                stale=False,
            )
        )
        return findings
    running = {arm.name: _identity(spec, arm) for arm in declared.arms}
    for entry in [plan["baseline"], *plan["fallbacks"]]:
        now_running = running.get(entry["label"])
        if now_running is None:
            findings.append(
                Finding("arm_changed", f"{entry['label']} is no longer an arm of the suite file")
            )
        elif now_running != _identity(entry, None):
            findings.append(
                Finding(
                    "arm_changed",
                    f"{entry['label']} now runs {_describe(now_running)}, not "
                    f"{_describe(_identity(entry, None))}",
                )
            )
    return findings


def _identity(
    source: dict[str, Any], arm: arms_module.ArmSpec | None
) -> tuple[str, str, str | None]:
    """``(provider, model_id, prompt_id)``: what an arm runs.

    From a plan entry when ``arm`` is ``None``; from a suite file's ``run_config``
    with the arm's overrides laid over it otherwise.
    """
    if arm is None:
        return (source["provider"], source["model_id"], source.get("prompt_id"))
    run_config = {**(source.get("run_config") or {}), **arm.overrides}
    return (
        str(run_config.get("provider") or "bedrock"),
        str(run_config.get("model_id") or "unknown"),
        prompt_id(run_config.get("system_prompt")),
    )


def _describe(identity: tuple[str, str, str | None]) -> str:
    provider, model_id, prompt = identity
    return f"{provider}:{model_id} with prompt {prompt or 'none'}"


def _status(arm: dict[str, Any]) -> str | None:
    return (arm.get("vs_baseline") or {}).get("status")


def _entry(arm: dict[str, Any]) -> dict[str, Any]:
    """The part of a comparison arm a plan pins."""
    vs = arm.get("vs_baseline") or {}
    entry: dict[str, Any] = {
        "label": arm["label"],
        "provider": arm["provider"],
        "model_id": arm["model_id"],
        "prompt_id": arm.get("prompt_id"),
        "evaluation_id": arm.get("evaluation_id"),
        "pass_rate": arm.get("pass_rate"),
    }
    if vs:
        entry["regressions"] = len(vs["regressions"])
        entry["baseline_passed"] = vs["baseline_passed"]
        entry["regression_upper_bound"] = vs["regression_upper_bound"]
    return entry


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
