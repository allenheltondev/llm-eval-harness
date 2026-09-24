"""The body of an evaluation: execute, grade, persist, report.

The execution core is :func:`execute_evaluation_with_seam`, which knows how to
run and grade an evaluation but nothing at all about *where* progress events go
or *where* records are persisted. Those two capabilities are injected:

``emit(event_dict)``
    Publish one progress event, already in its NDJSON wire shape (the dict
    :meth:`evalharness.evals.events._Event.as_log_entry` produces). Synchronous.
``store``
    An :class:`EvalStore` -- evaluation-row updates plus run load/save.
``cancelled()``
    Polled between runs and once more before grading; ``True`` settles the
    evaluation as ``cancelled`` exactly as an ``asyncio`` cancellation would.

Two lanes share that core:

*local*
    ``POST /evaluations`` with ``execution="local"`` (the default). One
    ``asyncio`` task per evaluation; ``emit`` is the in-process job's event log
    (:mod:`evalharness.evals.jobs`) and ``store`` is this server's history
    repository (:class:`LocalEvalStore`). :func:`run_evaluation` is the whole
    adapter.
*cloud*
    The worker Lambda outside this process, whose ``emit`` appends
    DynamoDB ``EVENT#`` items and whose ``store`` writes DynamoDB ``META`` /
    ``RUN#`` items. See ``docs/cloud-evals.md`` and
    :mod:`evalharness.evals.cloud`; the worker imports
    :func:`execute_evaluation_with_seam` directly.

Determinism
-----------
The same ``run_config`` is executed ``n`` times through the ordinary run engine
(:func:`evalharness.engine.runner.execute_run`) -- each repetition persists its
own run row, so every repeat is inspectable in ``/runs`` afterwards. At most
:data:`MAX_CONCURRENT_RUNS` repeats are in flight at once.

A repeat whose in-band error is throttle-classified (``model_throttled``, from
``engine.model_factory.classify_error``) is retried, sleeping
:data:`RETRY_BACKOFF_SECONDS` between attempts -- a module-level tuple so tests
can shorten it. Retries keep the repeat's index; only the successful attempt's
run id is kept. Anything still failing after the last attempt is reported as
``run_failed``, excluded from grading, listed in ``result.failed_runs``, and the
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
import time
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, NamedTuple, Protocol

from evalharness.config import Settings, get_settings
from evalharness.engine.events import ErrorEvent, RunCompleteEvent, RunStartEvent
from evalharness.engine.model_factory import ModelFactory, build_model, classify_error
from evalharness.engine.runner import execute_run
from evalharness.engine.schemas import RunRequest
from evalharness.errors import AppError
from evalharness.evals import grader, jobs, rubrics
from evalharness.evals.events import (
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
from evalharness.evals.jobs import EvalJob
from evalharness.evals.judge import JudgeFactory, build_judge_model
from evalharness.evals.metrics import local_metrics
from evalharness.evals.outcomes import RunOutcome
from evalharness.evals.schemas import EvaluationRequest, Suite
from evalharness.providers import DEFAULT_PROVIDER
from evalharness.store import history
from evalharness.store.repo import HistoryRepo, get_history_repo

logger = logging.getLogger(__name__)

MAX_CONCURRENT_RUNS = 3
#: Sleep between throttle retries; index 0 is used after the first failure.
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (5.0, 10.0)
THROTTLE_ERROR_CODE = "model_throttled"


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
    (:class:`evalharness.worker.ddb.DynamoEvalStore`, restated for that side as
    :class:`evalharness.worker.interfaces.RunStore`).
    """

    #: The evaluation this store is bound to -- every ``save_evaluation`` targets it.
    evaluation_id: str

    def save_evaluation(self, **fields: Any) -> None:
        """Partially update the evaluation record (``status``, ``result``, ...)."""
        ...

    def load_run(self, run_id: str) -> Any:
        """Read a run record back. Raises ``NotFoundError`` if it is gone.

        Returns anything with the :class:`~evalharness.store.history.RunRecord`
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
    try:
        async with aclosing(events):
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
    except Exception as exc:  # pragma: no cover - defensive
        code, message, retryable = classify_error(exc)
        outcome.error = {"code": code, "message": message, "retryable": retryable}

    outcome.duration_ms = int((time.perf_counter() - started) * 1000)
    if outcome.run_id is not None:
        record = store.load_run(outcome.run_id)
        outcome.tool_transcript = list(record.tool_transcript or [])
        if outcome.status != "completed":
            outcome.error = outcome.error or record.error
    return outcome


def _is_throttle(outcome: RunOutcome) -> bool:
    return bool(outcome.error) and outcome.error.get("code") == THROTTLE_ERROR_CODE


async def _execute_with_retries(
    job: _Job,
    deps: EvalDeps,
    store: EvalStore | None = None,
) -> RunOutcome:
    """Execute one run, retrying throttled attempts with backoff."""
    outcome = await _execute_once(job, deps, store)
    for attempt, backoff in enumerate(RETRY_BACKOFF_SECONDS, start=1):
        if outcome.succeeded or not _is_throttle(outcome):
            break
        logger.info("eval run %d throttled, retrying in %.1fs", job.index, backoff)
        await asyncio.sleep(backoff)
        outcome = await _execute_once(job, deps, store)
        outcome.attempts = attempt + 1
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
            if seam.cancelled():
                return
            seam.publish(RunStartedEvent(index=job.index, case_id=job.case_id))
            outcome = await _execute_with_retries(job, seam.deps, seam.store)
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
            {"index": outcome.index, "error": outcome.error}
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

    Judge reasoning, run error messages and the judge error are the only parts
    whose size the harness does not control; everything else is bounded by the
    suite limits. When any of it had to be cut further than the per-case
    limit, ``truncated`` says so, so a reader knows the prose is partial.
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


def _suite_case_result(
    case_id: str,
    runs: list[RunOutcome],
    verdict: grader.CaseVerdict,
    judge_error: str | None,
    pass_threshold: float,
) -> dict[str, Any]:
    """One case's line in a suite result.

    **Every repeat counts.** ``scores`` has one entry per repeat, in run order:
    the judge's score; ``0.0`` for a repeat that failed to run, because the
    application did not answer that time; or ``None`` for one that answered but
    was never judged. The case score is the mean over all of them. Averaging
    only the answers that came back would let one good answer in ten attempts
    pass -- exactly the flakiness repeats exist to expose.

    ``status`` is ``passed`` / ``failed`` when every repeat has a score,
    ``error`` when no repeat produced an answer at all, and ``judge_error`` when
    any answer went unjudged: that evidence is missing, and it is neither a pass
    nor a zero. Only a fully scored case can pass.
    """
    ordered = sorted(runs, key=lambda outcome: outcome.index)
    succeeded = [outcome for outcome in ordered if outcome.succeeded]
    scores: list[float | None] = [
        0.0 if not outcome.succeeded else verdict.scores.get(outcome.index) for outcome in ordered
    ]
    entry: dict[str, Any] = {
        "id": case_id,
        "run_ids": [outcome.run_id for outcome in succeeded],
        "runs": {"total": len(runs), "succeeded": len(succeeded)},
        "passed": False,
        "score": None,
        "scores": [None if value is None else round(value, 4) for value in scores],
        "reasoning": None,
        "error": None,
    }
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
        entry["status"] = "judge_error"
        unjudged = [outcome.index for outcome in succeeded if outcome.index not in verdict.scores]
        message = next(
            (verdict.judge_errors[index] for index in unjudged if index in verdict.judge_errors),
            judge_error or "The judge returned no verdict",
        )
        entry["error"] = {"code": "judge_error", "message": message}
        return entry

    score = sum(value for value in scores if value is not None) / len(scores)
    entry["passed"] = score >= pass_threshold
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
        },
        "metrics": {
            "pass_rate": passed / len(cases),
            "cases_total": len(cases),
            "cases_passed": passed,
            "cases_failed": sum(1 for case in cases if case["status"] == "failed"),
            "cases_errored": sum(1 for case in cases if case["status"] in ("error", "judge_error")),
            "judge_overall_score": mean,
        },
        "run_ids": [outcome.run_id for outcome in outcomes if outcome.succeeded],
        "failed_runs": [
            {"index": outcome.index, "case_id": outcome.case_id, "error": outcome.error}
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
    if judged.error is not None:
        result["judge_error"] = judged.error
    return _fit_suite_result(result)


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
    try:
        if not isinstance(request, EvaluationRequest):
            request = EvaluationRequest.model_validate(request)

        seam.store.save_evaluation(status="running")
        seam.publish(
            EvalStartEvent(
                evaluation_id=seam.evaluation_id, kind=request.kind, n=request.planned_runs
            )
        )

        if request.kind == "determinism":
            await _execute_batch(seam, _determinism_jobs(request), outcomes)
        elif request.kind == "suite":
            assert request.suite is not None
            await _execute_batch(seam, _suite_jobs(request.suite), outcomes)
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
            assert request.suite is not None
            suite_judgement = await grader.judge_suite(
                successes,
                suite=request.suite,
                rubric=request.rubric,
                grader=request.grader,
                judge_factory=seam.deps.judge_factory,
            )
            result = _build_suite_result(outcomes, suite_judgement, request)
        else:
            judged = await grader.judge(
                successes,
                kind=request.kind,
                rubric=request.rubric,
                grader=request.grader,
                judge_factory=seam.deps.judge_factory,
            )
            result = _build_result(outcomes, judged, request)
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
