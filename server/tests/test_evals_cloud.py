"""Tests for the cloud evaluation lane: submit, read, stream, cancel.

Nothing here reaches AWS. DynamoDB is :class:`tests.fake_table.FakeTable` -- an
in-memory stand-in for a ``boto3`` resource ``Table`` that really does evaluate
the ``Key(...)`` conditions :mod:`evalharness.evals.ddb_reader` builds, so the
key schema and the query shapes are under test and not just mocked away. The one
place the *real* AWS API shape matters, ``InvokeAgentRuntime``, is exercised
through botocore's ``Stubber``, which validates parameters against the shipped
service model.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import httpx
import pytest
from botocore.stub import Stubber
from fastapi import FastAPI

from evalharness.config import Settings, get_settings
from evalharness.errors import UpstreamError, register_exception_handlers
from evalharness.evals import cloud
from evalharness.evals import jobs as evals_jobs
from evalharness.evals.ddb_reader import GSI1_PK, GSI1_SK, EvalTable
from evalharness.routers import health as health_router
from evalharness.routers import runs
from evalharness.store import db, ddb_items
from evalharness.store.history import EvaluationRecord
from tests.fake_table import FakeTable

FUNCTION_NAME = "llm-eval-harness-EvalWorkerFunction-ABC123"
TABLE_NAME = "llm-eval-harness-store"


# --------------------------------------------------------------------------- #
# Item builders (the contract's shapes)
# --------------------------------------------------------------------------- #

TS = datetime(2026, 8, 12, 9, 0, 0, tzinfo=UTC)


def eval_meta(
    evaluation_id: str,
    *,
    status: str = "running",
    kind: str = "determinism",
    ts: datetime = TS,
    result: dict | None = None,
    error: dict | None = None,
    run_ids: list[str] | None = None,
    seq_count: int = 0,
) -> dict[str, Any]:
    return {
        "pk": f"EVAL#{evaluation_id}",
        "sk": "META",
        "id": evaluation_id,
        "ts": ts.isoformat(),
        "kind": kind,
        "status": status,
        "config": json.dumps({"kind": kind, "n": 2, "run_config": None, "rubric": None}),
        "run_ids": json.dumps(run_ids or []),
        "result": json.dumps(result) if result is not None else None,
        "error": json.dumps(error) if error is not None else None,
        "seq_count": seq_count,
        GSI1_PK: "EVAL",
        GSI1_SK: ts.isoformat(),
        "expiresAt": 1_000_000,
    }


def eval_event(evaluation_id: str, seq: int, event: dict[str, Any]) -> dict[str, Any]:
    return {
        "pk": f"EVAL#{evaluation_id}",
        "sk": f"EVENT#{seq:08d}",
        "seq": seq,
        "ts": TS.isoformat(),
        "event": json.dumps(event),
        "expiresAt": 1_000_000,
    }


def run_meta(
    run_id: str, *, evaluation_id: str = "eval-1", ts: datetime = TS, status: str = "completed"
) -> dict[str, Any]:
    return {
        "pk": f"RUN#{run_id}",
        "sk": "META",
        "id": run_id,
        "ts": ts.isoformat(),
        "model_id": "anthropic.claude-3-sonnet",
        "system_prompt": "",
        "user_prompt": "Assess order B456",
        "config": json.dumps({"toolset": None}),
        "output": "Delayed; escalate.",
        "tool_transcript": json.dumps([]),
        "metrics": json.dumps({"latency_ms": 120}),
        "guardrail_trace": None,
        "status": status,
        "error": None,
        "evaluation_id": evaluation_id,
        GSI1_PK: "RUN",
        GSI1_SK: ts.isoformat(),
        "expiresAt": 1_000_000,
    }


COMPLETE_EVENT = {"type": "eval_complete", "status": "completed", "result": {"grade": "A"}}
START_EVENT = {"type": "eval_start", "evaluation_id": "eval-1", "kind": "determinism", "n": 1}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class RecordingInvoker:
    """A stand-in for the worker Lambda: remembers every submission."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    def invoke(self, evaluation_id: str, payload: dict[str, Any]) -> None:
        self.calls.append((evaluation_id, payload))
        if self.fail is not None:
            raise self.fail


class RecordingWriter:
    """A stand-in for ``DynamoEvalStore`` that lands items where the reader looks.

    The real store speaks the low-level client's AttributeValue dicts while
    ``FakeTable`` holds plain items, so this bridges the two: it records the
    calls (the *ordering* is the contract -- the row has to exist before the
    invoke) and writes the same ``ddb_items`` shape the reader decodes, which
    is what lets a test follow a POST straight into a GET.
    """

    def __init__(self, table: FakeTable, evaluation_id: str, invoker: RecordingInvoker) -> None:
        self.table = table
        self.evaluation_id = evaluation_id
        self._invoker = invoker
        self.calls: list[tuple[str, int]] = []

    def _record(self, name: str) -> None:
        # How many invokes had happened when this write landed.
        self.calls.append((name, len(self._invoker.calls)))

    def begin(self, request: dict[str, Any], *, kind: str | None = None) -> None:
        self._record("begin")
        self._put(status="pending", kind=kind or request.get("kind"), error=None)

    def complete(self, status: str, *, result=None, error=None, run_ids=None) -> None:
        self._record("complete")
        self._put(status=status, kind=None, error=error)

    def _put(self, *, status: str, kind: str | None, error: dict | None) -> None:
        existing = self.table.get_item(
            Key={"pk": f"EVAL#{self.evaluation_id}", "sk": "META"}
        ).get("Item")
        record = EvaluationRecord(
            id=self.evaluation_id,
            ts=datetime.now(UTC),
            kind=kind or (existing or {}).get("kind") or "determinism",
            status=status,
            config={},
            run_ids=[],
            result=None,
            progress=None,
            error=error,
        )
        self.table.put_item(Item=ddb_items.evaluation_item(record))


@pytest.fixture
def writers(table, invoker) -> dict[str, RecordingWriter]:
    """Every writer ``submit`` built, by evaluation id."""
    return {}


@pytest.fixture
def writer_factory(table, invoker, writers):
    def build(evaluation_id: str) -> RecordingWriter:
        writer = RecordingWriter(table, evaluation_id, invoker)
        writers[evaluation_id] = writer
        return writer

    return lambda: build


@pytest.fixture
def initialized_db(tmp_path):
    return db.init_db(str(tmp_path / "cloud.db"))


@pytest.fixture(autouse=True)
def clean_job_registry():
    evals_jobs.clear()
    yield
    evals_jobs.clear()


@pytest.fixture(autouse=True)
def instant_polling(monkeypatch):
    """The events stream, minus the waiting."""
    monkeypatch.setattr(cloud, "POLL_INTERVAL_SECONDS", 0.0)


@pytest.fixture
def table() -> FakeTable:
    return FakeTable()


@pytest.fixture
def invoker() -> RecordingInvoker:
    return RecordingInvoker()


@pytest.fixture
def cloud_settings() -> Settings:
    return Settings(eval_function_name=FUNCTION_NAME, eval_table=TABLE_NAME)


@pytest.fixture
def app(initialized_db, table, invoker, cloud_settings, writer_factory) -> FastAPI:
    application = FastAPI()
    register_exception_handlers(application)
    application.include_router(runs.router, prefix="/api/v1")
    application.include_router(health_router.router, prefix="/api/v1")
    application.dependency_overrides[get_settings] = lambda: cloud_settings
    application.dependency_overrides[cloud.get_eval_table] = lambda: EvalTable(table)
    application.dependency_overrides[cloud.get_invoker] = lambda: invoker
    application.dependency_overrides[cloud.get_eval_writer_factory] = writer_factory
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture
def unconfigured_app(initialized_db) -> FastAPI:
    """The same router with the lane switched off (the default settings)."""
    application = FastAPI()
    register_exception_handlers(application)
    application.include_router(runs.router, prefix="/api/v1")
    application.include_router(health_router.router, prefix="/api/v1")
    application.dependency_overrides[get_settings] = lambda: Settings()
    return application


@pytest.fixture
async def unconfigured_client(unconfigured_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=unconfigured_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def determinism_body(**overrides) -> dict:
    payload: dict[str, Any] = {
        "kind": "determinism",
        "n": 4,
        "execution": "cloud",
        "run_config": {
            "model_id": "anthropic.claude-3-sonnet",
            "user_prompt": "Assess order B456",
        },
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Submit
# --------------------------------------------------------------------------- #


async def test_submitting_a_cloud_evaluation_invokes_the_runtime(client, invoker):
    accepted = await client.post("/api/v1/evaluations", json=determinism_body())

    assert accepted.status_code == 202
    body = accepted.json()
    assert body["status"] == "pending"
    assert body["kind"] == "determinism"
    assert body["execution"] == "cloud"
    assert body["run_ids"] == []
    assert body["config"]["n"] == 4

    assert len(invoker.calls) == 1
    evaluation_id, payload = invoker.calls[0]
    assert evaluation_id == body["id"]
    assert payload["evaluation_id"] == body["id"]
    # The worker gets the request verbatim, minus the lane selector itself.
    assert "execution" not in payload["request"]
    assert payload["request"]["kind"] == "determinism"
    assert payload["request"]["n"] == 4
    assert payload["request"]["run_config"]["user_prompt"] == "Assess order B456"
    assert payload["request"]["run_config"]["stream"] is False
    assert payload["request"]["grader"]["model_id"] == "amazon.nova-pro-v1:0"


async def test_a_cloud_submission_writes_nothing_to_the_local_history(client, invoker):
    """The pending row goes to DynamoDB, never to this server's own history.

    The distinction only shows when the two differ -- a laptop driving the
    cloud lane keeps its history in SQLite -- and it matters: the detail and
    event routes read the local repository *first* and only fall back to
    DynamoDB on NotFoundError, so a local row would shadow the worker's and
    replay an empty `pending` record forever.
    """
    accepted = await client.post("/api/v1/evaluations", json=determinism_body())

    listing = await client.get("/api/v1/evaluations")
    assert listing.json()["items"] == []

    # ...but the detail route resolves it right away through the fallback.
    detail = await client.get(f"/api/v1/evaluations/{accepted.json()['id']}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "pending"
    # `/events` gates on the same lookup (`evals_cloud.get_evaluation`) before
    # it streams anything, so this is also what stops the SPA following its own
    # 202 into a 404.


async def test_the_row_exists_before_the_invoke_is_queued(client, invoker, writers):
    """Ordering is the contract, not an implementation detail.

    `InvocationType="Event"` means the 202 says only that AWS queued the event.
    The worker may not run -- and so may not write META -- for seconds, longer
    on a cold start. The SPA follows the 202 straight into the event stream,
    which preflights the row and 404s while it is absent, so a successfully
    queued evaluation would surface to the user as an error.
    """
    accepted = await client.post("/api/v1/evaluations", json=determinism_body())
    writer = writers[accepted.json()["id"]]

    # `begin` landed while zero invokes had been made.
    assert writer.calls == [("begin", 0)]
    assert len(invoker.calls) == 1


async def test_an_evaluation_that_was_never_queued_does_not_sit_pending(table, initialized_db):
    """If the invoke fails, the row we just wrote has to be settled, not abandoned."""
    invoker = RecordingInvoker(fail=UpstreamError("nope", code="eval_worker_unavailable"))
    writers: dict[str, RecordingWriter] = {}

    def build(evaluation_id: str) -> RecordingWriter:
        writers[evaluation_id] = RecordingWriter(table, evaluation_id, invoker)
        return writers[evaluation_id]

    application = FastAPI()
    register_exception_handlers(application)
    application.include_router(runs.router, prefix="/api/v1")
    settings = Settings(eval_function_name=FUNCTION_NAME, eval_table=TABLE_NAME)
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[cloud.get_eval_table] = lambda: EvalTable(table)
    application.dependency_overrides[cloud.get_invoker] = lambda: invoker
    application.dependency_overrides[cloud.get_eval_writer_factory] = lambda: build

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.post("/api/v1/evaluations", json=determinism_body())

    assert response.status_code >= 500 or response.json()["error"]["code"] == (
        "eval_worker_unavailable"
    )
    (writer,) = writers.values()
    assert [name for name, _ in writer.calls] == ["begin", "complete"]
    item = table.get_item(Key={"pk": f"EVAL#{writer.evaluation_id}", "sk": "META"})["Item"]
    assert item["status"] == "error"


async def test_a_cloud_grade_submission_carries_its_run_ids(client, invoker):
    accepted = await client.post(
        "/api/v1/evaluations",
        json={"kind": "grade", "run_ids": ["run-a", "run-b"], "execution": "cloud"},
    )

    assert accepted.status_code == 202
    assert accepted.json()["run_ids"] == ["run-a", "run-b"]
    # Unlike the local lane, unknown ids are not a 404 here: they may live in DDB.
    assert invoker.calls[0][1]["request"]["run_ids"] == ["run-a", "run-b"]


async def test_the_cloud_lane_is_400_when_unconfigured(unconfigured_client):
    response = await unconfigured_client.post("/api/v1/evaluations", json=determinism_body())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "cloud_lane_unavailable"


async def test_a_local_submission_is_unaffected_by_the_cloud_settings(client, invoker):
    """``execution`` defaults to local, and local still means local."""
    response = await client.post(
        "/api/v1/evaluations",
        json={"kind": "grade", "run_ids": ["nope"]},
    )

    assert response.status_code == 404  # the local lane validates its run ids
    assert invoker.calls == []


def test_the_real_invoker_invokes_the_function_asynchronously():
    """Validated against botocore's shipped ``lambda`` service model.

    ``InvocationType="Event"`` is the contract: AWS queues the event and answers
    202 immediately, which is what lets an evaluation run for minutes while the
    server's POST returns at once.
    """
    client = boto3.client("lambda", region_name="us-east-1")
    payload = {"evaluation_id": "e" * 32, "request": {"kind": "determinism"}}

    with Stubber(client) as stubber:
        stubber.add_response(
            "invoke",
            {"StatusCode": 202},
            expected_params={
                "FunctionName": FUNCTION_NAME,
                "InvocationType": "Event",
                "Payload": json.dumps(payload).encode("utf-8"),
            },
        )
        invoker = cloud.LambdaInvoker(FUNCTION_NAME, "us-east-1", client=client)
        invoker.invoke("e" * 32, payload)
        stubber.assert_no_pending_responses()


def test_the_real_invoker_refuses_to_report_success_when_aws_did_not_accept():
    """Anything but 202 means the event was not queued.

    Swallowing that would tell the caller an evaluation had started when
    nothing is ever going to run it, and the row would read `pending` forever.
    """
    client = boto3.client("lambda", region_name="us-east-1")
    payload = {"evaluation_id": "e" * 32, "request": {"kind": "determinism"}}

    with Stubber(client) as stubber:
        stubber.add_response("invoke", {"StatusCode": 200}, expected_params=None)
        invoker = cloud.LambdaInvoker(FUNCTION_NAME, "us-east-1", client=client)
        with pytest.raises(UpstreamError) as caught:
            invoker.invoke("e" * 32, payload)

    assert caught.value.code == "eval_worker_unavailable"
    assert caught.value.detail["status_code"] == 200


# --------------------------------------------------------------------------- #
# Detail, listing, run fallback
# --------------------------------------------------------------------------- #


async def test_getting_a_cloud_evaluation_falls_back_to_dynamodb(client, table):
    table.add(
        eval_meta(
            "eval-1",
            status="completed",
            run_ids=["run-a"],
            result={"grade": "A", "score": 95},
        )
    )

    response = await client.get("/api/v1/evaluations/eval-1")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "eval-1"
    assert body["status"] == "completed"
    assert body["execution"] == "cloud"
    assert body["run_ids"] == ["run-a"]
    assert body["result"] == {"grade": "A", "score": 95}
    assert body["config"]["n"] == 2


async def test_an_unknown_evaluation_is_still_a_404_with_the_lane_on(client):
    response = await client.get("/api/v1/evaluations/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_listing_cloud_evaluations_reads_the_gsi1_partition(client, table):
    for index in range(3):
        table.add(
            eval_meta(f"eval-{index}", ts=TS + timedelta(minutes=index), status="completed")
        )
    table.add(run_meta("run-a"))  # the RUN partition must not leak into it

    response = await client.get("/api/v1/evaluations?execution=cloud")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == ["eval-2", "eval-1", "eval-0"]  # ts desc
    assert {item["execution"] for item in items} == {"cloud"}


async def test_the_cloud_listing_paginates_by_cursor(client, table):
    for index in range(3):
        table.add(eval_meta(f"eval-{index}", ts=TS + timedelta(minutes=index)))

    first = await client.get("/api/v1/evaluations?execution=cloud&limit=2")
    assert [item["id"] for item in first.json()["items"]] == ["eval-2", "eval-1"]
    cursor = first.json()["next_cursor"]
    assert cursor

    second = await client.get(f"/api/v1/evaluations?execution=cloud&limit=2&cursor={cursor}")
    assert [item["id"] for item in second.json()["items"]] == ["eval-0"]
    assert second.json()["next_cursor"] is None


async def test_a_malformed_cloud_cursor_is_a_400(client, table):
    response = await client.get("/api/v1/evaluations?execution=cloud&cursor=!!!not-base64!!!")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


async def test_the_cloud_listing_can_be_narrowed_by_kind_and_status(client, table):
    table.add(eval_meta("eval-0", kind="determinism", status="completed"))
    table.add(eval_meta("eval-1", kind="grade", status="completed", ts=TS + timedelta(minutes=1)))

    response = await client.get("/api/v1/evaluations?execution=cloud&kind=grade")

    assert [item["id"] for item in response.json()["items"]] == ["eval-1"]


async def test_the_default_listing_stays_local(client, table):
    table.add(eval_meta("eval-1"))

    response = await client.get("/api/v1/evaluations")

    assert response.json()["items"] == []


async def test_cloud_listings_are_400_when_the_lane_is_off(unconfigured_client):
    response = await unconfigured_client.get("/api/v1/evaluations?execution=cloud")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "cloud_lane_unavailable"


async def test_getting_a_run_falls_back_to_dynamodb(client, table):
    table.add(run_meta("run-a"))

    response = await client.get("/api/v1/runs/run-a")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "run-a"
    assert body["output"] == "Delayed; escalate."
    assert body["metrics"] == {"latency_ms": 120}
    assert body["tool_transcript"] == []
    assert body["config"] == {"toolset": None}


async def test_an_unknown_run_is_still_a_404(client):
    response = await client.get("/api/v1/runs/nope")

    assert response.status_code == 404


async def test_listing_cloud_runs_reads_the_run_gsi1_partition(client, table):
    for index in range(2):
        table.add(run_meta(f"run-{index}", ts=TS + timedelta(minutes=index)))
    table.add(eval_meta("eval-1"))  # the EVAL partition must not leak into it

    response = await client.get("/api/v1/runs?execution=cloud")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == ["run-1", "run-0"]
    assert items[0]["metrics"] == {"latency_ms": 120}
    assert items[0]["model_id"] == "anthropic.claude-3-sonnet"


async def test_the_default_run_listing_stays_local(client, table):
    table.add(run_meta("run-a"))

    response = await client.get("/api/v1/runs")

    assert response.json()["items"] == []


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #


async def test_cancelling_a_cloud_evaluation_puts_the_cancel_item(client, table):
    table.add(eval_meta("eval-1", status="running"))

    response = await client.delete("/api/v1/evaluations/eval-1")

    assert response.status_code == 204
    assert [(item["pk"], item["sk"]) for item in table.puts] == [("EVAL#eval-1", "CANCEL")]
    assert table.puts[0]["expiresAt"] > 0
    assert table.puts[0]["ts"]


async def test_cancelling_a_finished_cloud_evaluation_is_a_409(client, table):
    table.add(eval_meta("eval-1", status="completed"))

    response = await client.delete("/api/v1/evaluations/eval-1")

    assert response.status_code == 409
    assert response.json()["error"]["detail"]["status"] == "completed"
    assert table.puts == []


async def test_cancelling_an_unknown_evaluation_is_a_404(client):
    response = await client.delete("/api/v1/evaluations/nope")

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# The event stream
# --------------------------------------------------------------------------- #


async def read_events(client: httpx.AsyncClient, evaluation_id: str) -> list[dict]:
    async with client.stream("GET", f"/api/v1/evaluations/{evaluation_id}/events") as response:
        assert response.status_code == 200
        assert "application/x-ndjson" in response.headers["content-type"]
        return [json.loads(line) async for line in response.aiter_lines() if line]


async def test_a_finished_cloud_evaluation_replays_its_event_log(client, table):
    table.add(eval_meta("eval-1", status="completed", seq_count=4))
    table.add(
        eval_event("eval-1", 0, START_EVENT),
        eval_event("eval-1", 1, {"type": "run_started", "index": 0}),
        eval_event("eval-1", 2, {"type": "grading_started"}),
        eval_event("eval-1", 3, COMPLETE_EVENT),
    )

    events = await read_events(client, "eval-1")

    assert [event["type"] for event in events] == [
        "eval_start",
        "run_started",
        "grading_started",
        "eval_complete",
    ]
    # Nothing is synthesized: the worker's own final event is passed through.
    assert events[-1] == COMPLETE_EVENT


async def test_the_stream_follows_new_events_then_stops_on_eval_complete(table):
    """Driven one event at a time so the follow loop is deterministic."""
    fake = table
    fake.add(eval_meta("eval-1", status="running"))
    fake.add(
        eval_event("eval-1", 0, START_EVENT),
        eval_event("eval-1", 1, {"type": "run_started", "index": 0}),
    )
    stream = cloud.stream_events(EvalTable(fake), "eval-1")

    assert json.loads(await anext(stream))["type"] == "eval_start"
    assert json.loads(await anext(stream))["type"] == "run_started"

    # A new event lands while the reader is parked: the next pull picks it up.
    fake.add(eval_event("eval-1", 2, {"type": "run_completed", "index": 0, "run_id": "r"}))
    assert json.loads(await anext(stream))["type"] == "run_completed"

    fake.add(eval_meta("eval-1", status="completed"), eval_event("eval-1", 3, COMPLETE_EVENT))
    assert json.loads(await anext(stream)) == COMPLETE_EVENT

    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_the_stream_gives_up_on_a_terminal_meta_with_no_final_event(client, table):
    """A worker that died after writing its status must not hang the reader."""
    table.add(eval_meta("eval-1", status="error", seq_count=1))
    table.add(eval_event("eval-1", 0, START_EVENT))

    events = await read_events(client, "eval-1")

    assert [event["type"] for event in events] == ["eval_start"]


async def test_events_for_an_unknown_cloud_evaluation_are_a_404(client):
    response = await client.get("/api/v1/evaluations/nope/events")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_events_only_read_the_event_range(table):
    """``CANCEL`` and ``META`` share the partition and must never be streamed."""
    fake = table
    fake.add(eval_meta("eval-1", status="completed"))
    fake.add({"pk": "EVAL#eval-1", "sk": "CANCEL", "ts": TS.isoformat()})
    fake.add(eval_event("eval-1", 0, COMPLETE_EVENT))

    items = EvalTable(fake).events_after("eval-1")

    assert [item["sk"] for item in items] == ["EVENT#00000000"]


async def test_events_after_a_sequence_number_are_exclusive(table):
    fake = table
    for seq in range(3):
        fake.add(eval_event("eval-1", seq, {"type": "run_started", "index": seq}))

    items = EvalTable(fake).events_after("eval-1", after_seq=1)

    assert [int(item["seq"]) for item in items] == [2]


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


async def test_health_reports_the_cloud_lane_as_configured(client):
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["cloud_evals"] == {"configured": True}


async def test_health_reports_the_cloud_lane_as_unconfigured(unconfigured_client):
    response = await unconfigured_client.get("/api/v1/health")

    assert response.json()["cloud_evals"] == {"configured": False}


async def test_a_half_configured_lane_is_not_configured():
    assert cloud.is_configured(Settings(eval_function_name=FUNCTION_NAME)) is False
    assert cloud.is_configured(Settings(eval_table=TABLE_NAME)) is False
    assert cloud.is_configured(Settings(eval_function_name=FUNCTION_NAME, eval_table=TABLE_NAME))


# --------------------------------------------------------------------------- #
# Item -> response mapping edge cases
# --------------------------------------------------------------------------- #


async def test_the_cloud_listing_can_be_narrowed_by_status_alone(client, table):
    table.add(eval_meta("eval-0", kind="determinism", status="completed"))
    table.add(eval_meta("eval-1", kind="determinism", status="error", ts=TS + timedelta(minutes=1)))

    response = await client.get("/api/v1/evaluations?execution=cloud&status=error")

    assert [item["id"] for item in response.json()["items"]] == ["eval-1"]


async def test_a_native_json_value_in_config_is_passed_through_unparsed(client, table):
    """A permissive writer may store config/result as a native map instead of a
    JSON string; ``_json`` must accept both instead of only ``json.loads``-ing."""
    item = eval_meta("eval-native", status="completed", result={"grade": "A"})
    item["config"] = {"kind": "determinism", "n": 1, "run_config": None, "rubric": None}
    table.add(item)

    response = await client.get("/api/v1/evaluations/eval-native")

    assert response.json()["config"]["n"] == 1


def test_get_invoker_reuses_the_same_instance_for_the_same_settings():
    settings = Settings(eval_function_name=FUNCTION_NAME, aws_region="us-east-1")

    first = cloud.get_invoker(settings)
    second = cloud.get_invoker(settings)

    assert first is not None
    assert first is second


def test_get_invoker_builds_a_distinct_instance_per_function():
    settings_a = Settings(eval_function_name=FUNCTION_NAME, aws_region="us-east-1")
    settings_b = Settings(eval_function_name=FUNCTION_NAME + "-other", aws_region="us-east-1")

    first = cloud.get_invoker(settings_a)
    second = cloud.get_invoker(settings_b)

    assert first is not second


def test_get_invoker_is_none_when_unconfigured():
    assert cloud.get_invoker(Settings()) is None
