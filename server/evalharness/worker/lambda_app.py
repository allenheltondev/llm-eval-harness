"""The Lambda entry point for the cloud evaluation lane.

Deployed as an ordinary Python Lambda (``EvalWorkerFunction`` in
``infra/template.yaml``) and invoked **asynchronously** by the FastAPI server:
``Invoke`` with ``InvocationType="Event"`` returns as soon as AWS has accepted
the event, so the server never waits for an evaluation and never holds a
connection open for one. Progress is read back from DynamoDB exactly as
``docs/cloud-evals.md`` specifies.

That async invoke is what replaced the previous design's ack-then-background
dance. On AgentCore the handler had to return in under a second while the job
kept running behind it, kept alive by health reporting. A Lambda cannot do
that -- the execution environment is frozen the moment the handler returns --
so the whole evaluation runs *inside* the invocation, and the queueing is AWS's
job rather than ours. One less moving part.

The 15-minute ceiling
---------------------
Lambda's hard limit is 900 seconds, and an evaluation can legitimately want
longer: up to 25 repeats at a concurrency of 3, plus grading, plus throttle
backoff. Being killed at the ceiling would be the worst outcome -- the
execution environment vanishes mid-write and the evaluation sits at ``running``
until its 90-day TTL, with nothing to tell the reader it is never coming back.

So the deadline is cooperative *first*. :class:`Deadline` watches the
invocation's own remaining time and, with :data:`DEADLINE_MARGIN_SECONDS` to
spare, reports itself through the engine's ``cancelled`` seam -- polled between
runs and before grading. The engine stops at a clean boundary with every
finished run already persisted, and the worker then settles the evaluation as
``error`` with ``deadline_exceeded`` rather than the ``cancelled`` the engine
would otherwise record, because nobody cancelled it.

Asking is not enough on its own, because ``cancelled`` is polled *between*
runs and nowhere inside one. A single streaming model call, or the whole
grading phase, can begin just inside the margin and run for minutes unpolled.
So the evaluation is additionally bounded by ``asyncio.wait_for`` at
:data:`HARD_DEADLINE_RESERVE_SECONDS`, which cancels in-flight work instead of
requesting it. The smaller reserve is what makes the cooperative stop the one
that normally fires: it keeps the run in progress, where the hard stop loses
it.

Duplicate deliveries
--------------------
Asynchronous invocation is at-least-once, and ``MaximumRetryAttempts: 0`` does
not change that -- it governs retries after a *failure*. So the conditional
``pending`` -> ``running`` update is an ownership claim: exactly one delivery
can win it, and a delivery that loses executes nothing and writes nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from evalharness.worker import interfaces
from evalharness.worker.ddb import DynamoEvalStore

logger = logging.getLogger(__name__)

__all__ = [
    "DEADLINE_MARGIN_SECONDS",
    "HARD_DEADLINE_RESERVE_SECONDS",
    "Deadline",
    "build_store",
    "execute",
    "handler",
]

#: Seconds of the invocation reserved for settling up, as seen by the
#: *cooperative* stop. Checked only between runs and before grading, so it also
#: has to absorb the tail of whatever run is in flight when the deadline
#: passes -- which is exactly the part it cannot bound. See
#: :data:`HARD_DEADLINE_RESERVE_SECONDS`.
DEADLINE_MARGIN_SECONDS = 60.0

#: Seconds reserved for the terminal DynamoDB writes after the *hard* stop.
#:
#: The cooperative stop is a request; this is the enforcement. A single
#: streaming model call, or the whole grading phase, can start with just over
#: ``DEADLINE_MARGIN_SECONDS`` left and run for minutes -- and nothing polls
#: ``cancelled`` while it does. Lambda would then kill the environment
#: mid-write and the evaluation would read ``running`` until its 90-day TTL,
#: which is the precise outcome the deadline exists to prevent. So the whole
#: evaluation is additionally bounded by ``asyncio.wait_for``, leaving this
#: much of the invocation to write the terminal row.
#:
#: Smaller than the cooperative margin on purpose: the cooperative stop must
#: get its chance first, because stopping at a run boundary keeps more work
#: than being cancelled mid-flight.
HARD_DEADLINE_RESERVE_SECONDS = 25.0

#: What the evaluation settles as when the invocation runs out of time.
DEADLINE_ERROR_CODE = "deadline_exceeded"


class Deadline:
    """Reports when an invocation is close enough to its limit to stop.

    ``remaining()`` returns seconds left, straight from Lambda's context. The
    verdict is latched: once tripped it stays tripped, so the engine cannot see
    it flicker between polls and the worker can ask afterwards whether *this*
    is why the run stopped.
    """

    def __init__(
        self,
        remaining: Callable[[], float],
        margin: float = DEADLINE_MARGIN_SECONDS,
    ) -> None:
        self._remaining = remaining
        self._margin = margin
        self.tripped = False

    def seconds_left(self) -> float | None:
        """Seconds until the invocation is killed, or ``None`` if unreadable."""
        try:
            return self._remaining()
        except Exception:  # noqa: BLE001 - a broken clock must not stop an evaluation
            logger.exception("could not read the invocation's remaining time")
            return None

    def budget(self, reserve: float = HARD_DEADLINE_RESERVE_SECONDS) -> float | None:
        """How long work may run before the terminal write must start.

        ``None`` when the remaining time cannot be read, which means "do not
        impose a hard bound" -- a broken clock must not cut an evaluation
        short.
        """
        left = self.seconds_left()
        if left is None:
            return None
        return max(0.0, left - reserve)

    def expired(self) -> bool:
        if self.tripped:
            return True
        try:
            left = self._remaining()
        except Exception:  # noqa: BLE001 - a broken clock must not stop an evaluation
            logger.exception("could not read the invocation's remaining time")
            return False
        if left <= self._margin:
            logger.warning("deadline reached with %.1fs left; stopping cleanly", left)
            self.tripped = True
        return self.tripped


def deadline_from(context: Any) -> Deadline:
    """A :class:`Deadline` reading the Lambda context's remaining time.

    Falls back to the function's configured timeout counted from now when the
    context has no ``get_remaining_time_in_millis`` -- a direct invocation in a
    test or a local run -- so the guard is never silently absent.
    """
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if callable(getter):
        return Deadline(lambda: getter() / 1000.0)

    budget = float(os.environ.get("EVAL_WORKER_TIMEOUT_SECONDS", "900"))
    started = time.monotonic()
    return Deadline(lambda: budget - (time.monotonic() - started))


def build_store(evaluation_id: str) -> DynamoEvalStore:
    """Construct the DynamoDB store from the function's environment.

    ``TABLE_NAME`` is set by ``infra/template.yaml``; ``AWS_REGION`` is set by
    Lambda itself, so the fallback only matters running the artifact locally.
    """
    return DynamoEvalStore(
        table_name=os.environ.get("TABLE_NAME", ""),
        evaluation_id=evaluation_id,
        region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
    )


def _rejected(code: str, message: str) -> dict[str, Any]:
    """The result body for an invocation we refuse to start."""
    logger.warning("rejecting invocation: %s: %s", code, message)
    return {"status": "rejected", "error": {"code": code, "message": message}}


def _duplicate(evaluation_id: str) -> dict[str, Any]:
    """The result body for a delivery that did not win the ownership claim.

    Not an error: the evaluation is running (or already finished) under another
    delivery, and its state in DynamoDB is correct. Nothing is written here,
    because writing anything would step on the owner.
    """
    logger.warning("eval %s already claimed; skipping duplicate delivery", evaluation_id)
    return {"status": "duplicate", "evaluation_id": evaluation_id, "execution": "cloud"}


# --------------------------------------------------------------------------- #
# The job
# --------------------------------------------------------------------------- #


async def execute(
    evaluation_id: str,
    request: dict[str, Any],
    store: DynamoEvalStore,
    deadline: Deadline | None = None,
) -> str:
    """Run one evaluation to a terminal state and report the status it settled at.

    Never raises. An escaping exception would leave the evaluation at
    ``running`` forever, since nothing downstream is watching this invocation --
    the server reads DynamoDB. Every path ends in a terminal ``META`` write,
    which is what releases the reader polling ``GET /evaluations/{id}/events``.
    """
    try:
        if store.cancel_requested():
            # Cancelled between the server's POST and our first check.
            store.complete("cancelled", run_ids=[])
            return "cancelled"

        cancelled = _stop_condition(store, deadline)
        try:
            outcome = await asyncio.wait_for(
                interfaces.run_evaluation(request, store.emit, store, cancelled),
                timeout=None if deadline is None else deadline.budget(),
            )
        except TimeoutError:
            # The cooperative stop was asked for and did not get taken: the
            # engine was inside a single run, or inside grading, where nothing
            # polls `cancelled`. Cancelling mid-flight loses the run in
            # progress, but the finished ones are already in DynamoDB and the
            # row settles honestly -- which beats being killed by Lambda and
            # reading `running` forever.
            logger.warning("eval %s hit the hard deadline mid-flight", evaluation_id)
            store.complete(
                "error",
                error={
                    "code": DEADLINE_ERROR_CODE,
                    "message": (
                        "The evaluation did not finish inside the worker's "
                        "15-minute limit and was stopped while a run was still "
                        "in flight. Completed runs are preserved; re-run with a "
                        "smaller n."
                    ),
                },
                run_ids=store.saved_run_ids,
            )
            return "error"

        if deadline is not None and deadline.tripped:
            # The engine stopped because we asked it to, and it settles that as
            # `cancelled`. Nobody cancelled this. Overwrite the buffered
            # terminal state before flushing it, so the row says what actually
            # happened and keeps the runs that did finish.
            store.complete(
                "error",
                error={
                    "code": DEADLINE_ERROR_CODE,
                    "message": (
                        "The evaluation did not finish inside the worker's "
                        "15-minute limit and was stopped at a run boundary. "
                        "Completed runs are preserved; re-run with a smaller n."
                    ),
                },
                run_ids=outcome["run_ids"],
            )
            logger.warning("eval %s stopped at the deadline", evaluation_id)
            return "error"

        # The engine settles the row itself (`save_evaluation(status=...)`),
        # which the store buffers until `eval_complete` is durable; `finalize`
        # flushes it. A `False` return means the engine returned without
        # settling at all, so the worker settles from the outcome instead.
        if not store.finalize():
            store.complete(
                outcome["status"],
                result=outcome["result"],
                error=outcome["error"],
                run_ids=outcome["run_ids"],
            )
        logger.info("eval %s finished: %s", evaluation_id, outcome["status"])
        return str(outcome["status"])

    except asyncio.CancelledError:
        # Record what we can without awaiting anything -- a suspension point
        # here re-raises immediately.
        _finalize_quietly(store, "cancelled", None)
        raise

    except interfaces.InvalidPayload as exc:
        logger.error("eval %s: unusable request body: %s", evaluation_id, exc)
        _finalize_quietly(store, "error", {"code": "invalid_request", "message": str(exc)})
        return "error"

    except interfaces.EvalEngineUnavailable as exc:
        logger.error("eval %s: evaluation engine unavailable: %s", evaluation_id, exc)
        _finalize_quietly(store, "error", {"code": "eval_engine_unavailable", "message": str(exc)})
        return "error"

    except Exception as exc:  # noqa: BLE001 - nothing else will report this
        logger.exception("eval %s failed", evaluation_id)
        _finalize_quietly(
            store,
            "error",
            {"code": "internal_error", "message": str(exc) or exc.__class__.__name__},
        )
        return "error"


def _stop_condition(store: DynamoEvalStore, deadline: Deadline | None) -> Callable[[], bool]:
    """What the engine polls between runs: a user cancel, or the deadline."""
    if deadline is None:
        return store.cancel_requested
    return lambda: store.cancel_requested() or deadline.expired()


def _finalize_quietly(store: DynamoEvalStore, status: str, error: dict[str, Any] | None) -> None:
    """Best-effort terminal write from an error path.

    If even this fails there is nothing useful left to do -- the evaluation will
    read as ``running`` until its TTL expires -- so it is logged and dropped
    rather than allowed to mask the original failure.
    """
    try:
        if not store.finalize():
            store.complete(status, error=error)
    except Exception:  # noqa: BLE001
        logger.exception("failed to write terminal state for eval %s", store.evaluation_id)


# --------------------------------------------------------------------------- #
# The entry point
# --------------------------------------------------------------------------- #


def handler(
    event: Any,
    context: Any = None,
    *,
    store_factory: Callable[[str], DynamoEvalStore] = build_store,
) -> dict[str, Any]:
    """Lambda handler: run one evaluation from an async invocation.

    Synchronous on purpose. The server already invoked with
    ``InvocationType="Event"``, so returning early would buy nothing and
    freezing the environment mid-evaluation would lose the job.

    The returned body is for CloudWatch and for a synchronous invocation in
    testing; the server never reads it. Failures of the *evaluation* are not
    reported here at all -- they land in DynamoDB, where the reader is looking.
    """
    try:
        evaluation_id, request = interfaces.validate_payload(event)
    except interfaces.InvalidPayload as exc:
        return _rejected("invalid_payload", str(exc))

    try:
        store = store_factory(evaluation_id)
        store.begin(request)
        claimed = store.mark_running()
    except Exception as exc:  # noqa: BLE001
        logger.exception("eval %s: could not write initial state", evaluation_id)
        return _rejected("store_unavailable", str(exc) or exc.__class__.__name__)

    if not claimed:
        # Lambda's asynchronous delivery is at-least-once, so a second copy of
        # this event can arrive even with MaximumRetryAttempts: 0. Running it
        # would buy every model run twice and overwrite the first delivery's
        # EVENT# items, since each store numbers its events from zero. The
        # conditional pending -> running update is the claim; losing it means
        # someone else owns this evaluation.
        return _duplicate(evaluation_id)

    status = asyncio.run(execute(evaluation_id, request, store, deadline_from(context)))
    return {"status": status, "evaluation_id": evaluation_id, "execution": "cloud"}
