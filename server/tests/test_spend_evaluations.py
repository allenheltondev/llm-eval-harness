"""Per-user spend, end to end: admission at the router, settlement in the engine.

Identity is injected the way :func:`nimbus.auth.require_auth` leaves it, as
verified claims on ``request.state.user``. Costs are exact: one run of
``fake.model`` reports 1M input tokens at a $1/M price, so it costs $1.00.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from nimbus import pricing, spend
from nimbus.config import Settings, get_settings
from nimbus.engine.fake_model import FakeModel, Text
from nimbus.errors import register_exception_handlers
from nimbus.evals import admission
from nimbus.evals import engine as evals_engine
from nimbus.evals import jobs as evals_jobs
from nimbus.evals.cloud import worker_payload
from nimbus.evals.judge import FakeJudgeModel, get_judge_factory
from nimbus.evals.schemas import EvaluationRequest
from nimbus.routers import runs
from nimbus.spend import Principal, SqliteSpendStore
from nimbus.store import db
from tests.test_evals_seam import Recorder, RecordingStore

MILLION = 1_000_000
MODEL = "fake.model"
CLAIMS = {"sub": "user-1", "email": "dev@example.com"}


@pytest.fixture(autouse=True)
def fake_prices(tmp_path, monkeypatch):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"models": {MODEL: {"input": 1, "output": 0}}}))
    monkeypatch.setenv(pricing.PRICING_FILE_ENV, str(path))
    pricing.reset_cache()
    spend.reset_cache()
    monkeypatch.setattr(evals_engine, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    yield
    pricing.reset_cache()
    spend.reset_cache()


@pytest.fixture(autouse=True)
def clean_jobs():
    evals_jobs.clear()
    yield
    evals_jobs.clear()


@pytest.fixture
def initialized_db(tmp_path):
    return db.init_db(str(tmp_path / "spend-evals.db"))


@pytest.fixture
def store(initialized_db) -> SqliteSpendStore:
    return SqliteSpendStore()


def dollar_run(_request) -> FakeModel:
    """One agent turn of exactly 1M input tokens: $1.00 at the fake price."""
    return FakeModel(script=[Text("done")], usage_per_turn=(MILLION, 0))


def build_app(claims: dict[str, Any] | None, settings: Settings) -> FastAPI:
    application = FastAPI()
    register_exception_handlers(application)
    application.include_router(runs.router, prefix="/api/v1")
    application.dependency_overrides[runs.get_model_factory] = lambda: dollar_run
    application.dependency_overrides[get_judge_factory] = lambda: lambda _id: FakeJudgeModel()
    application.dependency_overrides[get_settings] = lambda: settings

    @application.middleware("http")
    async def identify(request, call_next):
        if claims is not None:
            request.state.user = claims
        return await call_next(request)

    return application


@pytest.fixture
def make_client(initialized_db):
    def _make(claims: dict[str, Any] | None = CLAIMS, **settings: Any) -> httpx.AsyncClient:
        application = build_app(claims, Settings(_env_file=None, db_path="unused", **settings))
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://testserver"
        )

    yield _make


def body(runs: int = 2, prompt: str = "hi", **fields: Any) -> dict[str, Any]:
    return {
        "kind": "determinism",
        "n": runs,
        "run_config": {"model_id": MODEL, "user_prompt": prompt},
        **fields,
    }


async def finished(client: httpx.AsyncClient, evaluation_id: str) -> dict[str, Any]:
    for _ in range(400):
        detail = (await client.get(f"/api/v1/evaluations/{evaluation_id}")).json()
        if detail["status"] not in ("pending", "running"):
            return detail
        await asyncio.sleep(0)
    raise AssertionError("evaluation never finished")


def today() -> str:
    return spend.window_key("day")


def reported_cost_micros(detail: dict[str, Any]) -> int:
    """What the finished evaluation says it cost (runs and judge), in micro-dollars."""
    return spend.to_micros(detail["result"]["cost"]["total_usd"])


# --------------------------------------------------------------------------- #
# Attribution and settlement
# --------------------------------------------------------------------------- #


async def test_an_evaluation_is_attributed_and_counted_at_its_real_cost(make_client, store):
    client = make_client()

    accepted = await client.post("/api/v1/evaluations", json=body(runs=2))
    detail = await finished(client, accepted.json()["id"])

    assert detail["status"] == "completed"
    assert detail["config"]["user"] == "user-1"
    assert detail["config"]["user_email"] == "dev@example.com"
    # The up-front estimate was replaced by the real figure: two $1 runs plus the judge.
    assert reported_cost_micros(detail) > 2 * MILLION
    assert store.total("user-1", today()) == reported_cost_micros(detail)


async def test_an_unauthenticated_stack_records_and_counts_nothing(make_client, store):
    client = make_client(claims=None)

    accepted = await client.post("/api/v1/evaluations", json=body())
    detail = await finished(client, accepted.json()["id"])

    assert detail["status"] == "completed"
    assert "user" not in detail["config"]
    assert store.total("user-1", today()) == 0


async def test_a_client_cannot_name_its_own_identity_or_reservation(make_client, store):
    """The body's ``principal`` is overwritten by the token's: no spoofing, no free ride."""
    client = make_client()
    spoofed = {"user": "victim", "window": "2001-01-01", "reserved_micros": 999_999_999}

    accepted = await client.post("/api/v1/evaluations", json=body(principal=spoofed))
    detail = await finished(client, accepted.json()["id"])

    assert detail["config"]["user"] == "user-1"
    assert store.total("victim", "2001-01-01") == 0
    assert store.total("user-1", today()) == reported_cost_micros(detail)  # the real window


async def test_a_body_principal_is_dropped_even_when_there_is_no_identity(make_client, store):
    client = make_client(claims=None)

    accepted = await client.post(
        "/api/v1/evaluations", json=body(principal={"user": "victim", "reserved_micros": 5})
    )
    detail = await finished(client, accepted.json()["id"])

    assert "user" not in detail["config"]
    assert store.total("victim", today()) == 0


# --------------------------------------------------------------------------- #
# The ceiling
# --------------------------------------------------------------------------- #


async def test_an_evaluation_that_would_pass_the_limit_is_refused_before_it_starts(
    make_client, store
):
    client = make_client(spend_limit_usd=1.5)
    # 8M characters ~ 2M tokens ~ $2 per run (estimated), twice: well past $1.50.
    response = await client.post("/api/v1/evaluations", json=body(prompt="x" * 8_000_000))

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "spend_limit_reached"
    assert error["detail"]["limit_usd"] == 1.5
    assert store.total("user-1", today()) == 0  # the refused reservation was taken back
    assert (await client.get("/api/v1/evaluations")).json()["items"] == []  # nothing was created


async def test_once_the_limit_is_spent_the_next_evaluation_is_refused(make_client, store):
    client = make_client(spend_limit_usd=2.0)
    first = await client.post("/api/v1/evaluations", json=body(runs=2))
    done = await finished(client, first.json()["id"])  # two $1 runs plus the judge: past $2
    assert done["status"] == "completed"

    second = await client.post("/api/v1/evaluations", json=body(runs=2))

    assert second.status_code == 402
    assert store.total("user-1", today()) == reported_cost_micros(done)  # the refusal cost nothing


async def test_another_users_spend_does_not_count_against_you(make_client, store):
    store.add("someone-else", today(), 50 * MILLION)
    client = make_client(spend_limit_usd=5.0)

    accepted = await client.post("/api/v1/evaluations", json=body(runs=2))

    assert accepted.status_code == 202


async def test_a_limit_on_an_unpriced_model_is_refused_because_it_cannot_be_counted(
    make_client, store
):
    client = make_client(spend_limit_usd=5.0)
    unpriced = body()
    unpriced["run_config"]["model_id"] = "no.such-model"

    response = await client.post("/api/v1/evaluations", json=unpriced)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "spend_model_unpriced"
    assert store.total("user-1", today()) == 0


async def test_an_unpriced_model_is_fine_when_there_is_no_limit(make_client, store):
    client = make_client()
    unpriced = body()
    unpriced["run_config"]["model_id"] = "no.such-model"

    assert (await client.post("/api/v1/evaluations", json=unpriced)).status_code == 202


async def test_a_reservation_is_given_back_when_the_evaluation_cannot_start(make_client, store):
    client = make_client()

    response = await client.post(
        "/api/v1/evaluations", json={**body(), "kind": "grade", "run_ids": ["no-such-run"]}
    )

    assert response.status_code == 404
    assert store.total("user-1", today()) == 0


# --------------------------------------------------------------------------- #
# Settlement in the engine, however an evaluation ends
# --------------------------------------------------------------------------- #


def reserved_request(store, runs: int = 3) -> EvaluationRequest:
    payload = EvaluationRequest.model_validate(body(runs=runs, grader={"model_id": "j"}))
    principal = spend.reserve(store, Settings(_env_file=None), Principal(user="user-1"), 10.0)
    return payload.model_copy(update={"principal": principal})


async def execute(request: EvaluationRequest, store, *, recorder=None, factory=dollar_run):
    recorder = recorder or Recorder()
    return await evals_engine.execute_evaluation_with_seam(
        request,
        recorder.emit,
        RecordingStore("eval-spend"),
        recorder.cancelled,
        deps=evals_engine.EvalDeps(
            settings=Settings(_env_file=None),
            model_factory=factory,
            judge_factory=lambda _id, _provider="bedrock": FakeJudgeModel(),
            spend=store,
        ),
    )


async def test_a_completed_evaluation_settles_to_its_actual_cost(store):
    request = reserved_request(store)
    assert store.total("user-1", today()) == 10 * MILLION  # the reservation

    terminal = await execute(request, store)

    assert terminal["status"] == "completed"
    assert store.total("user-1", today()) == 3 * MILLION


async def test_an_evaluation_that_fails_every_run_gives_the_whole_reservation_back(store):
    def broken(_request):
        raise RuntimeError("model unavailable")

    terminal = await execute(reserved_request(store), store, factory=broken)

    assert terminal["status"] == "error"
    assert store.total("user-1", today()) == 0


async def test_a_cancelled_evaluation_is_charged_for_what_it_ran(store):
    request = reserved_request(store, runs=5)
    recorder = Recorder(cancel_after=1)

    terminal = await execute(request, store, recorder=recorder)

    assert terminal["status"] == "cancelled"
    spent = store.total("user-1", today())
    assert 0 < spent < 10 * MILLION  # ran some, not all, and the rest was given back
    assert spent % MILLION == 0


async def test_an_evaluation_without_a_principal_touches_no_counter(store):
    request = EvaluationRequest.model_validate(body(grader={"model_id": "j"}))

    await execute(request, store)

    assert store.total("user-1", today()) == 0


async def test_a_settlement_failure_never_fails_the_evaluation(store):
    class Broken:
        def add(self, *_args):
            raise RuntimeError("counter store down")

        def total(self, *_args):
            return 0

    terminal = await execute(reserved_request(store), Broken())

    assert terminal["status"] == "completed"  # the evaluation stands; the failure is logged


# --------------------------------------------------------------------------- #
# Admission, on its own
# --------------------------------------------------------------------------- #


def test_admission_reserves_the_estimate_against_the_callers_window(store):
    payload = EvaluationRequest.model_validate(body(runs=2, prompt="x" * 400_000))

    admitted = admission.admit(payload, CLAIMS, Settings(_env_file=None), store)

    # 400k chars ~ 100k tokens ~ $0.10 a run, two runs.
    assert admitted.principal.reserved_micros == pytest.approx(200_000, rel=0.01)
    assert store.total("user-1", today()) == admitted.principal.reserved_micros


def test_admission_does_not_mutate_the_request_it_was_given(store):
    payload = EvaluationRequest.model_validate(body())

    admission.admit(payload, CLAIMS, Settings(_env_file=None), store)

    assert payload.principal is None


def test_a_reserved_principal_survives_the_trip_to_the_cloud_worker(store):
    admitted = admission.admit(
        EvaluationRequest.model_validate(body()), CLAIMS, Settings(_env_file=None), store
    )

    sent = worker_payload("eval-1", admitted)["request"]
    received = EvaluationRequest.model_validate(sent)

    assert received.principal == admitted.principal  # so the worker settles the same window


def test_an_old_stored_config_without_a_user_is_unchanged():
    config = EvaluationRequest.model_validate(body()).stored_config()

    assert "user" not in config and "user_email" not in config


# --------------------------------------------------------------------------- #
# Single runs are counted too: the ceiling cannot be walked around
# --------------------------------------------------------------------------- #


def run_body(**fields: Any) -> dict[str, Any]:
    return {"model_id": MODEL, "user_prompt": "hi", "stream": False, **fields}


async def test_a_direct_run_is_counted_at_its_cost(make_client, store):
    client = make_client()

    response = await client.post("/api/v1/runs", json=run_body())

    assert response.status_code == 200
    assert store.total("user-1", today()) == MILLION  # one $1 run; no judge involved


async def test_a_streamed_run_settles_when_its_stream_ends(make_client, store):
    client = make_client()

    response = await client.post("/api/v1/runs", json=run_body(stream=True))

    assert response.status_code == 200
    assert store.total("user-1", today()) == MILLION


async def test_direct_runs_stop_at_the_limit_like_evaluations(make_client, store):
    client = make_client(spend_limit_usd=1.5)
    assert (await client.post("/api/v1/runs", json=run_body())).status_code == 200  # $1
    assert (await client.post("/api/v1/runs", json=run_body())).status_code == 200  # $2

    third = await client.post("/api/v1/runs", json=run_body())

    assert third.status_code == 402
    assert third.json()["error"]["code"] == "spend_limit_reached"
    assert store.total("user-1", today()) == 2 * MILLION


async def test_a_run_and_an_evaluation_share_one_ceiling(make_client, store):
    client = make_client(spend_limit_usd=1.0)
    assert (await client.post("/api/v1/runs", json=run_body())).status_code == 200  # spends $1

    refused = await client.post("/api/v1/evaluations", json=body(runs=2))

    assert refused.status_code == 402


async def test_a_run_that_fails_gives_its_reservation_back(make_client, store):
    def failing(_request):
        raise RuntimeError("model unavailable")

    client = make_client()
    client._transport.app.dependency_overrides[runs.get_model_factory] = lambda: failing

    response = await client.post("/api/v1/runs", json=run_body(user_prompt="x" * 400_000))

    assert response.status_code >= 400
    assert store.total("user-1", today()) == 0


async def test_a_client_disconnect_gives_back_what_the_run_did_not_spend(initialized_db, store):
    response = await runs.create_run(
        payload=runs.RunRequest(model_id=MODEL, user_prompt="x" * 400_000),
        settings=Settings(_env_file=None),
        model_factory=dollar_run,
        claims=CLAIMS,
    )
    iterator = response.body_iterator
    await anext(iterator)  # run_start: the reservation is standing
    assert store.total("user-1", today()) > 0

    await iterator.aclose()  # the client went away before any usage was reported

    assert store.total("user-1", today()) == 0


async def test_an_unpriced_model_cannot_run_under_a_limit(make_client, store):
    client = make_client(spend_limit_usd=5.0)

    response = await client.post("/api/v1/runs", json=run_body(model_id="no.such-model"))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "spend_model_unpriced"


async def test_an_unauthenticated_run_counts_nothing(make_client, store):
    client = make_client(claims=None)

    assert (await client.post("/api/v1/runs", json=run_body())).status_code == 200
    assert store.total("user-1", today()) == 0
