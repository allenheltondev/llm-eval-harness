"""The cloud evaluation lane: submit to the worker Lambda, read back from DynamoDB.

``docs/cloud-evals.md`` is the contract; this module is the server's whole half
of it. Nothing here executes an evaluation -- that happens in a worker Lambda
which imports
:func:`evalharness.evals.engine.execute_evaluation_with_seam` and backs the seam
with DynamoDB writes. The server:

submit
    Generates the evaluation id, invokes the function asynchronously with
    ``{"evaluation_id", "request"}``, and answers ``202`` with an
    :class:`~evalharness.schemas.runs.EvaluationDetail` synthesized from the
    request. **Nothing is written locally** -- a cloud evaluation has no SQLite
    row at all, which is what makes lane detection on reads a simple "SQLite
    first, then DynamoDB" fallback.
read
    ``META`` items for detail/list, ``EVENT#`` items for the NDJSON stream,
    ``RUN#`` items for run detail/list.
cancel
    Puts the ``CANCEL`` flag item and answers ``204``; the worker notices
    between runs.

All DynamoDB access is delegated to :mod:`evalharness.evals.ddb_reader`, and the
Lambda call sits behind :class:`Invoker`, so the whole lane is exercised in
tests with an in-memory table and a recording invoker.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends
from starlette.concurrency import run_in_threadpool

from evalharness.config import Settings, get_settings
from evalharness.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    UpstreamError,
)
from evalharness.evals import ddb_reader, jobs
from evalharness.evals.ddb_reader import EvalTable
from evalharness.evals.schemas import EvaluationRequest
from evalharness.schemas.runs import EvaluationDetail, Page, RunDetail, RunSummary
from evalharness.store import ddb_items

logger = logging.getLogger(__name__)

#: How long the events stream waits between polls for new ``EVENT#`` items.
#: Module-level so tests can shorten it.
POLL_INTERVAL_SECONDS = 1.5
#: Extra polls granted after ``META`` goes terminal, so the worker's own
#: ``eval_complete`` (written *after* the terminal status) is never truncated.
TERMINAL_GRACE_POLLS = 2



class CloudLaneUnavailableError(BadRequestError):
    """The cloud lane was asked for but the server has no worker/table configured."""

    code = "cloud_lane_unavailable"


def is_configured(settings: Settings) -> bool:
    """The lane needs *both* a worker function and a DynamoDB table."""
    return bool(settings.eval_function_name and settings.eval_table)


def _require_configured(settings: Settings) -> None:
    if not is_configured(settings):
        raise CloudLaneUnavailableError(
            "The cloud evaluation lane is not configured on this server",
            detail={
                "eval_function_name": settings.eval_function_name is not None,
                "eval_table": settings.eval_table is not None,
            },
        )


def require_table(table: EvalTable | None) -> EvalTable:
    """Unwrap the table dependency, 400ing when the lane is unconfigured."""
    if table is None:
        raise CloudLaneUnavailableError(
            "The cloud evaluation lane is not configured on this server"
        )
    return table


# --------------------------------------------------------------------------- #
# Invoking the worker Lambda
# --------------------------------------------------------------------------- #


def worker_payload(evaluation_id: str, request: EvaluationRequest) -> dict[str, Any]:
    """The JSON body handed to the worker: id plus the request minus ``execution``."""
    return {
        "evaluation_id": evaluation_id,
        "request": request.model_dump(mode="json", exclude={"execution"}),
    }


#: What ``lambda:Invoke`` with ``InvocationType="Event"`` accepts: 1 MB, raised
#: from 256 KB in October 2025 [aws]. Taken as 10^6 so it can never admit
#: something AWS counts as over, whichever way it counts a megabyte.
ASYNC_PAYLOAD_LIMIT_BYTES = 1_000_000

#: DynamoDB's cap on a single item: 400 KB [aws].
DYNAMODB_ITEM_LIMIT_BYTES = 400 * 1024

#: The most the cloud lane accepts -- and the one that actually binds.
#:
#: The invoke limit above is the obvious constraint and the looser one. The
#: request is also stored, whole, as ``config`` on the evaluation's ``META``
#: item, and that same item gets the ``result`` when the evaluation finishes:
#: two large attributes sharing one 400 KB item. So the request may use at most
#: half of it, leaving the rest for a result that is itself bounded (a suite
#: result is held to ``engine.MAX_RESULT_BYTES`` serialized). The first
#: version of this check enforced the 1 MB invoke limit, which let a 300 KB
#: request through to fail at DynamoDB with a raw 500.
#:
#: ``EvaluationRequest`` bounds none of the prompts, the rubric, ``run_ids`` or a
#: suite's cases, and the Function URL accepts bodies up to 6 MB, so without
#: this a request could be valid at the API and still impossible to store. The
#: local lane has neither constraint.
CLOUD_REQUEST_LIMIT_BYTES = 200_000


def encode_payload(payload: dict[str, Any]) -> bytes:
    """The exact bytes sent as the invoke payload -- and so the bytes measured."""
    return json.dumps(payload).encode("utf-8")


def _too_large(size: int) -> PayloadTooLargeError:
    return PayloadTooLargeError(
        f"This evaluation is {size:,} bytes once serialized, and the cloud lane can "
        f"take at most {CLOUD_REQUEST_LIMIT_BYTES:,}. Shorten the prompts, the rubric "
        "or the suite, or run it on the local lane, which has no such limit.",
        detail={"bytes": size, "limit_bytes": CLOUD_REQUEST_LIMIT_BYTES},
        code="evaluation_too_large",
    )


class Invoker(Protocol):
    """Starts one evaluation on the worker. Synchronous (called off the loop)."""

    def invoke(self, evaluation_id: str, payload: dict[str, Any]) -> Any: ...


class LambdaInvoker:
    """The real invoker: an **asynchronous** ``lambda:Invoke``.

    ``InvocationType="Event"`` is the whole design. AWS queues the event and
    returns ``202`` as soon as it is durably accepted, so this call finishes in
    milliseconds while the evaluation runs for minutes inside the function --
    and the server never holds anything open for it. Progress comes back
    through DynamoDB, per ``docs/cloud-evals.md``.

    AWS retries a failed asynchronous invocation, and the function's
    ``EventInvokeConfig`` in ``infra/template.yaml`` keeps that on. It is safe
    because the worker's ``pending`` -> ``running`` update is an ownership
    claim: a redelivery of an evaluation that was already claimed runs nothing.
    What a retry buys is recovery from a failure *before* the claim, where the
    worker raises precisely so that Lambda redelivers the event.
    """

    def __init__(self, function_name: str, region_name: str, client: Any | None = None) -> None:
        self._function_name = function_name
        self._region_name = region_name
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = boto3.client("lambda", region_name=self._region_name)
        return self._client

    def invoke(self, evaluation_id: str, payload: dict[str, Any]) -> Any:
        body = encode_payload(payload)
        try:
            response = self.client.invoke(
                FunctionName=self._function_name,
                InvocationType="Event",
                Payload=body,
            )
        except ClientError as exc:
            # Wrapped, never raw: an unwrapped botocore error is a generic 500
            # to the caller, with nothing to say what actually went wrong.
            aws_error = exc.response.get("Error", {}).get("Code", "")
            if aws_error == "RequestTooLargeException":
                # `submit` measures first, so this means AWS counted differently.
                raise _too_large(len(body)) from exc
            raise UpstreamError(
                "The evaluation worker could not be invoked",
                detail={"evaluation_id": evaluation_id, "aws_error": aws_error},
                code="eval_worker_unavailable",
            ) from exc
        except BotoCoreError as exc:
            raise UpstreamError(
                "The evaluation worker could not be reached",
                detail={"evaluation_id": evaluation_id, "aws_error": exc.__class__.__name__},
                code="eval_worker_unavailable",
            ) from exc
        # An async invoke answers 202 with no body worth reading; anything else
        # means AWS did not accept the event, and the caller must not be told
        # the evaluation started.
        status = response.get("StatusCode")
        if status != 202:
            raise UpstreamError(
                f"The evaluation worker did not accept the job (HTTP {status})",
                detail={"evaluation_id": evaluation_id, "status_code": status},
                code="eval_worker_unavailable",
            )
        return response


_invokers: dict[tuple[str, str], LambdaInvoker] = {}


def get_invoker(settings: Settings = Depends(get_settings)) -> Invoker | None:
    """FastAPI dependency: the worker invoker, or ``None`` when unconfigured."""
    function_name = settings.eval_function_name
    if not function_name:
        return None
    key = (function_name, settings.aws_region)
    if key not in _invokers:
        _invokers[key] = LambdaInvoker(function_name, settings.aws_region)
    return _invokers[key]


def get_eval_writer_factory(
    settings: Settings = Depends(get_settings),
) -> EvalWriterFactory | None:
    """FastAPI dependency: how ``submit`` writes the ``pending`` row, or ``None``.

    Separate from :func:`get_eval_table`, which is the *reader*. Kept as a
    dependency rather than built inside ``submit`` so a test can substitute it
    exactly the way it substitutes :func:`get_invoker`.
    """
    if not settings.eval_table:
        return None
    return build_eval_writer_factory(settings)


def get_eval_table(settings: Settings = Depends(get_settings)) -> EvalTable | None:
    """FastAPI dependency: the DynamoDB reader, or ``None`` when unconfigured."""
    return ddb_reader.build_eval_table(settings)


# --------------------------------------------------------------------------- #
# Submit
# --------------------------------------------------------------------------- #


async def submit(
    request: EvaluationRequest,
    *,
    settings: Settings,
    invoker: Invoker | None,
    store_factory: EvalWriterFactory | None,
) -> EvaluationDetail:
    """Hand an evaluation to the worker and answer with its ``pending`` detail.

    The ``pending`` row is written **before** the invoke, and that ordering is
    the contract rather than an implementation detail. ``InvocationType="Event"``
    means the ``202`` from AWS says only that the event was queued: the worker
    may not start -- and so may not write ``META`` -- for seconds, longer on a
    cold start. The SPA follows the ``202`` straight into
    ``GET /evaluations/{id}/events``, which preflights the row and 404s while it
    is absent, so a successfully queued evaluation would surface as an error.

    The row goes into DynamoDB, *not* through this server's history repository.
    That distinction matters when the two differ -- a laptop running the cloud
    lane against a deployed stack keeps its history in SQLite -- because a local
    row would shadow the worker's: ``GET /evaluations/{id}/events`` reads the
    repository first and only falls back to DynamoDB on ``NotFoundError``, so a
    stray SQLite row would replay an empty ``pending`` record forever instead of
    streaming the real thing.

    :class:`DynamoEvalStore` is the writer for the same reason: it is the one
    place that knows this item's shape, and its ``begin`` is a conditional put,
    so whichever of the two sides goes first wins and the other leaves the row
    alone. Either order produces the same item.

    ``kind="grade"`` run ids are *not* validated here the way the local lane
    validates them -- the runs they name may live in DynamoDB rather than this
    server's SQLite, so only the worker can resolve them.
    """
    _require_configured(settings)
    if invoker is None:  # pragma: no cover - _require_configured covers this
        raise CloudLaneUnavailableError("The cloud evaluation lane is not configured")

    evaluation_id = uuid4().hex
    payload = worker_payload(evaluation_id, request)
    # Measured before anything is written: a request that cannot be invoked, or
    # cannot be stored, must not leave a `pending` row behind to be abandoned.
    size = len(encode_payload(payload))
    if size > CLOUD_REQUEST_LIMIT_BYTES:
        raise _too_large(size)
    factory = store_factory or build_eval_writer_factory(settings)
    store = factory(evaluation_id)
    store.begin(payload["request"], kind=request.kind)

    try:
        await run_in_threadpool(invoker.invoke, evaluation_id, payload)
    except Exception:
        # The row exists and nothing is going to run it, so settle it here
        # rather than leave a `pending` that never moves. Best effort: the
        # caller gets the invoke's own error either way.
        _abandon(store, evaluation_id)
        raise
    logger.info("submitted cloud evaluation %s", evaluation_id)

    return EvaluationDetail(
        id=evaluation_id,
        ts=datetime.now(UTC),
        kind=request.kind,
        status="pending",
        config=request.stored_config(),
        run_ids=request.run_ids if request.kind == "grade" else [],
        result=None,
        progress=None,
        error=None,
        execution="cloud",
    )


class EvalWriter(Protocol):
    """The slice of :class:`DynamoEvalStore` this module writes through."""

    def begin(self, request: dict[str, Any], *, kind: str | None = ...) -> None: ...

    def complete(
        self,
        status: str,
        *,
        result: dict[str, Any] | None = ...,
        error: dict[str, Any] | None = ...,
        run_ids: list[str] | None = ...,
    ) -> None: ...


EvalWriterFactory = Callable[[str], EvalWriter]


def build_eval_writer_factory(settings: Settings) -> EvalWriterFactory:
    """Build DynamoDB eval stores for this server's table.

    Imported lazily: the worker's store pulls in the DynamoDB client, and a
    server with the cloud lane switched off should not pay for that at import.
    """
    from evalharness.worker.ddb import DynamoEvalStore

    def build(evaluation_id: str) -> EvalWriter:
        return DynamoEvalStore(
            table_name=settings.eval_table or "",
            evaluation_id=evaluation_id,
            region_name=settings.aws_region,
            retention_days=settings.history_retention_days,
        )

    return build


def _abandon(store: EvalWriter, evaluation_id: str) -> None:
    """Settle a row whose worker was never reached."""
    try:
        store.complete(
            "error",
            error={
                "code": "eval_worker_unavailable",
                "message": "The evaluation was never handed to the worker.",
            },
            run_ids=[],
        )
    except Exception:  # noqa: BLE001 - the invoke's own error is the one to report
        logger.exception("could not settle unstarted evaluation %s", evaluation_id)


# --------------------------------------------------------------------------- #
# Item -> response mapping
# --------------------------------------------------------------------------- #


def evaluation_detail(item: dict[str, Any]) -> EvaluationDetail:
    """An ``EVAL#/META`` item as an :class:`EvaluationDetail`.

    Item -> record is :mod:`evalharness.store.ddb_items` (the same mapping the
    worker writes through and the DynamoDB history backend reads through);
    record -> response is the ordinary ``model_validate`` the local lane uses.
    The only cloud-specific thing left here is the lane label.
    """
    record = ddb_items.evaluation_record(item)
    return EvaluationDetail.model_validate({**asdict(record), "execution": "cloud"})


def run_detail(item: dict[str, Any]) -> RunDetail:
    """A ``RUN#/META`` item as a :class:`RunDetail`."""
    return RunDetail.model_validate(ddb_items.run_record(item))


def run_summary(item: dict[str, Any]) -> RunSummary:
    """A ``RUN#/META`` item as a listing row."""
    return RunSummary.model_validate(ddb_items.run_record(item))


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def get_evaluation(table: EvalTable, evaluation_id: str) -> EvaluationDetail:
    """The cloud evaluation, or a 404 identical to the local lane's."""
    item = table.get_evaluation(evaluation_id)
    if item is None:
        raise NotFoundError(f"Evaluation {evaluation_id!r} not found")
    return evaluation_detail(item)


def list_evaluations(
    table: EvalTable,
    *,
    kind: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = 25,
) -> Page[EvaluationDetail]:
    """Newest-first page of cloud evaluations from the GSI1 ``EVAL`` partition.

    ``kind``/``status`` are applied to the page after it is read (the index is
    keyed on time alone), so they narrow a page rather than fill one -- the same
    semantics a DynamoDB ``FilterExpression`` would give.
    """
    items, next_cursor = table.list_evaluations(limit=limit, cursor=cursor)
    details = [evaluation_detail(item) for item in items]
    if kind is not None:
        details = [detail for detail in details if detail.kind == kind]
    if status is not None:
        details = [detail for detail in details if detail.status == status]
    return Page[EvaluationDetail](items=details, next_cursor=next_cursor)


def get_run(table: EvalTable, run_id: str) -> RunDetail:
    """The cloud run record, or a 404 identical to the local lane's."""
    item = table.get_run(run_id)
    if item is None:
        raise NotFoundError(f"Run {run_id!r} not found")
    return run_detail(item)


def list_runs(table: EvalTable, *, cursor: str | None = None, limit: int = 25) -> Page[RunSummary]:
    """Newest-first page of cloud runs from the GSI1 ``RUN`` partition."""
    items, next_cursor = table.list_runs(limit=limit, cursor=cursor)
    return Page[RunSummary](items=[run_summary(item) for item in items], next_cursor=next_cursor)


def cancel_evaluation(table: EvalTable, evaluation_id: str) -> None:
    """Request cancellation of a running cloud evaluation.

    Best-effort and asynchronous: the ``CANCEL`` item goes in and the caller
    answers ``204`` without waiting for the worker to notice. A *finished*
    evaluation is a ``409``, matching the local lane -- the contract only
    prescribes the running case.
    """
    item = table.get_evaluation(evaluation_id)
    if item is None:
        raise NotFoundError(f"Evaluation {evaluation_id!r} not found")
    status = str(item.get("status") or "")
    if ddb_reader.is_terminal(status):
        raise ConflictError(
            f"Evaluation {evaluation_id!r} already finished", detail={"status": status}
        )
    table.request_cancel(evaluation_id)


# --------------------------------------------------------------------------- #
# The event stream
# --------------------------------------------------------------------------- #


async def stream_events(table: EvalTable, evaluation_id: str) -> AsyncIterator[str]:
    """Replay this evaluation's ``EVENT#`` items as NDJSON, then follow them.

    The loop reads ``META`` *before* the events it then streams, so an event
    written before the terminal status can never be missed. ``eval_complete``
    is written after the terminal status, though, so a terminal ``META`` alone
    does not end the stream: it grants :data:`TERMINAL_GRACE_POLLS` further
    polls, and the stream ends as soon as ``eval_complete`` is seen (or the
    grace runs out, for an evaluation whose worker died before emitting it).

    Nothing is synthesized -- the worker's own ``eval_complete`` is the last
    line, exactly as in the local lane.
    """
    last_seq = -1
    grace = TERMINAL_GRACE_POLLS
    while True:
        meta = await run_in_threadpool(table.get_evaluation, evaluation_id)
        terminal = meta is None or ddb_reader.is_terminal(str(meta.get("status") or ""))

        items = await run_in_threadpool(table.events_after, evaluation_id, last_seq)
        for item in items:
            last_seq = int(item["seq"])
            entry = json.loads(item["event"])
            yield jobs.to_json_line(entry)
            if entry.get("type") == "eval_complete":
                return

        if items:
            continue  # drain greedily; only an empty read costs a poll interval
        if terminal:
            grace -= 1
            if grace <= 0:
                return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
