"""The body of an evaluation: execute, grade, persist, report.

The execution core is :func:`execute_evaluation_with_seam`, which knows how to
run and grade an evaluation but nothing at all about *where* progress events go
or *where* records are persisted. Those two capabilities are injected:

``emit(event_dict)``
    Publish one progress event, already in its NDJSON wire shape (the dict
    :meth:`nimbus.evals.events._Event.as_log_entry` produces). Synchronous.
``store``
    An :class:`EvalStore` -- evaluation-row updates plus run load/save.
``cancelled()``
    Polled between runs and once more before grading; ``True`` settles the
    evaluation as ``cancelled`` exactly as an ``asyncio`` cancellation would.

Two lanes share that core:

*local*
    ``POST /evaluations`` with ``execution="local"`` (the default). One
    ``asyncio`` task per evaluation; ``emit`` is the in-process job's event log
    (:mod:`nimbus.evals.jobs`) and ``store`` is this server's history
    repository (:class:`LocalEvalStore`). :func:`run_evaluation` is the whole
    adapter.
*cloud*
    The worker Lambda outside this process, whose ``emit`` appends
    DynamoDB ``EVENT#`` items and whose ``store`` writes DynamoDB ``META`` /
    ``RUN#`` items. See ``docs/cloud-evals.md`` and
    :mod:`nimbus.evals.cloud`; the worker imports
    :func:`execute_evaluation_with_seam` directly.

Determinism
-----------
The same ``run_config`` is executed ``n`` times through the ordinary run engine
(:func:`nimbus.engine.runner.execute_run`) -- each repetition persists its
own run row, so every repeat is inspectable in ``/runs`` afterwards. At most
:data:`MAX_CONCURRENT_RUNS` repeats are in flight at once.

A repeat whose in-band error is throttle-classified (``model_throttled``, from
``engine.model_factory.classify_error``) or otherwise marked ``retryable`` (a
provider 5xx, a dropped connection, a run timeout) is retried, sleeping
:data:`RETRY_BACKOFF_SECONDS` between attempts, each sleep jittered so
concurrent runs that were throttled together do not retry together. The tuple is
module-level so tests can shorten it. Retries keep the repeat's index; only the
successful attempt's run id is kept. Anything still failing after the last attempt is reported as
``run_failed``, excluded from grading, listed in ``result.failed_runs`` (with its
``run_id`` when the run got far enough to be recorded, else ``None``), and the
other repeats carry on.

Statuses
--------
``completed``
    The batch finished. The judge may still have failed -- that shows up as
    ``result.judge_error`` next to the (always present) local metrics.
``error``
    Not a single repeat succeeded, or the job itself blew up.
``cancelled``
    ``DELETE /evaluations/{id}`` arrived mid-flight. Whatever had already
    completed is persisted, no further repeats start.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import random
import time
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Protocol

from nimbus import spend as spend_module
from nimbus.config import Settings, get_settings
from nimbus.engine.events import ErrorEvent, RunCompleteEvent, RunStartEvent
from nimbus.engine.model_factory import ModelFactory, build_model, classify_error
from nimbus.engine.runner import execute_run
from nimbus.engine.schemas import RunRequest
from nimbus.errors import AppError
from nimbus.evals import assertions, budget, grader, jobs, rubrics
from nimbus.evals.events import (
    EvalCompleteEvent,
    EvalEvent,
    EvalStartEvent,
    GradingCompletedEvent,
    GradingStartedEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunStartedEvent,
    RunSummary,
)
from nimbus.evals.jobs import EvalJob
from nimbus.evals.judge import JudgeFactory, build_judge_model
from nimbus.evals.metrics import local_metrics
from nimbus.evals.outcomes import RunOutcome
from nimbus.evals.schemas import EvaluationRequest, GraderConfig, Suite
from nimbus.providers import DEFAULT_PROVIDER
from nimbus.store import history
from nimbus.store.repo import HistoryRepo, get_history_repo

logger = logging.getLogger(__name__)

MAX_CONCURRENT_RUNS = 3
#: Sleep between throttle retries; index 0 is used after the first failure.
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (5.0, 10.0)
THROTTLE_ERROR_CODE = "model_throttled"
#: The error code of a run cut off by ``Settings.run_timeout_seconds``.
RUN_TIMEOUT_ERROR_CODE = "run_timeout"
#: Each backoff sleeps between ``1 - RETRY_JITTER`` and ``1`` times its nominal
#: length.
RETRY_JITTER = 0.5


@dataclass
class EvalDeps:
    """Everything the job needs from the request that started it.

    The model/judge factories are the same dependency-injection seams the run
    router uses, so the whole pipeline can be driven by scripted fakes.
    """

    settings: Settings
    model_factory: ModelFactory
    judge_factory: JudgeFactory
    #: The history backend the repeats and the evaluation row are written
    #: through. ``None`` means "whatever ``settings`` resolves to" -- which is
    #: what the cloud worker wants (its repeats go to the microVM's own SQLite
    #: and are mirrored into DynamoDB by its :class:`EvalStore`), and what a
    #: local server wants too. It is a field rather than a lookup so a test can
    #: drive the whole pipeline against an in-memory table.
    repo: HistoryRepo | None = None
    #: Where per-user spend is settled. ``None`` means the store these settings
    #: resolve to; a field so a test can drive settlement against its own.
    spend: spend_module.SpendStore | None = None

    def spend_store(self) -> spend_module.SpendStore:
        """The counter store these deps settle spend into."""
        return self.spend if self.spend is not None else spend_module.get_spend_store(self.settings)

    def history_repo(self) -> HistoryRepo:
        """The repository these deps write history through."""
        return self.repo if self.repo is not None else get_history_repo(self.settings)


def default_deps(settings: Settings | None = None) -> EvalDeps:
    """The real (non-injected) dependency set: Bedrock models, Bedrock judge.

    The local lane always gets these through FastAPI's ``Depends``; the cloud
    worker has no request to hang them off, so it builds them from its own
    environment through this helper.
    """
    settings = settings or Settings()
    return EvalDeps(
        settings=settings,
        model_factory=lambda request: build_model(request, settings),
        judge_factory=lambda model_id, provider=DEFAULT_PROVIDER: build_judge_model(
            model_id, settings, provider
        ),
    )


# --------------------------------------------------------------------------- #
# The emitter/store seam
# --------------------------------------------------------------------------- #

#: One progress event in its NDJSON wire shape.
EventDict = dict[str, Any]
#: Publish one progress event. Must not block for long; must not be awaited.
EmitFn = Callable[[EventDict], Any]
#: Polled between runs and before grading. ``True`` means "stop, settle as cancelled".
CancelledFn = Callable[[], bool]


class EvalStore(Protocol):
    """Where an evaluation's durable records go.

    Deliberately tiny: the engine only ever needs to update *its own*
    evaluation row, read a run back after it has been executed (or, for
    ``kind="grade"``, read one that already existed), and publish a finished
    run. The local lane backs all three with the history repository
    (:class:`LocalEvalStore`);
    the cloud worker backs them with DynamoDB items
    (:class:`nimbus.worker.ddb.DynamoEvalStore`, restated for that side as
    :class:`nimbus.worker.interfaces.RunStore`).
    """

    #: The evaluation this store is bound to -- every ``save_evaluation`` targets it.
    evaluation_id: str

    def save_evaluation(self, **fields: Any) -> None:
        """Partially update the evaluation record (``status``, ``result``, ...)."""
        ...

    def load_run(self, run_id: str) -> Any:
        """Read a run record back. Raises ``NotFoundError`` if it is gone.

        Returns anything with the :class:`~nimbus.store.history.RunRecord`
        attributes the engine reads (``id``, ``status``, ``output``,
        ``user_prompt``, ``tool_transcript``, ``metrics``, ``error``).
        """
        ...

    def save_run(self, run_id: str) -> None:
        """Publish a just-finished run, before its ``run_completed`` event.

        A no-op for the local lane, where ``execute_run`` has already written
        the row through the same repository the reader uses; a copy of that row
        into a ``RUN#`` item in the cloud lane.
        """
        ...


class LocalEvalStore:
    """The local lane's :class:`EvalStore`: this server's history repository.

    Which backend that is depends on the deployment (SQLite on a laptop,
    DynamoDB in a deployed server that still has the local lane switched on) --
    resolved per call rather than at construction, because the job outlives the
    request that started it and must not hold a session open across it.
    """

    def __init__(self, evaluation_id: str = "", repo: HistoryRepo | None = None) -> None:
        self.evaluation_id = evaluation_id
        self._repo = repo

    @property
    def repo(self) -> HistoryRepo:
        return self._repo if self._repo is not None else get_history_repo(get_settings())

    def save_evaluation(self, **fields: Any) -> None:
        self.repo.update_evaluation(self.evaluation_id, **fields)

    def load_run(self, run_id: str) -> history.RunRecord:
        return self.repo.get_run(run_id)

    def save_run(self, run_id: str) -> None:
        """Nothing to do: ``execute_run`` wrote the row through the same repository."""


#: The name this class had while SQLite was the only local backend.
SqliteEvalStore = LocalEvalStore


class _CooperativeCancel(Exception):
    """Raised internally when ``cancelled()`` reports a cancellation request."""


@dataclass
class _Seam:
    """The four injected capabilities, bundled so helpers take one argument."""

    evaluation_id: str
    emit: EmitFn
    store: EvalStore
    cancelled: CancelledFn
    deps: EvalDeps
    #: Spend so far and the budget gate (:mod:`nimbus.evals.budget`).
    ledger: budget.CostLedger = field(default_factory=budget.CostLedger)

    def publish(self, event: EvalEvent) -> None:
        """Serialize an event to its wire shape and hand it to ``emit``."""
        self.emit(event.as_log_entry())


# --------------------------------------------------------------------------- #
# Executing the repeats
# --------------------------------------------------------------------------- #


class _Job(NamedTuple):
    """One run an evaluation has to make: where it sits, and what it runs."""

    index: int
    run_config: RunRequest
    case_id: str | None = None


def _determinism_jobs(request: EvaluationRequest) -> list[_Job]:
    """The same run, ``n`` times."""
    assert request.run_config is not None
    return [_Job(index, request.run_config) for index in range(request.n)]


def _suite_jobs(suite: Suite) -> list[_Job]:
    """Every case, ``repeats`` times, in suite order -- a case's repeats adjacent."""
    jobs: list[_Job] = []
    for case in suite.cases:
        run_config = suite.run_config.for_case(case)
        for _repeat in range(suite.repeats):
            jobs.append(_Job(len(jobs), run_config, case.id))
    return jobs


def planned_jobs(request: EvaluationRequest) -> list[_Job]:
    """Every run ``request`` will make (none for ``grade``, which re-reads stored runs)."""
    if request.kind == "determinism":
        return _determinism_jobs(request)
    if request.kind == "suite":
        assert request.suite is not None
        return _suite_jobs(request.suite)
    return []


async def _execute_once(
    job: _Job,
    deps: EvalDeps,
    store: EvalStore | None = None,
) -> RunOutcome:
    """Run ``job.run_config`` once, consuming the engine's event stream internally.

    ``store`` defaults to the local repository -- which is where ``execute_run``
    has just written the row in *either* lane, the cloud store's ``load_run``
    simply preferring that same local row.
    """
    store = store or LocalEvalStore()
    outcome = RunOutcome(
        index=job.index, user_prompt=job.run_config.user_prompt, case_id=job.case_id
    )
    started = time.perf_counter()

    events = execute_run(
        job.run_config,
        settings=deps.settings,
        model_factory=deps.model_factory,
        repo=deps.repo,
    )
    timeout_seconds = deps.settings.run_timeout_seconds
    deadline = asyncio.timeout(timeout_seconds or None)
    try:
        async with deadline, aclosing(events):
            async for event in events:
                match event:
                    case RunStartEvent():
                        outcome.run_id = event.run_id
                    case ErrorEvent():
                        outcome.error = {
                            "code": event.code,
                            "message": event.message,
                            "retryable": event.retryable,
                        }
                    case RunCompleteEvent():
                        outcome.status = event.status
                        outcome.output = event.final_text
                    case _:
                        pass
    except asyncio.CancelledError:
        raise
    except AppError as exc:
        # Setup failures (an unknown toolset, a missing provider key) never
        # reach the stream -- they are raised before ``run_start``.
        outcome.error = {"code": exc.code, "message": exc.message, "retryable": False}
    except Exception as exc:
        if deadline.expired():
            # The runner persisted the cut-off run as ``cancelled``; the outcome
            # says why, and lets the retry loop have another go.
            outcome.error = {
                "code": RUN_TIMEOUT_ERROR_CODE,
                "message": f"Run exceeded the {timeout_seconds:g}s run timeout",
                "retryable": True,
            }
        else:  # pragma: no cover - defensive
            code, message, retryable = classify_error(exc)
            outcome.error = {"code": code, "message": message, "retryable": retryable}

    outcome.duration_ms = int((time.perf_counter() - started) * 1000)
    if outcome.run_id is not None:
        record = store.load_run(outcome.run_id)
        outcome.tool_transcript = list(record.tool_transcript or [])
        outcome.cost_usd = (record.metrics or {}).get("cost_usd")
        if outcome.status != "completed":
            outcome.error = outcome.error or record.error
    return outcome


def _is_retryable(outcome: RunOutcome) -> bool:
    """Whether a failed attempt is worth another go: throttled, or marked retryable."""
    error = outcome.error
    if not error:
        return False
    return error.get("code") == THROTTLE_ERROR_CODE or bool(error.get("retryable"))


def _jittered(seconds: float) -> float:
    """``seconds`` scaled into ``[1 - RETRY_JITTER, 1]`` of itself."""
    return seconds * random.uniform(1 - RETRY_JITTER, 1.0)


async def _execute_with_retries(
    job: _Job,
    deps: EvalDeps,
    store: EvalStore | None = None,
) -> RunOutcome:
    """Execute one run, retrying retryable failures with jittered backoff."""
    outcome = await _execute_once(job, deps, store)
    for attempt, backoff in enumerate(RETRY_BACKOFF_SECONDS, start=1):
        if outcome.succeeded or not _is_retryable(outcome):
            break
        delay = _jittered(backoff)
        logger.info(
            "eval run %d failed (%s), retrying in %.1fs",
            job.index,
            (outcome.error or {}).get("code"),
            delay,
        )
        await asyncio.sleep(delay)
        spent_before = outcome.cost_usd
        outcome = await _execute_once(job, deps, store)
        outcome.attempts = attempt + 1
        if spent_before is not None:  # a failed attempt's tokens are still spent
            outcome.cost_usd = spent_before + (outcome.cost_usd or 0.0)
    return outcome


async def _execute_batch(seam: _Seam, jobs: list[_Job], collected: list[RunOutcome]) -> None:
    """Execute the batch, at most :data:`MAX_CONCURRENT_RUNS` at a time.

    Finished repeats are appended to ``collected`` as they land rather than
    returned in one go: a cancellation mid-batch has to be able to persist the
    repeats that *did* complete, and the gather never returns in that case.

    ``cancelled()`` is polled once per repeat, after the concurrency slot is
    acquired and before anything is emitted, so a cancellation stops the batch
    at the next slot rather than mid-run.
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)

    async def one(job: _Job) -> None:
        async with semaphore:
            if seam.cancelled() or not seam.ledger.admit():
                return
            seam.publish(RunStartedEvent(index=job.index, case_id=job.case_id))
            outcome = await _execute_with_retries(job, seam.deps, seam.store)
            seam.ledger.settle(outcome.cost_usd)
        collected.append(outcome)
        _publish_run(seam, outcome)

    await asyncio.gather(*(one(job) for job in jobs))
    collected.sort(key=lambda outcome: outcome.index)


def _publish_run(seam: _Seam, outcome: RunOutcome) -> None:
    """Hand the finished run to the store, *then* announce it.

    That order is the contract's writer rule: a client that reacts to
    ``run_completed`` by fetching the run must always find it.
    """
    if outcome.run_id is not None:
        seam.store.save_run(outcome.run_id)
    _emit_run_result(seam, outcome)


def _emit_run_result(seam: _Seam, outcome: RunOutcome) -> None:
    if outcome.succeeded:
        assert outcome.run_id is not None
        seam.publish(
            RunCompletedEvent(
                index=outcome.index,
                run_id=outcome.run_id,
                status=outcome.status,
                summary=RunSummary(**outcome.summary()),
                case_id=outcome.case_id,
            )
        )
    else:
        seam.publish(
            RunFailedEvent(index=outcome.index, error=outcome.error, case_id=outcome.case_id)
        )


def _load_stored_runs(seam: _Seam, run_ids: list[str]) -> list[RunOutcome]:
    """Turn already-stored runs into outcomes (``kind="grade"``).

    Missing ids are rejected by the router before the job starts, so anything
    unreadable here is a genuine mid-flight deletion.
    """
    outcomes: list[RunOutcome] = []
    for index, run_id in enumerate(run_ids):
        try:
            record = seam.store.load_run(run_id)
        except AppError as exc:
            outcome = RunOutcome(
                index=index,
                run_id=run_id,
                error={"code": exc.code, "message": exc.message, "retryable": False},
            )
            outcomes.append(outcome)
            _emit_run_result(seam, outcome)
            continue
        outcome = RunOutcome(
            index=index,
            run_id=record.id,
            status=record.status,
            output=record.output or "",
            user_prompt=record.user_prompt,
            tool_transcript=list(record.tool_transcript or []),
            duration_ms=int((record.metrics or {}).get("latency_ms", 0)),
            error=record.error,
        )
        outcomes.append(outcome)
        _emit_run_result(seam, outcome)
    return outcomes


# --------------------------------------------------------------------------- #
# Result assembly
# --------------------------------------------------------------------------- #


def _build_result(
    outcomes: list[RunOutcome],
    judged: grader.JudgeResult,
    request: EvaluationRequest,
) -> dict[str, Any]:
    successes = [outcome for outcome in outcomes if outcome.succeeded]
    metrics = local_metrics(
        [outcome.output for outcome in successes],
        [outcome.tool_transcript for outcome in successes],
    )
    metrics.update(judged.metrics)

    result: dict[str, Any] = {
        "grade": judged.grade,
        "score": judged.score,
        "reasoning": judged.reasoning,
        "judge": {
            "model_id": request.grader.model_id,
            "system_prompt_used": request.grader.system_prompt is not None,
            "rubric_used": request.rubric is not None,
        },
        "metrics": metrics,
        "run_ids": [outcome.run_id for outcome in successes],
        "failed_runs": [
            {"index": outcome.index, "run_id": outcome.run_id, "error": outcome.error}
            for outcome in outcomes
            if not outcome.succeeded
        ],
    }
    if judged.error is not None:
        result["judge_error"] = judged.error
    return result


#: Longest judge reasoning kept per suite case, in *serialized* bytes. In the
#: cloud lane the whole result is written to DynamoDB twice -- the META row and
#: the ``grading_completed`` event -- and an item is capped at 400 KB. The
#: store writes plain ``json.dumps`` output, which escapes every non-ASCII
#: character (six bytes for most, twelve for an emoji), so a limit counted in
#: characters would not bound the item at all.
MAX_CASE_REASONING_BYTES = 1_000

#: The most a suite result may take once serialized. With the 200,000-byte cap
#: on the request (stored beside it as ``config``) this keeps the META row
#: under DynamoDB's 400 KB item limit with room for the other attributes.
#: :func:`_fit_suite_result` enforces it, so it holds for any judge output.
MAX_RESULT_BYTES = 180_000

#: Longest assertion ``detail`` kept, in serialized bytes. A detail is a
#: one-line account of what a check saw, and there can be one per check per
#: repeat -- up to ``schemas.MAX_SUITE_ASSERTION_CHECKS`` of them.
MAX_ASSERTION_DETAIL_BYTES = 200

#: Successively tighter limits for the free-text fields of a result that is
#: still over budget. ``0`` drops the text: the ids, statuses and scores --
#: what a suite is *for* -- always survive.
_TEXT_LIMITS = (MAX_CASE_REASONING_BYTES, 300, 100, 0)


def _serialized_size(value: Any) -> int:
    """Bytes ``value`` takes as the store writes it (ASCII-escaped JSON)."""
    return len(json.dumps(value, default=str))


def _clip(text: str, limit: int = MAX_CASE_REASONING_BYTES) -> str:
    """``text`` cut so its serialized form (quotes aside) fits in ``limit`` bytes."""
    if _serialized_size(text) - 2 <= limit:
        return text
    low, high = 0, len(text)  # the longest prefix that fits, with its ellipsis
    while low < high:
        middle = (low + high + 1) // 2
        if _serialized_size(text[:middle].rstrip() + "…") - 2 <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…" if low else ""


def _fit_suite_result(result: dict[str, Any]) -> dict[str, Any]:
    """Shrink the free text of ``result`` until it serializes within budget.

    Judge reasoning, assertion details, run error messages and the judge error
    are the only parts whose size the harness does not control; everything else
    is bounded by the suite limits (``MAX_SUITE_ASSERTION_CHECKS`` included).
    When any of it had to be cut further than its per-item limit, ``truncated``
    says so, so a reader knows the prose is partial.
    """
    if _serialized_size(result) <= MAX_RESULT_BYTES:
        return result
    # The error dicts are shared with the run outcomes; clip copies, not those.
    result = copy.deepcopy(result)
    result["truncated"] = True
    for limit in _TEXT_LIMITS:
        for case in result["cases"]:
            if case["reasoning"] is not None:
                case["reasoning"] = _clip(case["reasoning"], limit) or None
            for check in _assertion_verdicts(case):
                if check["detail"] is not None:
                    check["detail"] = _clip(check["detail"], limit) or None
        errors = [case["error"] for case in result["cases"]]
        errors += [failed["error"] for failed in result["failed_runs"]]
        for error in errors:
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                error["message"] = _clip(error["message"], limit)
        if isinstance(result.get("judge_error"), str):
            result["judge_error"] = _clip(result["judge_error"], limit)
        if _serialized_size(result) <= MAX_RESULT_BYTES:
            break
    return result


def _assertion_verdicts(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Every ``{type, passed, detail}`` recorded on a case entry, across its repeats."""
    return [
        check for repeat in case.get("repeats") or [] for check in repeat.get("assertions") or []
    ]


def _check_repeat(outcome: RunOutcome, checks: list[Any]) -> list[dict[str, Any]]:
    """Run a case's assertions against one repeat's answer, details clipped."""
    verdicts = assertions.evaluate(
        checks,
        output=outcome.output,
        tool_transcript=outcome.tool_transcript,
        duration_ms=outcome.duration_ms,
    )
    for verdict in verdicts:
        verdict["detail"] = _clip(verdict["detail"], MAX_ASSERTION_DETAIL_BYTES)
    return verdicts


def _suite_case_result(
    case_id: str,
    runs: list[RunOutcome],
    verdict: grader.CaseVerdict,
    judge_error: str | None,
    pass_threshold: float,
    *,
    checks: list[Any] | None = None,
    judged: bool = True,
) -> dict[str, Any]:
    """One case's line in a suite result.

    **Every repeat counts.** ``scores`` has one entry per repeat, in run order:
    the judge's score; ``0.0`` for a repeat that failed to run, because the
    application did not answer that time; or ``None`` for one that answered but
    was never judged. The case score is the mean over all of them. Averaging
    only the answers that came back would let one good answer in ten attempts
    pass -- exactly the flakiness repeats exist to expose.

    ``repeats`` is the same list with each repeat's run attached --
    ``{"run_id", "ran", "score"}`` in run order -- so a failed repeat keeps its
    place (and its run, when one was recorded) instead of the successful ones
    being renumbered. ``run_ids`` stays the successful runs only.

    ``status`` is ``passed`` / ``failed`` when every repeat has a score,
    ``error`` when no repeat produced an answer at all, and ``judge_error`` when
    any answer went unjudged: that evidence is missing, and it is neither a pass
    nor a zero. Only a fully scored case can pass.

    **Assertions.** A case with ``checks`` (its ``assert:`` list) runs them
    against every repeat that answered: each repeat gains ``assertions`` (one
    ``{type, passed, detail}`` per check, in the case's order; ``None`` for a
    repeat that did not run) and ``assertions_passed``. The case can then pass
    only if every repeat that answered passed every check *and* its score meets
    ``pass_threshold``; one failed check makes the case ``failed`` even when the
    judge could not score it, because that much is already certain. A case with
    ``judged=False`` never reaches the judge: each repeat that answered scores
    ``1.0`` if all its checks passed and ``0.0`` if not. A case with checks also
    carries ``judged`` and an ``assertions: {total, passed, failed}`` tally over
    every check on every repeat; a case without checks carries none of these
    fields, so its line is exactly what it was before assertions existed.
    """
    checks = list(checks or [])
    ordered = sorted(runs, key=lambda outcome: outcome.index)
    succeeded = [outcome for outcome in ordered if outcome.succeeded]
    checked: list[list[dict[str, Any]] | None] = [
        _check_repeat(outcome, checks) if checks and outcome.succeeded else None
        for outcome in ordered
    ]
    repeat_passed = [
        None if found is None else all(check["passed"] for check in found) for found in checked
    ]

    def repeat_score(outcome: RunOutcome, passed: bool | None) -> float | None:
        if not outcome.succeeded:
            return 0.0
        if not judged:
            return 1.0 if passed else 0.0
        return verdict.scores.get(outcome.index)

    scores = [
        repeat_score(outcome, passed)
        for outcome, passed in zip(ordered, repeat_passed, strict=True)
    ]
    repeats: list[dict[str, Any]] = []
    for outcome, score, found, passed in zip(ordered, scores, checked, repeat_passed, strict=True):
        repeat: dict[str, Any] = {
            "run_id": outcome.run_id,
            "ran": outcome.succeeded,
            "score": None if score is None else round(score, 4),
        }
        if checks:
            repeat["assertions"] = found
            repeat["assertions_passed"] = passed
        repeats.append(repeat)

    entry: dict[str, Any] = {
        "id": case_id,
        "run_ids": [outcome.run_id for outcome in succeeded],
        "repeats": repeats,
        "runs": {"total": len(runs), "succeeded": len(succeeded)},
        "passed": False,
        "score": None,
        "scores": [None if value is None else round(value, 4) for value in scores],
        "reasoning": None,
        "error": None,
    }
    if checks:
        every = [check for found in checked if found for check in found]
        failures = sum(1 for check in every if not check["passed"])
        entry["judged"] = judged
        entry["assertions"] = {
            "total": len(every),
            "passed": len(every) - failures,
            "failed": failures,
        }
    assertions_failed = any(passed is False for passed in repeat_passed)
    if verdict.spread is not None:
        entry["judge_spread"] = round(verdict.spread, 4)
    if verdict.reasons:
        entry["reasoning"] = _clip(" | ".join(verdict.reasons))
    if not succeeded:
        entry["status"] = "error"
        entry["error"] = next(
            (outcome.error for outcome in runs if outcome.error),
            {"code": "not_run", "message": "No run of this case completed"},
        )
        return entry
    if any(value is None for value in scores):
        unjudged = [outcome.index for outcome in succeeded if outcome.index not in verdict.scores]
        message = next(
            (verdict.judge_errors[index] for index in unjudged if index in verdict.judge_errors),
            judge_error or "The judge returned no verdict",
        )
        entry["error"] = {"code": "judge_error", "message": message}
        entry["status"] = "failed" if assertions_failed else "judge_error"
        return entry

    score = sum(value for value in scores if value is not None) / len(scores)
    entry["passed"] = score >= pass_threshold and not assertions_failed
    entry["status"] = "passed" if entry["passed"] else "failed"
    entry["score"] = round(score, 4)
    return entry


def _suite_summary(cases: list[dict[str, Any]], pass_threshold: float) -> str:
    """The one-paragraph ``reasoning`` of a suite: counts, then the names that matter.

    Per-case reasoning lives on each case; repeating a hundred of them here
    would only make the headline unreadable (and the result larger).
    """
    passed = sum(1 for case in cases if case["passed"])
    parts = [f"{passed}/{len(cases)} cases passed (threshold {pass_threshold:.2f})"]
    labels = (("failed", "failed"), ("error", "did not run"), ("judge_error", "not judged"))
    for status, label in labels:
        ids = [case["id"] for case in cases if case["status"] == status]
        if ids:
            parts.append(f"{label}: {', '.join(ids)}")
    return "; ".join(parts)


def _build_suite_result(
    outcomes: list[RunOutcome],
    judged: grader.SuiteJudgement,
    request: EvaluationRequest,
) -> dict[str, Any]:
    """The result of ``kind="suite"``: per-case verdicts plus the suite's totals.

    The headline ``score`` is the mean over the cases the judge scored; the
    stricter number is ``metrics.pass_rate``, which counts a case that did not
    run or was not judged as not passed.
    """
    suite = request.suite
    assert suite is not None
    runs_by_case: dict[str, list[RunOutcome]] = {case.id: [] for case in suite.cases}
    for outcome in outcomes:
        runs_by_case.setdefault(str(outcome.case_id), []).append(outcome)

    cases = [
        _suite_case_result(
            case.id,
            runs_by_case[case.id],
            judged.verdicts.get(case.id, grader.CaseVerdict()),
            judged.error,
            suite.pass_threshold,
            checks=case.assert_,
            judged=case.judge,
        )
        for case in suite.cases
    ]
    scored = [case["score"] for case in cases if case["score"] is not None]
    mean = sum(scored) / len(scored) if scored else None
    score_100 = None if mean is None else max(0, min(100, round(mean * 100)))
    passed = sum(1 for case in cases if case["passed"])

    result: dict[str, Any] = {
        "grade": None if score_100 is None else rubrics.score_to_grade(score_100),
        "score": score_100,
        "reasoning": _suite_summary(cases, suite.pass_threshold),
        "judge": {
            "model_id": request.grader.model_id,
            "system_prompt_used": request.grader.system_prompt is not None,
            "rubric_used": request.rubric is not None,
            **({"panel": _judge_labels(request.panel)} if request.panel else {}),
        },
        "metrics": {
            "pass_rate": passed / len(cases),
            "cases_total": len(cases),
            "cases_passed": passed,
            "cases_failed": sum(1 for case in cases if case["status"] == "failed"),
            "cases_errored": sum(1 for case in cases if case["status"] in ("error", "judge_error")),
            "judge_overall_score": mean,
            **_assertion_metrics(cases),
            **_latency_metrics(outcomes),
        },
        "run_ids": [outcome.run_id for outcome in outcomes if outcome.succeeded],
        "failed_runs": [
            {
                "index": outcome.index,
                "case_id": outcome.case_id,
                "run_id": outcome.run_id,
                "error": outcome.error,
            }
            for outcome in outcomes
            if not outcome.succeeded
        ],
        "suite": {
            "name": suite.name,
            "repeats": suite.repeats,
            "pass_threshold": suite.pass_threshold,
        },
        "cases": cases,
    }
    if suite.calibrate:
        result["calibration"] = _calibration(cases, judged.calibration, suite.pass_threshold)
    if judged.error is not None:
        result["judge_error"] = judged.error
    return _fit_suite_result(result)


def _calibration(
    cases: list[dict[str, Any]],
    probes: dict[str, dict[str, float | None]],
    pass_threshold: float,
) -> dict[str, Any]:
    """How the judge scored each case's calibration probes, and which look wrong.

    Each calibrated case gets ``calibration: {"reference", "empty"}`` (a score,
    or ``None`` when that probe went unjudged; ``reference`` only when the case
    has an ``expected`` answer). The suite-level summary flags a probe on the
    wrong side of ``pass_threshold``: a reference answer the judge would fail,
    or an empty answer it would pass. Either means this rubric and judge cannot
    be trusted to separate right from wrong on that case. Nothing here changes
    a case's verdict or score.
    """
    flagged: list[dict[str, Any]] = []
    for case in cases:
        scores = probes.get(case["id"])
        if scores is None:
            continue
        case["calibration"] = {
            probe: None if score is None else round(score, 4) for probe, score in scores.items()
        }
        reference, empty = scores.get("reference"), scores.get("empty")
        if reference is not None and reference < pass_threshold:
            flagged.append({"id": case["id"], "probe": "reference", "score": round(reference, 4)})
        if empty is not None and empty >= pass_threshold:
            flagged.append({"id": case["id"], "probe": "empty", "score": round(empty, 4)})

    def mean(probe: str) -> float | None:
        scores = [score for scores in probes.values() if (score := scores.get(probe)) is not None]
        return round(sum(scores) / len(scores), 4) if scores else None

    return {
        "cases": len(probes),
        "reference_mean": mean("reference"),
        "empty_mean": mean("empty"),
        "flagged": flagged,
    }


def _latency_metrics(outcomes: list[RunOutcome]) -> dict[str, dict[str, int]]:
    """``latency_ms: {p50, p95}`` over the runs that answered; nothing when none did.

    Nearest-rank percentiles of each run's wall-clock time, so a fallback that
    answers as well but twice as slowly shows up next to its pass rate.
    """
    durations = sorted(outcome.duration_ms for outcome in outcomes if outcome.succeeded)
    if not durations:
        return {}

    def percentile(fraction: float) -> int:
        return durations[max(0, math.ceil(fraction * len(durations)) - 1)]

    return {"latency_ms": {"p50": percentile(0.50), "p95": percentile(0.95)}}


def _assertion_metrics(cases: list[dict[str, Any]]) -> dict[str, int]:
    """``assertions_total`` / ``assertions_failed``; nothing when no case asserts."""
    tallies = [case["assertions"] for case in cases if "assertions" in case]
    if not tallies:
        return {}
    return {
        "assertions_total": sum(tally["total"] for tally in tallies),
        "assertions_failed": sum(tally["failed"] for tally in tallies),
    }


async def _judge_suite(
    successes: list[RunOutcome],
    request: EvaluationRequest,
    seam: _Seam,
    *,
    judge_factory: JudgeFactory | None = None,
) -> grader.SuiteJudgement:
    """Judge a suite's answers, leaving out the cases that opted out of the judge.

    A suite in which every case opted out never builds a judge at all -- which
    is also what lets an assertion-only suite run without judge credentials.
    ``judge_factory`` defaults to the deps' own; the engine passes the metered
    one so judge spend is counted (:mod:`nimbus.evals.budget`).
    """
    suite = request.suite
    assert suite is not None
    skipped = {case.id for case in suite.cases if not case.judge}
    to_judge = [outcome for outcome in successes if outcome.case_id not in skipped]
    if successes and not to_judge:
        return grader.SuiteJudgement()
    factory = judge_factory or seam.deps.judge_factory
    judges = [request.grader, *_panel_members(request)]
    judgements = await asyncio.gather(
        *(
            grader.judge_suite(
                to_judge,
                suite=suite,
                rubric=request.rubric,
                grader=judge,
                judge_factory=factory,
            )
            for judge in judges
        )
    )
    return grader.combine_judgements(
        list(zip(_judge_labels(judges), judgements, strict=True))
    )


def _panel_members(request: EvaluationRequest) -> list[GraderConfig]:
    """The panel's judges, each defaulting to the primary judge's system prompt."""
    return [
        member
        if member.system_prompt is not None
        else member.model_copy(update={"system_prompt": request.grader.system_prompt})
        for member in request.panel
    ]


def _judge_labels(judges: list[GraderConfig]) -> list[str]:
    return [f"{judge.provider}:{judge.model_id}" for judge in judges]


def _terminal(
    seam: _Seam,
    status: str,
    outcomes: list[RunOutcome],
    result: dict[str, Any] | None,
    error: dict[str, Any] | None,
) -> dict[str, Any]:
    """The value :func:`execute_evaluation_with_seam` returns: the final state."""
    return {
        "evaluation_id": seam.evaluation_id,
        "status": status,
        "result": result,
        "error": error,
        "run_ids": [outcome.run_id for outcome in outcomes if outcome.succeeded],
    }


def _settle_cancelled(seam: _Seam, outcomes: list[RunOutcome]) -> dict[str, Any]:
    """Persist + announce a cancellation. Deliberately contains no ``await``.

    On the ``asyncio`` cancellation path the task is *already* cancelled, so any
    suspension point here would re-raise before the state was saved.
    """
    successes = [outcome for outcome in outcomes if outcome.succeeded]
    seam.store.save_evaluation(
        status="cancelled",
        run_ids=[outcome.run_id for outcome in successes],
    )
    seam.publish(EvalCompleteEvent(status="cancelled", result=None))
    return _terminal(seam, "cancelled", outcomes, None, None)


# --------------------------------------------------------------------------- #
# The execution core
# --------------------------------------------------------------------------- #


async def execute_evaluation_with_seam(
    request: EvaluationRequest | dict[str, Any],
    emit: EmitFn,
    store: EvalStore,
    cancelled: CancelledFn | None = None,
    *,
    deps: EvalDeps | None = None,
    evaluation_id: str | None = None,
) -> dict[str, Any]:
    """Execute + grade one evaluation against an injected emitter and store.

    This is the whole evaluation, lane-agnostic. ``emit`` publishes each
    progress event as a plain dict, ``store`` publishes finished runs and reads
    stored ones, and ``cancelled`` is polled between runs and before grading.

    ``request`` may be an :class:`EvaluationRequest` or the plain dict an
    out-of-process caller received on the wire -- validation belongs to the
    engine either way. ``evaluation_id`` defaults to ``store.evaluation_id``
    and ``deps`` to :func:`default_deps`, so a host can call this with just the
    four documented arguments.

    Returns the terminal state::

        {"evaluation_id", "status", "result", "error", "run_ids"}

    An ``asyncio`` cancellation is persisted and announced, then re-raised (the
    local lane's cancel path); a cooperative ``cancelled()`` is persisted,
    announced, and *returned* as ``status="cancelled"``.
    """
    seam = _Seam(
        evaluation_id=evaluation_id or getattr(store, "evaluation_id", ""),
        emit=emit,
        store=store,
        cancelled=cancelled or (lambda: False),
        deps=deps or default_deps(),
    )
    outcomes: list[RunOutcome] = []
    judge_meter: budget.JudgeMeter | None = None
    try:
        if not isinstance(request, EvaluationRequest):
            request = EvaluationRequest.model_validate(request)
        batch = planned_jobs(request)
        seam.ledger = budget.CostLedger(
            request.max_cost_usd,
            prior_per_run=budget.prior_per_run(job.run_config for job in batch),
        )
        judge_meter = budget.JudgeMeter(seam.deps.judge_factory)

        seam.store.save_evaluation(status="running")
        seam.publish(
            EvalStartEvent(
                evaluation_id=seam.evaluation_id, kind=request.kind, n=request.planned_runs
            )
        )

        if request.kind in ("determinism", "suite"):
            await _execute_batch(seam, batch, outcomes)
        else:
            outcomes.extend(_load_stored_runs(seam, request.run_ids))

        successes = [outcome for outcome in outcomes if outcome.succeeded]
        seam.store.save_evaluation(
            run_ids=[outcome.run_id for outcome in successes],
            progress={
                "completed": len(successes),
                "failed": len(outcomes) - len(successes),
                "total": request.planned_runs,
            },
        )

        if seam.cancelled():
            raise _CooperativeCancel

        seam.publish(GradingStartedEvent())
        if request.kind == "suite":
            suite_judgement = await _judge_suite(
                successes, request, seam, judge_factory=judge_meter.factory
            )
            result = _build_suite_result(outcomes, suite_judgement, request)
        else:
            judged = await grader.judge(
                successes,
                kind=request.kind,
                rubric=request.rubric,
                grader=request.grader,
                judge_factory=judge_meter.factory,
            )
            result = _build_result(outcomes, judged, request)
        result = budget.annotate(result, request, seam.ledger, judge_meter)
        seam.publish(GradingCompletedEvent(result=result))

        status = "completed" if successes else "error"
        error = None if successes else {"code": "no_successful_runs", "message": "Every run failed"}
        seam.store.save_evaluation(status=status, result=result, error=error)
        seam.publish(EvalCompleteEvent(status=status, result=result))
        return _terminal(seam, status, outcomes, result, error)

    except asyncio.CancelledError:
        logger.info("evaluation %s cancelled", seam.evaluation_id)
        _settle_cancelled(seam, outcomes)
        raise

    except _CooperativeCancel:
        logger.info("evaluation %s cancelled", seam.evaluation_id)
        return _settle_cancelled(seam, outcomes)

    except Exception as exc:
        logger.exception("evaluation %s failed", seam.evaluation_id)
        error = {"code": "internal_error", "message": str(exc) or exc.__class__.__name__}
        seam.store.save_evaluation(status="error", error=error)
        seam.publish(EvalCompleteEvent(status="error", result=None))
        return _terminal(seam, "error", outcomes, None, error)

    finally:
        _settle_spend(seam, request, judge_meter)


def _settle_spend(
    seam: _Seam, request: EvaluationRequest | dict[str, Any], judge_meter: budget.JudgeMeter | None
) -> None:
    """Swap the evaluation's spend reservation for what it actually cost.

    Runs however the evaluation ended (finished, failed, cancelled): a cancelled
    one still spent what it spent. Never raises: an accounting failure must not
    turn a finished evaluation into a failed one, so it is logged instead.
    """
    principal = getattr(request, "principal", None)
    if principal is None:
        return
    actual = seam.ledger.spent + ((judge_meter.cost_usd() or 0.0) if judge_meter else 0.0)
    try:
        spend_module.settle(seam.deps.spend_store(), principal, actual)
    except Exception:
        logger.exception("could not settle spend for evaluation %s", seam.evaluation_id)


# --------------------------------------------------------------------------- #
# The local lane: one asyncio task, an in-process job log, SQLite
# --------------------------------------------------------------------------- #


async def run_evaluation(
    job: EvalJob, evaluation_id: str, request: EvaluationRequest, deps: EvalDeps
) -> None:
    """Run one evaluation locally: job log as emitter, the history repo as store.

    The whole local lane is this adapter -- everything else lives in
    :func:`execute_evaluation_with_seam`.
    """
    try:
        await execute_evaluation_with_seam(
            request,
            job.emit_entry,
            LocalEvalStore(evaluation_id, deps.history_repo()),
            lambda: job.cancelled,
            deps=deps,
            evaluation_id=evaluation_id,
        )
    finally:
        job.close()


def start(evaluation_id: str, request: EvaluationRequest, deps: EvalDeps) -> EvalJob:
    """Register the job for ``evaluation_id`` and start it in the background."""
    job = jobs.register(evaluation_id)
    job.task = asyncio.create_task(run_evaluation(job, evaluation_id, request, deps))
    return job
