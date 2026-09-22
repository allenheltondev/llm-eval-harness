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
So the engine additionally runs as its own task with a hard bound at
:data:`HARD_DEADLINE_RESERVE_SECONDS`, and is cancelled rather than asked when
it overruns. The smaller reserve is what makes the cooperative stop the one
that normally fires: it keeps the run in progress, where the hard stop loses
it.

Either way the engine settles ``cancelled`` itself and publishes
``eval_complete``, which makes the store terminal before the worker gets
control back -- so a deadline cannot be corrected afterwards. The worker
records the reason on the store *first* (:meth:`DynamoEvalStore.stop_with`),
and the store writes the engine's ``cancelled`` as ``deadline_exceeded``.

Duplicate deliveries
--------------------
An evaluation's event can arrive more than once: asynchronous invocation is
at-least-once, and Lambda also redelivers after a failed invocation
(``MaximumRetryAttempts``). So the conditional ``pending`` -> ``running``
update is an ownership claim: exactly one delivery can win it, and a delivery
that loses executes nothing and writes nothing. That claim is also what makes
the retries safe to leave on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any

from evalharness.store import db
from evalharness.worker import interfaces
from evalharness.worker.ddb import DynamoEvalStore

logger = logging.getLogger(__name__)

__all__ = [
    "BOUNDARY_STOP",
    "DEADLINE_MARGIN_SECONDS",
    "IN_FLIGHT_STOP",
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
#: which is the precise outcome the deadline exists to prevent. So the engine
#: task is additionally cancelled once only this much of the invocation is
#: left, which is what the terminal write then has to fit in.
#:
#: Smaller than the cooperative margin on purpose: the cooperative stop must
#: get its chance first, because stopping at a run boundary keeps more work
#: than being cancelled mid-flight.
HARD_DEADLINE_RESERVE_SECONDS = 25.0

#: What the evaluation settles as when the invocation runs out of time.
DEADLINE_ERROR_CODE = "deadline_exceeded"

#: The error recorded when the cooperative stop is taken: the engine noticed the
#: deadline between runs and stopped cleanly.
BOUNDARY_STOP = {
    "code": DEADLINE_ERROR_CODE,
    "message": (
        "The evaluation did not finish inside the worker's 15-minute limit and "
        "was stopped at a run boundary. Completed runs are preserved; re-run "
        "with a smaller n."
    ),
}

#: The error recorded when the hard bound fires: a run or grading was still in
#: flight and had to be cancelled.
IN_FLIGHT_STOP = {
    "code": DEADLINE_ERROR_CODE,
    "message": (
        "The evaluation did not finish inside the worker's 15-minute limit and "
        "was stopped while a run was still in flight. Completed runs are "
        "preserved; re-run with a smaller n."
    ),
}


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


#: Where each invocation's scratch database lives. A directory rather than a
#: file because SQLite runs in WAL mode here, which keeps ``-wal`` and ``-shm``
#: files beside the database; removing the directory removes all of them.
SCRATCH_ROOT = os.path.join(tempfile.gettempdir(), "evalharness-worker")


@contextlib.contextmanager
def scratch_database(root: str = SCRATCH_ROOT) -> Iterator[None]:
    """Give one invocation a SQLite database of its own, and leave nothing behind.

    The run engine writes every run to SQLite and ``DynamoEvalStore.save_run``
    mirrors it to DynamoDB; the local row is scratch from then on. But Lambda
    reuses both the process and ``/tmp`` across warm invocations, and
    ``store.db`` keeps a module-level engine -- so a single shared file would
    accumulate the outputs and tool transcripts of every unrelated evaluation
    this environment ever ran, until ephemeral storage became the failure mode.

    So the directory is wiped at the start, which clears whatever a killed
    invocation left, and again at the end, so an idle frozen environment holds
    no evaluation's data at all. One environment runs one invocation at a time,
    so nothing else can be using it.
    """
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    try:
        with db.scoped_db(os.path.join(root, "scratch.db")):
            yield
    finally:
        shutil.rmtree(root, ignore_errors=True)


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
        task = asyncio.ensure_future(
            interfaces.run_evaluation(request, store.emit, store, cancelled)
        )
        try:
            done, _ = await asyncio.wait(
                {task}, timeout=None if deadline is None else deadline.budget()
            )
        except asyncio.CancelledError:
            # This coroutine is being cancelled while the engine runs, and
            # `asyncio.wait` -- unlike `await task` -- does not pass that on:
            # the engine would keep running behind us. Cancel it and let it
            # settle through the store on its own cancel path *before* this
            # cancellation continues outward, so the row and the stream's last
            # line are the engine's rather than a guess written ahead of it.
            await _cancel_and_settle(task)
            # Unreachable today: we are being cancelled, so _cancel_and_settle
            # re-raises. Kept so this path can never fall through into the
            # result handling below if that ever changes.
            raise  # pragma: no cover - _cancel_and_settle re-raises here

        if task not in done:
            # The cooperative stop was asked for and not taken: the engine is
            # inside a single run, or inside grading, where nothing polls
            # `cancelled`. Arm the reason *before* cancelling. The engine's
            # cancel handler settles `cancelled` and publishes `eval_complete`,
            # which flushes the row -- so this is the last moment the reason
            # can still be recorded. (`asyncio.wait_for` cancels and only then
            # raises, which is too late.)
            #
            # Unless the user got there first. A cancel that lands while a call
            # is stuck is never seen by the engine -- it polls between runs --
            # so the hard stop is merely what *interrupts* it. The user asked
            # for this; it settles as `cancelled`, as the cooperative path
            # already does by checking the flag before the deadline.
            if store.cancel_requested():
                logger.info("eval %s: cancelled during a stalled call", evaluation_id)
                await _cancel_and_settle(task)
                _settle(store, "cancelled", None)
                return "cancelled"
            logger.warning("eval %s hit the hard deadline mid-flight", evaluation_id)
            store.stop_with(IN_FLIGHT_STOP)
            await _cancel_and_settle(task)
            _settle(store, "error", IN_FLIGHT_STOP)
            return "error"
        outcome = task.result()

        if outcome["status"] == "cancelled" and deadline is not None and deadline.tripped:
            # The stop condition armed the reason when the deadline tripped, so
            # the engine's `cancelled` has already been recorded as the
            # deadline -- in the row and in the stream's last line. Nobody
            # cancelled this.
            _settle(store, "error", BOUNDARY_STOP)
            logger.warning("eval %s stopped at the deadline", evaluation_id)
            return "error"

        # The engine settles the row itself and then publishes `eval_complete`,
        # which flushes it -- so the store is normally terminal already, and
        # `finalize()` returning False means "nothing left to do", NOT "the
        # engine never settled". Reading it the second way wrote again, hit
        # "already terminal", and reported every successful evaluation as
        # `error`. Settle from the outcome only when the store is not terminal
        # and there was nothing buffered either: the engine really did return
        # without settling.
        if not store.is_terminal and not store.finalize():
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
    """What the engine polls between runs: a user cancel, or the deadline.

    The user's cancel is checked first and wins, and it arms nothing, so it
    still settles as ``cancelled``. A deadline arms its reason on the store at
    the moment it trips -- before the engine settles -- because afterwards is
    too late to correct (see :meth:`DynamoEvalStore.stop_with`).
    """
    if deadline is None:
        return store.cancel_requested

    def stop() -> bool:
        if store.cancel_requested():
            return True
        if deadline.expired():
            store.stop_with(BOUNDARY_STOP)
            return True
        return False

    return stop


async def _cancel_and_settle(task: asyncio.Task[Any]) -> None:
    """Cancel the engine and wait for it to finish settling through the store.

    The ``CancelledError`` that comes back is normally the engine's own, from
    the cancel sent here, and is absorbed. If *this* coroutine is being
    cancelled as well, that is not ours to absorb, so it propagates.
    """
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise


def _settle(store: DynamoEvalStore, status: str, error: dict[str, Any] | None) -> None:
    """Make sure a stopped evaluation is durably written as ``status``.

    With the real engine this is normally already done: it settled through the
    store -- which, for a deadline, translated its ``cancelled`` using the armed
    reason -- and the ``eval_complete`` it published flushed the row. This
    covers the rest -- a buffered state that was never flushed, or an engine
    that never settled at all -- without ever overwriting a row that is already
    terminal.
    """
    if store.is_terminal or store.finalize():
        return
    store.complete(status, error=error, run_ids=store.saved_run_ids)


def _finalize_quietly(store: DynamoEvalStore, status: str, error: dict[str, Any] | None) -> None:
    """Best-effort terminal write from an error path.

    If even this fails there is nothing useful left to do -- the evaluation will
    read as ``running`` until its TTL expires -- so it is logged and dropped
    rather than allowed to mask the original failure.
    """
    if store.is_terminal:
        # Already settled -- by the engine's own cancel path, typically. Writing
        # again would only raise, and the row is right as it stands.
        return
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
    scratch: Callable[[], AbstractContextManager[Any]] = scratch_database,
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

    # Everything that can fail runs BEFORE the claim, and the scratch database
    # is part of that. The claim is the point of no return: past it, a failure
    # strands the row at `running`, because every redelivery finds it claimed
    # and stands down. Before it, a failure leaves `pending`, and raising gets
    # the event redelivered to a retry that claims and runs it.
    with scratch():
        try:
            store = store_factory(evaluation_id)
            store.begin(request)
            claimed = store.mark_running()
        except Exception:
            # Fail the invocation rather than return. Nobody reads an async
            # invocation's return value, and returning tells Lambda it
            # succeeded: no retry, and the `pending` row the server wrote before
            # invoking sits there forever. Raising gets the event redelivered
            # (`MaximumRetryAttempts` in infra/template.yaml), and redelivery is
            # safe because nothing has been claimed yet.
            logger.exception("eval %s: could not claim; failing for a retry", evaluation_id)
            raise

        if not claimed:
            # A second copy of this event can arrive -- asynchronous delivery is
            # at-least-once, and Lambda redelivers after a failure. Running it
            # would buy every model run twice and overwrite the first delivery's
            # EVENT# items, since each store numbers its events from zero. The
            # conditional pending -> running update is the claim; losing it
            # means someone else owns this evaluation.
            return _duplicate(evaluation_id)

        status = asyncio.run(execute(evaluation_id, request, store, deadline_from(context)))
    return {"status": status, "evaluation_id": evaluation_id, "execution": "cloud"}
