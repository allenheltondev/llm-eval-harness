"""The Lambda entry point: envelope validation, the job body, and the deadline.

The cloud lane's shape rests on the *invocation* being asynchronous rather than
the handler returning early: the server invokes with ``InvocationType="Event"``,
so the whole evaluation runs inside one invocation and FastAPI reads progress
from DynamoDB instead of holding anything open.

Which makes the 15-minute ceiling the thing worth pinning down. Being killed at
it would leave the evaluation reading ``running`` until its TTL, so the worker
stops itself first, at a run boundary, and records *why* --
:func:`test_execute_settles_a_deadline_as_an_error_not_a_cancellation` is that
guarantee.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from evalharness.worker import interfaces, lambda_app
from evalharness.worker.ddb import DynamoEvalStore
from tests.fake_dynamodb import FakeDynamoDBClient

TABLE = "llm-eval-harness-ScenariosTable-TEST"
EVAL_ID = "0123456789abcdef0123456789abcdef"
REQUEST = {"kind": "determinism", "n": 2, "run_config": {"model_id": "m", "user_prompt": "p"}}
PAYLOAD = {"evaluation_id": EVAL_ID, "request": REQUEST}


@pytest.fixture
def client() -> FakeDynamoDBClient:
    return FakeDynamoDBClient()


@pytest.fixture
def store_factory(client: FakeDynamoDBClient):
    def factory(evaluation_id: str) -> DynamoEvalStore:
        return DynamoEvalStore(TABLE, evaluation_id, client=client)

    return factory


def meta(client: FakeDynamoDBClient, evaluation_id: str = EVAL_ID) -> dict:
    item = client.item(f"EVAL#{evaluation_id}", "META")
    assert item is not None, "META item was never written"
    return item


def collected(client: FakeDynamoDBClient, evaluation_id: str = EVAL_ID) -> list[dict]:
    return [
        json.loads(item["event"]["S"])
        for item in client.items_with_prefix(f"EVAL#{evaluation_id}", "EVENT#")
    ]


# --------------------------------------------------------------------------- #
# Payload validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("not a dict", "non-object payload"),
        ({}, "missing everything"),
        ({"request": REQUEST}, "missing evaluation_id"),
        ({"evaluation_id": EVAL_ID}, "missing request"),
        ({"evaluation_id": "", "request": REQUEST}, "empty evaluation_id"),
        ({"evaluation_id": 42, "request": REQUEST}, "non-string evaluation_id"),
        ({"evaluation_id": "EVAL#injected", "request": REQUEST}, "key-injecting id"),
        ({"evaluation_id": EVAL_ID, "request": {}}, "empty request"),
        ({"evaluation_id": EVAL_ID, "request": "nope"}, "non-object request"),
    ],
)
def test_validate_payload_rejects_bad_envelopes(payload, reason):
    with pytest.raises(interfaces.InvalidPayload):
        interfaces.validate_payload(payload)


def test_validate_payload_returns_the_id_and_request():
    evaluation_id, request = interfaces.validate_payload(PAYLOAD)
    assert evaluation_id == EVAL_ID
    assert request == REQUEST


def test_validate_payload_strips_the_execution_switch():
    """``execution`` routes the request; it is not part of the eval schema."""
    _, request = interfaces.validate_payload(
        {"evaluation_id": EVAL_ID, "request": {**REQUEST, "execution": "cloud"}}
    )
    assert "execution" not in request
    assert request == REQUEST


def test_handler_rejects_a_bad_payload_without_touching_dynamodb(client, store_factory):
    response = lambda_app.handler({"nope": True}, store_factory=store_factory)

    assert response["status"] == "rejected"
    assert response["error"]["code"] == "invalid_payload"
    assert client.calls == []


def test_handler_reports_an_unwritable_store():
    def exploding(_evaluation_id: str):
        raise RuntimeError("table is gone")

    response = lambda_app.handler(PAYLOAD, store_factory=exploding)

    assert response["status"] == "rejected"
    assert response["error"]["code"] == "store_unavailable"


def test_handler_runs_the_whole_evaluation_inside_the_invocation(
    monkeypatch, client, store_factory
):
    """Synchronous on purpose: a Lambda is frozen the moment its handler returns.

    The previous host acknowledged fast and kept working in the background. Here
    the async *invoke* does that job, so anything the handler does not finish
    does not happen.
    """
    async def fake_seam(request, emit, store, cancelled=None, **_kwargs):
        emit({"type": "eval_complete", "status": "completed", "result": None})
        store.save_evaluation(status="completed", result=None, error=None)
        return {"status": "completed", "result": None, "error": None, "run_ids": []}

    monkeypatch.setattr(interfaces, "load_seam", lambda: fake_seam)

    response = lambda_app.handler(PAYLOAD, store_factory=store_factory)

    assert response == {"status": "completed", "evaluation_id": EVAL_ID, "execution": "cloud"}
    # Terminal before the handler returned -- not merely started.
    assert meta(client)["status"] == {"S": "completed"}


def test_handler_writes_pending_then_running_before_executing(
    monkeypatch, client, store_factory
):
    """The opening state is durable before any model call is made."""
    seen: list[str] = []

    async def fake_seam(request, emit, store, cancelled=None, **_kwargs):
        seen.append(meta(client)["status"]["S"])
        store.save_evaluation(status="completed", result=None, error=None)
        return {"status": "completed", "result": None, "error": None, "run_ids": []}

    monkeypatch.setattr(interfaces, "load_seam", lambda: fake_seam)
    lambda_app.handler(PAYLOAD, store_factory=store_factory)

    put_statuses = [
        kwargs["Item"]["status"]["S"]
        for name, kwargs in client.calls
        if name == "put_item" and kwargs["Item"]["sk"]["S"] == "META"
    ]
    assert put_statuses == ["pending"]
    assert seen == ["running"]


# --------------------------------------------------------------------------- #
# The deadline
# --------------------------------------------------------------------------- #


def test_deadline_trips_only_inside_the_margin():
    left = [lambda_app.DEADLINE_MARGIN_SECONDS + 1]
    deadline = lambda_app.Deadline(lambda: left[0])

    assert deadline.expired() is False
    left[0] = lambda_app.DEADLINE_MARGIN_SECONDS
    assert deadline.expired() is True


def test_deadline_is_latched():
    """The engine polls repeatedly; a verdict that flickered would be useless."""
    left = [0.0]
    deadline = lambda_app.Deadline(lambda: left[0])
    assert deadline.expired() is True

    left[0] = 10_000.0
    assert deadline.expired() is True
    assert deadline.tripped is True


def test_a_broken_clock_does_not_stop_an_evaluation():
    def exploding() -> float:
        raise RuntimeError("no context")

    assert lambda_app.Deadline(exploding).expired() is False


def test_deadline_from_reads_the_lambda_context():
    class Context:
        @staticmethod
        def get_remaining_time_in_millis() -> int:
            return 5_000

    deadline = lambda_app.deadline_from(Context())
    # 5s left is well inside the margin.
    assert deadline.expired() is True


def test_deadline_from_falls_back_when_there_is_no_context(monkeypatch):
    """Never silently absent: a direct invocation still gets a guard."""
    monkeypatch.setenv("EVAL_WORKER_TIMEOUT_SECONDS", "0")
    assert lambda_app.deadline_from(None).expired() is True

    monkeypatch.setenv("EVAL_WORKER_TIMEOUT_SECONDS", "900")
    assert lambda_app.deadline_from(None).expired() is False


async def test_execute_settles_a_deadline_as_an_error_not_a_cancellation(
    monkeypatch, client, store_factory
):
    """Nobody cancelled this, so it must not read as cancelled.

    The engine's cooperative-stop path settles `cancelled`; the worker
    overwrites that buffered state with `deadline_exceeded` before it is
    flushed, keeping the runs that did finish.
    """
    async def fake_seam(request, emit, store, cancelled=None, **_kwargs):
        assert cancelled() is True  # the deadline is already past
        store.save_evaluation(status="cancelled", result=None, error=None)
        return {"status": "cancelled", "result": None, "error": None, "run_ids": ["run-1"]}

    monkeypatch.setattr(interfaces, "load_seam", lambda: fake_seam)

    store = store_factory(EVAL_ID)
    store.begin(REQUEST)
    store.mark_running()
    # The moment the cooperative stop trips: exactly at the margin, so there is
    # still hard budget left for the engine to return through. An invocation
    # never sees this the other way round, because the margin is the larger of
    # the two reserves.
    deadline = lambda_app.Deadline(lambda: lambda_app.DEADLINE_MARGIN_SECONDS)

    status = await lambda_app.execute(EVAL_ID, REQUEST, store, deadline)

    assert status == "error"
    item = meta(client)
    assert item["status"] == {"S": "error"}
    assert json.loads(item["error"]["S"])["code"] == lambda_app.DEADLINE_ERROR_CODE
    # The finished runs survive -- that is the point of stopping cleanly.
    assert json.loads(item["run_ids"]["S"]) == ["run-1"]
    assert "run boundary" in json.loads(item["error"]["S"])["message"]


async def test_a_user_cancel_still_settles_as_cancelled(monkeypatch, client, store_factory):
    """The deadline must not swallow a real cancellation."""
    async def fake_seam(request, emit, store, cancelled=None, **_kwargs):
        # Nothing to stop for yet -- plenty of time left and no cancel.
        assert cancelled() is False
        store.request_cancel()
        assert cancelled() is True
        store.save_evaluation(status="cancelled", result=None, error=None)
        return {"status": "cancelled", "result": None, "error": None, "run_ids": []}

    monkeypatch.setattr(interfaces, "load_seam", lambda: fake_seam)

    store = store_factory(EVAL_ID)
    store.begin(REQUEST)
    store.mark_running()
    # Plenty of time left: the only reason to stop is the user's cancel. The
    # cancel arrives mid-run so this exercises the composite stop condition
    # rather than execute()'s short-circuit for an already-cancelled job.
    deadline = lambda_app.Deadline(lambda: 10_000.0)

    status = await lambda_app.execute(EVAL_ID, REQUEST, store, deadline)

    assert status == "cancelled"
    assert meta(client)["status"] == {"S": "cancelled"}


# --------------------------------------------------------------------------- #
# The job body
# --------------------------------------------------------------------------- #


async def test_execute_drives_the_engine_and_writes_the_terminal_state(
    monkeypatch, client, store_factory
):
    result = {"grade": "A", "score": 0.95, "run_ids": ["run-1"]}

    async def fake_seam(request, emit, store, cancelled=None, **_kwargs):
        # The seam receives the validated model, not the raw dict.
        assert request.kind == "determinism"
        assert cancelled() is False
        emit({"type": "eval_start", "evaluation_id": EVAL_ID, "kind": "determinism", "n": 2})
        store.put_run({"id": "run-1", "status": "completed"})
        emit({"type": "run_completed", "index": 0, "run_id": "run-1", "status": "completed"})
        # The engine settles the row, then publishes -- the store reorders the
        # two writes so the terminal status never lands first.
        store.save_evaluation(status="completed", result=result, error=None)
        emit({"type": "eval_complete", "status": "completed", "result": result})
        return {"status": "completed", "result": result, "error": None, "run_ids": ["run-1"]}

    monkeypatch.setattr(interfaces, "load_seam", lambda: fake_seam)

    store = store_factory(EVAL_ID)
    store.begin(REQUEST)
    store.mark_running()
    await lambda_app.execute(EVAL_ID, REQUEST, store)

    item = meta(client)
    assert item["status"] == {"S": "completed"}
    assert json.loads(item["result"]["S"]) == result
    assert json.loads(item["run_ids"]["S"]) == ["run-1"]

    events = collected(client)
    assert [event["type"] for event in events] == [
        "eval_start",
        "run_completed",
        "eval_complete",
    ]
    assert client.item("RUN#run-1", "META") is not None


async def test_execute_settles_from_the_outcome_when_the_engine_never_called_save_evaluation(
    monkeypatch, client, store_factory
):
    """The other half of the ``finalize()`` contract: an engine that returns a
    result *without* ever buffering a terminal ``save_evaluation`` (so
    ``finalize()`` is a no-op) is settled by the worker itself, from the
    returned outcome dict -- this is the real "engine returned without
    settling at all" case the module's docstring describes."""
    result = {"grade": "B", "score": 0.7, "run_ids": ["run-9"]}

    async def fake_seam(request, emit, store, cancelled=None, **_kwargs):
        emit({"type": "eval_start", "evaluation_id": EVAL_ID, "kind": "determinism", "n": 1})
        # Deliberately never calls store.save_evaluation/finalize.
        return {"status": "completed", "result": result, "error": None, "run_ids": ["run-9"]}

    monkeypatch.setattr(interfaces, "load_seam", lambda: fake_seam)

    store = store_factory(EVAL_ID)
    store.begin(REQUEST)
    store.mark_running()
    await lambda_app.execute(EVAL_ID, REQUEST, store)

    item = meta(client)
    assert item["status"] == {"S": "completed"}
    assert json.loads(item["result"]["S"]) == result
    assert json.loads(item["run_ids"]["S"]) == ["run-9"]
    # complete() synthesizes the eval_complete event since the engine's own
    # emit() calls never included one.
    assert collected(client)[-1]["type"] == "eval_complete"


async def test_execute_short_circuits_when_cancelled_before_it_starts(
    monkeypatch, client, store_factory
):
    async def never_called(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("engine ran despite a pending cancel")

    monkeypatch.setattr(interfaces, "load_seam", lambda: never_called)

    store = store_factory(EVAL_ID)
    store.begin(REQUEST)
    store.request_cancel()
    await lambda_app.execute(EVAL_ID, REQUEST, store)

    assert meta(client)["status"] == {"S": "cancelled"}
    assert [event["type"] for event in collected(client)] == ["eval_complete"]


async def test_execute_records_an_engine_crash_as_a_terminal_error(
    monkeypatch, client, store_factory
):
    """A detached task that raises would otherwise leave the eval at ``running``."""

    async def exploding(*_args, **_kwargs):
        raise ValueError("judge exploded")

    monkeypatch.setattr(interfaces, "load_seam", lambda: exploding)

    store = store_factory(EVAL_ID)
    store.begin(REQUEST)
    await lambda_app.execute(EVAL_ID, REQUEST, store)  # must not raise

    item = meta(client)
    assert item["status"] == {"S": "error"}
    assert json.loads(item["error"]["S"]) == {
        "code": "internal_error",
        "message": "judge exploded",
    }
    # The reader's stream still terminates.
    assert collected(client)[-1]["type"] == "eval_complete"


async def test_execute_records_cancellation_and_reraises(monkeypatch, client, store_factory):
    """A runtime shutdown mid-evaluation still leaves a terminal, readable row --
    and the CancelledError itself must propagate, not be swallowed."""

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(interfaces, "load_seam", lambda: cancelled)

    store = store_factory(EVAL_ID)
    store.begin(REQUEST)

    with pytest.raises(asyncio.CancelledError):
        await lambda_app.execute(EVAL_ID, REQUEST, store)

    assert meta(client)["status"] == {"S": "cancelled"}
    assert collected(client)[-1]["type"] == "eval_complete"


async def test_execute_reports_an_invalid_payload_from_the_seam(monkeypatch, client, store_factory):
    async def invalid(*_args, **_kwargs):
        raise interfaces.InvalidPayload("run_config is required")

    monkeypatch.setattr(interfaces, "load_seam", lambda: invalid)

    store = store_factory(EVAL_ID)
    store.begin(REQUEST)
    await lambda_app.execute(EVAL_ID, REQUEST, store)  # must not raise

    item = meta(client)
    assert item["status"] == {"S": "error"}
    assert json.loads(item["error"]["S"]) == {
        "code": "invalid_request",
        "message": "run_config is required",
    }


async def test_execute_reports_a_missing_engine_seam(monkeypatch, client, store_factory):
    def unavailable():
        raise interfaces.EvalEngineUnavailable("execute_evaluation_with_seam is not available")

    monkeypatch.setattr(interfaces, "load_seam", unavailable)

    store = store_factory(EVAL_ID)
    store.begin(REQUEST)
    await lambda_app.execute(EVAL_ID, REQUEST, store)

    assert json.loads(meta(client)["error"]["S"])["code"] == "eval_engine_unavailable"


async def test_execute_survives_a_dynamodb_failure_on_the_terminal_write(
    monkeypatch, client, store_factory
):
    async def exploding(*_args, **_kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(interfaces, "load_seam", lambda: exploding)

    store = store_factory(EVAL_ID)
    store.begin(REQUEST)
    client.fail_on["put_item"] = RuntimeError("table gone")

    await lambda_app.execute(EVAL_ID, REQUEST, store)  # logged, not raised


async def test_build_store_reads_the_runtime_environment(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "some-table")
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    store = lambda_app.build_store(EVAL_ID)

    assert store.table_name == "some-table"
    assert store.evaluation_id == EVAL_ID


async def test_build_store_refuses_an_unconfigured_runtime(monkeypatch):
    monkeypatch.delenv("TABLE_NAME", raising=False)
    with pytest.raises(ValueError, match="TABLE_NAME"):
        lambda_app.build_store(EVAL_ID)


async def test_a_run_that_ignores_the_cooperative_stop_is_cut_off(
    monkeypatch, client, store_factory
):
    """The cooperative stop is a request; the hard deadline is the enforcement.

    `cancelled` is polled between runs and before grading. A single streaming
    model call, or the whole grading phase, can start just inside the margin
    and then run for minutes with nothing polling it. Without a hard bound
    Lambda kills the environment mid-write and the evaluation reads `running`
    until its TTL -- the exact outcome the deadline exists to prevent.
    """
    async def hangs_forever(request, emit, store, cancelled=None, **_kwargs):
        # A run already finished and was mirrored to DynamoDB before the stall.
        store.put_run({"id": "run-1", "status": "completed"})
        await asyncio.sleep(3600)
        raise AssertionError("should have been cancelled")

    monkeypatch.setattr(interfaces, "load_seam", lambda: hangs_forever)

    store = store_factory(EVAL_ID)
    store.begin(REQUEST)
    store.mark_running()
    # Past the cooperative margin, with a sliver of hard budget left -- enough
    # for the engine to start and mirror its finished run, not enough for the
    # stall that follows.
    deadline = lambda_app.Deadline(lambda: lambda_app.HARD_DEADLINE_RESERVE_SECONDS + 0.25)

    status = await lambda_app.execute(EVAL_ID, REQUEST, store, deadline)

    assert status == "error"
    item = meta(client)
    assert item["status"] == {"S": "error"}
    error = json.loads(item["error"]["S"])
    assert error["code"] == lambda_app.DEADLINE_ERROR_CODE
    assert "in flight" in error["message"]
    # Whatever finished before the stall is still reported.
    assert json.loads(item["run_ids"]["S"]) == ["run-1"]


async def test_a_broken_clock_imposes_no_hard_bound(monkeypatch, client, store_factory):
    """A clock that cannot be read must not cut an evaluation short."""

    async def finishes(request, emit, store, cancelled=None, **_kwargs):
        return {"status": "completed", "result": {"ok": True}, "error": None, "run_ids": []}

    monkeypatch.setattr(interfaces, "load_seam", lambda: finishes)

    def broken() -> float:
        raise RuntimeError("no clock")

    store = store_factory(EVAL_ID)
    store.begin(REQUEST)
    store.mark_running()
    deadline = lambda_app.Deadline(broken)
    assert deadline.budget() is None

    assert await lambda_app.execute(EVAL_ID, REQUEST, store, deadline) == "completed"


def test_a_duplicate_delivery_does_not_execute_anything(client, store_factory):
    """Lambda async delivery is at-least-once; MaximumRetryAttempts: 0 does not change that.

    A second delivery that ran the evaluation again would buy every model run
    twice and overwrite the first delivery's EVENT# items, because each store
    numbers its events from zero. Losing the conditional pending -> running
    update is what says "someone else owns this".
    """
    first = store_factory(EVAL_ID)
    first.begin(REQUEST)
    assert first.mark_running() is True

    second = store_factory(EVAL_ID)
    second.begin(REQUEST)
    assert second.mark_running() is False


def test_the_handler_skips_a_delivery_it_did_not_claim(monkeypatch, client, store_factory):
    """And the handler must act on the claim, not merely record it."""

    def exploding_seam():
        raise AssertionError("a duplicate delivery must not execute the evaluation")

    monkeypatch.setattr(interfaces, "load_seam", exploding_seam)

    winner = store_factory(EVAL_ID)
    winner.begin(REQUEST)
    assert winner.mark_running() is True

    result = lambda_app.handler(PAYLOAD, store_factory=store_factory)

    assert result["status"] == "duplicate"
    assert result["evaluation_id"] == EVAL_ID
    # Untouched: still running under the delivery that won.
    assert meta(client)["status"] == {"S": "running"}
