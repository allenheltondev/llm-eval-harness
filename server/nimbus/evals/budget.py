"""What an evaluation spends, and the budget that bounds it.

Two pieces, both lane-agnostic so the local lane and the cloud worker behave
identically (they share :func:`nimbus.evals.engine.execute_evaluation_with_seam`):

:class:`CostLedger`
    Tracks the model-under-test's spend as runs finish and decides whether the
    next run may start. With ``max_cost_usd`` set, a run is admitted only while
    ``spent + (in_flight + 1) * mean_cost_per_run`` stays within the budget.
    Once one is refused, no later run starts. Runs already in flight always
    finish, so the overshoot is bounded by the concurrency limit. Until the
    first run finishes the per-run figure is a pre-flight estimate
    (:func:`prior_per_run`), so the first concurrent batch is sized against the
    budget too rather than always starting in full.
:class:`JudgeMeter`
    Wraps the judge factory so every judge model it builds reports its token
    usage back here, whichever provider the judge runs on. Judge spend is
    reported separately from the runs. It does not count against the budget,
    because the judge runs after the last run is scheduled and grading the
    runs that did happen is the point of partial results.

Every figure is an estimate from :mod:`nimbus.pricing`.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Iterable
from typing import Any

from strands.models.model import Model

from nimbus import pricing
from nimbus.evals.judge import JudgeFactory, call_judge_factory
from nimbus.providers import DEFAULT_PROVIDER, Provider

#: The error a suite case carries when the budget ran out before it ran.
BUDGET_EXHAUSTED_ERROR = {
    "code": "budget_exhausted",
    "message": "Not run: the evaluation's max_cost_usd budget was spent",
}


class CostLedger:
    """Model-under-test spend, and budget admission for new runs."""

    def __init__(self, max_cost_usd: float | None = None, prior_per_run: float = 0.0) -> None:
        self.max_cost_usd = max_cost_usd
        #: Estimated cost of one run, used until a finished run gives a real mean.
        self.prior_per_run = prior_per_run
        self.spent = 0.0
        self.finished = 0
        self.in_flight = 0
        self.skipped = 0
        self.exhausted = False

    def estimate_per_run(self) -> float:
        """Mean cost of the runs finished so far; the prior before the first one."""
        return self.spent / self.finished if self.finished else self.prior_per_run

    def admit(self) -> bool:
        """Whether one more run may start. Refusing is permanent."""
        if self.max_cost_usd is not None and not self.exhausted:
            projected = self.spent + (self.in_flight + 1) * self.estimate_per_run()
            if self.spent >= self.max_cost_usd or projected > self.max_cost_usd:
                self.exhausted = True
        if self.exhausted:
            self.skipped += 1
            return False
        self.in_flight += 1
        return True

    def settle(self, cost_usd: float | None) -> None:
        """Record an admitted run as finished, with what it cost (``None`` = unknown)."""
        self.in_flight = max(0, self.in_flight - 1)
        self.finished += 1
        self.spent += cost_usd or 0.0


class JudgeMeter:
    """A judge factory whose models count the tokens they use."""

    def __init__(self, inner: JudgeFactory) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self.model_id: str | None = None
        self.provider: str = DEFAULT_PROVIDER
        #: False when a built judge could not be instrumented: its usage, and
        #: so its cost, is unknown rather than zero.
        self.metered = True
        #: Models already instrumented, so a factory that hands back the same
        #: instance twice does not count its tokens twice.
        self._instrumented: set[int] = set()
        self.usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_write_input_tokens": 0,
        }

    def factory(self, model_id: str, provider: Provider = DEFAULT_PROVIDER) -> Model:
        """The :data:`JudgeFactory` handed to the grader in place of the real one."""
        model = call_judge_factory(self._inner, model_id, provider)
        self.model_id, self.provider = model_id, provider
        self._instrument(model)
        return model

    def _instrument(self, model: Model) -> None:
        if id(model) in self._instrumented:
            return
        self._instrumented.add(id(model))
        original = model.stream
        meter = self

        async def stream(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            async for event in original(*args, **kwargs):
                if isinstance(event, dict):
                    usage = (event.get("metadata") or {}).get("usage")
                    if usage:
                        meter._add(usage)
                yield event

        try:
            model.stream = stream  # type: ignore[method-assign]
        except (AttributeError, TypeError):  # pragma: no cover - a frozen model class
            self.metered = False

    def _add(self, usage: dict[str, Any]) -> None:
        with self._lock:
            self.usage["input_tokens"] += int(usage.get("inputTokens") or 0)
            self.usage["output_tokens"] += int(usage.get("outputTokens") or 0)
            self.usage["cache_read_input_tokens"] += int(usage.get("cacheReadInputTokens") or 0)
            self.usage["cache_write_input_tokens"] += int(usage.get("cacheWriteInputTokens") or 0)

    def cost_usd(self) -> float | None:
        """The judge's estimated spend: ``0`` if it never ran, ``None`` if unpriced."""
        if self.model_id is None:
            return 0.0
        if not self.metered:
            return None
        return pricing.cost_usd(self.provider, self.model_id, self.usage)


def prior_per_run(run_configs: Iterable[Any]) -> float:
    """Mean pre-flight cost estimate over the runs an evaluation will make.

    Unpriced runs contribute nothing (``0`` when none is priced): a budget
    already requires a priced model, so that only happens with no budget.
    """
    estimates = [
        estimate
        for config in run_configs
        if (
            estimate := pricing.estimate_run_cost(
                config.provider,
                config.model_id,
                len(config.system_prompt) + len(config.user_prompt),
                config.inference.max_tokens,
            )
        )
        is not None
    ]
    return sum(estimates) / len(estimates) if estimates else 0.0


def _run_model(request: Any) -> tuple[str, str] | None:
    """``(provider, model_id)`` of the runs an evaluation executes, if it executes any."""
    if request.kind == "suite" and request.suite is not None:
        config = request.suite.run_config
    elif request.kind == "determinism" and request.run_config is not None:
        config = request.run_config
    else:
        return None
    return config.provider, config.model_id


def runs_cost_usd(request: Any, ledger: CostLedger) -> float | None:
    """What the evaluation's own runs cost; ``None`` when their model is unpriced."""
    model = _run_model(request)
    if model is None:  # kind="grade" executes nothing: the runs were paid for earlier
        return 0.0
    if not pricing.is_priced(*model):
        return None
    return round(ledger.spent, 6)


def annotate(
    result: dict[str, Any], request: Any, ledger: CostLedger, meter: JudgeMeter
) -> dict[str, Any]:
    """Add ``cost`` and ``budget_exhausted`` to a finished evaluation's result."""
    runs_usd = runs_cost_usd(request, ledger)
    judge_usd = meter.cost_usd()
    total = None if runs_usd is None or judge_usd is None else round(runs_usd + judge_usd, 6)
    result["cost"] = {
        "currency": "USD",
        "estimate": True,
        "pricing_as_of": pricing.PRICING_AS_OF,
        "runs_usd": runs_usd,
        "judge_usd": judge_usd,
        "total_usd": total,
        "judge_tokens": dict(meter.usage),
        "max_cost_usd": ledger.max_cost_usd,
    }
    result["budget_exhausted"] = ledger.exhausted
    if ledger.exhausted:
        result["skipped_runs"] = ledger.skipped
        for case in result.get("cases") or []:
            if (case.get("runs") or {}).get("total") == 0:
                case["error"] = dict(BUDGET_EXHAUSTED_ERROR)
    return result
