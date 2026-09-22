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

So the deadline is cooperative. :class:`Deadline` watches the invocation's own
remaining time and, with :data:`DEADLINE_MARGIN_SECONDS` to spare, reports
itself through the engine's ``cancelled`` seam -- polled between runs and
before grading. The engine stops at a clean boundary with every finished run
already persisted, and the worker then settles the evaluation as ``error``
with ``deadline_exceeded`` rather than the ``cancelled`` the engine would
otherwise record, because nobody cancelled it.
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
    "Deadline",
    "build_store",
    "execute",
    "handler",
]

#: Seconds of the invocation reserved for settling up. Has to cover the
#: terminal DynamoDB writes (an ``eval_complete`` event plus the META update)
#: with room for a retry, and is checked only *between* runs -- so it also
#: absorbs the tail of whatever run is in flight when the deadline passes.
DEADLINE_MARGIN_SECONDS = 60.0

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
        outcome = await interfaces.run_evaluation(request, store.emit, store, cancelled)

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
        store.mark_running()
    except Exception as exc:  # noqa: BLE001
        logger.exception("eval %s: could not write initial state", evaluation_id)
        return _rejected("store_unavailable", str(exc) or exc.__class__.__name__)

    status = asyncio.run(execute(evaluation_id, request, store, deadline_from(context)))
    return {"status": status, "evaluation_id": evaluation_id, "execution": "cloud"}
