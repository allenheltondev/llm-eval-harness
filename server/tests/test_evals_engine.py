"""Unit tests for the evaluation job internals: retries and the event log.

The router tests drive these through HTTP; these drive them directly, where a
single repeat's attempt sequence is deterministic (no concurrency to interleave).
"""

import asyncio
import json

import pytest
from botocore.exceptions import ClientError
from sqlmodel import Session
from strands.types.exceptions import ModelThrottledException

from nimbus.config import Settings
from nimbus.engine.fake_model import Error, FakeModel, Text
from nimbus.evals import engine as evals_engine
from nimbus.evals import jobs as evals_jobs
from nimbus.evals.events import EvalCompleteEvent, EvalStartEvent, RunStartedEvent
from nimbus.evals.judge import FakeJudgeModel
from nimbus.evals.schemas import EvaluationRequest
from nimbus.store import db, history

THROTTLE = [Error(ModelThrottledException("slow down"))]


@pytest.fixture
def initialized_db(tmp_path):
    return db.init_db(str(tmp_path / "evals_engine.db"))


@pytest.fixture(autouse=True)
def instant_retries(monkeypatch):
    monkeypatch.setattr(evals_engine, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))


def _job(evaluation_request, index: int = 0):
    """The single job a one-run determinism request makes."""
    return evals_engine._Job(index, evaluation_request.run_config)

def deps(factory) -> evals_engine.EvalDeps:
    return evals_engine.EvalDeps(
        settings=Settings(),
        model_factory=factory,
        judge_factory=lambda _model_id: FakeJudgeModel(),
    )


def request(n: int = 2) -> EvaluationRequest:
    return EvaluationRequest.model_validate(
        {
            "kind": "determinism",
            "n": n,
            "run_config": {"model_id": "m", "user_prompt": "Assess order B456"},
        }
    )


class SequencedModels:
    """Hands out one scripted ``FakeModel`` per attempt, in order."""

    def __init__(self, *scripts: list) -> None:
        self.scripts = list(scripts)
        self.calls = 0

    def __call__(self, _request) -> FakeModel:
        script = self.scripts[self.calls] if self.calls < len(self.scripts) else self.scripts[-1]
        self.calls += 1
        return FakeModel(script=list(script))


# --------------------------------------------------------------------------- #
# Throttle retries
# --------------------------------------------------------------------------- #


async def test_a_run_that_throttles_twice_then_succeeds(initialized_db):
    models = SequencedModels(THROTTLE, THROTTLE, [Text("finally")])

    outcome = await evals_engine._execute_with_retries(_job(request()), deps(models))

    assert models.calls == 3
    assert outcome.succeeded
    assert outcome.attempts == 3
    assert outcome.output == "finally"
    # The run id kept is the successful attempt's; the throttled attempts are
    # still on record as their own error rows.
    with Session(db.get_engine()) as session:
        rows, _ = history.list_runs(session)
        assert history.get_run(session, outcome.run_id).status == "completed"
    assert len(rows) == 3
    assert sorted(row.status for row in rows) == ["completed", "error", "error"]


async def test_a_run_that_never_stops_throttling_gives_up_after_two_retries(initialized_db):
    models = SequencedModels(THROTTLE)

    outcome = await evals_engine._execute_with_retries(_job(request()), deps(models))

    assert models.calls == 3
    assert not outcome.succeeded
    assert outcome.error["code"] == "model_throttled"
    assert outcome.error["retryable"] is True


async def test_a_non_throttle_failure_is_not_retried(initialized_db):
    models = SequencedModels([Error(RuntimeError("kaboom"))])

    outcome = await evals_engine._execute_with_retries(_job(request()), deps(models))

    assert models.calls == 1
    assert outcome.error["code"] == "internal_error"


async def test_backoff_is_actually_awaited_between_attempts(initialized_db, monkeypatch):
    slept: list[float] = []

    async def record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(evals_engine, "RETRY_BACKOFF_SECONDS", (5.0, 10.0))
    monkeypatch.setattr(evals_engine, "_jittered", lambda seconds: seconds)
    monkeypatch.setattr(evals_engine.asyncio, "sleep", record)
    models = SequencedModels(THROTTLE, THROTTLE, [Text("finally")])

    await evals_engine._execute_with_retries(_job(request()), deps(models))

    assert slept == [5.0, 10.0]


async def test_a_retryable_provider_error_is_retried_like_a_throttle(initialized_db):
    server_error = ClientError(
        {"Error": {"Code": "InternalServerException", "Message": "try later"}}, "ConverseStream"
    )
    models = SequencedModels([Error(server_error)], [Text("recovered")])

    outcome = await evals_engine._execute_with_retries(_job(request()), deps(models))

    assert models.calls == 2
    assert outcome.succeeded
    assert outcome.attempts == 2


async def test_a_non_retryable_provider_error_is_not_retried(initialized_db):
    denied = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "ConverseStream"
    )
    models = SequencedModels([Error(denied)])

    outcome = await evals_engine._execute_with_retries(_job(request()), deps(models))

    assert models.calls == 1
    assert outcome.error["retryable"] is False


def test_backoff_jitter_stays_within_half_to_full_of_the_nominal_delay():
    delays = [evals_engine._jittered(10.0) for _ in range(500)]

    assert all(5.0 <= delay <= 10.0 for delay in delays)
    assert len(set(delays)) > 1  # actually random, not a constant


def hanging_models(hang_on_calls: set[int]):
    """A factory whose listed (1-based) attempts never produce output."""
    created: list[FakeModel] = []

    def factory(_request) -> FakeModel:
        model = FakeModel(script=[Text("done")])
        created.append(model)
        if len(created) in hang_on_calls:

            async def hang(*_args, **_kwargs):
                await asyncio.sleep(30)
                yield {}

            model.stream = hang  # type: ignore[method-assign]
        return model

    factory.created = created  # type: ignore[attr-defined]
    return factory


def deps_with_timeout(factory, seconds: float) -> evals_engine.EvalDeps:
    return evals_engine.EvalDeps(
        settings=Settings(run_timeout_seconds=seconds),
        model_factory=factory,
        judge_factory=lambda _model_id: FakeJudgeModel(),
    )


async def test_a_run_past_the_timeout_is_cancelled_and_retried(initialized_db):
    factory = hanging_models({1})

    outcome = await evals_engine._execute_with_retries(
        _job(request()), deps_with_timeout(factory, 0.05)
    )

    assert outcome.succeeded
    assert outcome.attempts == 2
    assert len(factory.created) == 2
    with Session(db.get_engine()) as session:
        rows, _ = history.list_runs(session)
    assert sorted(row.status for row in rows) == ["cancelled", "completed"]


async def test_a_run_that_always_times_out_reports_run_timeout(initialized_db):
    factory = hanging_models({1, 2, 3})

    outcome = await evals_engine._execute_with_retries(
        _job(request()), deps_with_timeout(factory, 0.05)
    )

    assert not outcome.succeeded
    assert outcome.error["code"] == evals_engine.RUN_TIMEOUT_ERROR_CODE
    assert outcome.error["retryable"] is True
    assert "0.05s" in outcome.error["message"]
    assert outcome.run_id is not None  # the cut-off attempt is still on record


async def test_no_timeout_is_applied_by_default(initialized_db):
    assert Settings().run_timeout_seconds == 0
    models = SequencedModels([Text("fine")])

    outcome = await evals_engine._execute_with_retries(_job(request()), deps(models))

    assert outcome.succeeded


async def test_a_setup_failure_before_run_start_is_reported_as_the_outcomes_error(
    initialized_db,
):
    """An unknown toolset raises BadRequestError before the runner
    ever yields ``run_start`` -- this is the AppError branch of ``_execute_once``,
    not the generic ``classify_error`` one that in-band run failures take."""
    models = SequencedModels([Text("unused")])
    bad_request = EvaluationRequest.model_validate(
        {
            "kind": "determinism",
            "n": 1,
            "run_config": {
                "model_id": "m",
                "user_prompt": "hi",
                "toolset": "does-not-exist",
            },
        }
    )

    outcome = await evals_engine._execute_once(_job(bad_request), deps(models))

    assert not outcome.succeeded
    assert outcome.run_id is None
    assert outcome.error["code"] == "unknown_toolset"
    assert outcome.error["retryable"] is False
    assert "does-not-exist" in outcome.error["message"]
    # The model was never invoked -- the failure is entirely in setup.
    assert models.calls == 0


async def test_a_completed_run_reports_its_summary(initialized_db):
    models = SequencedModels([Text("hello")])

    outcome = await evals_engine._execute_with_retries(_job(request()), deps(models))

    assert outcome.summary()["output_chars"] == 5
    assert outcome.summary()["tool_calls"] == 0
    assert outcome.summary()["duration_ms"] >= 0


# --------------------------------------------------------------------------- #
# The job event log
# --------------------------------------------------------------------------- #


async def test_subscribers_get_a_replay_then_live_events():
    job = evals_jobs.EvalJob("eval-1")
    job.emit(EvalStartEvent(evaluation_id="eval-1", kind="determinism", n=2))

    lines: list[str] = []

    async def follow():
        async for line in job.follow():
            lines.append(line)

    follower = asyncio.create_task(follow())
    await asyncio.sleep(0)  # let the replay flush

    job.emit(RunStartedEvent(index=0))
    job.emit(EvalCompleteEvent(status="completed", result={"grade": "A"}))
    job.close()
    await asyncio.wait_for(follower, timeout=5)

    assert [line.count("\n") for line in lines] == [1, 1, 1]
    assert [json.loads(line)["type"] for line in lines] == [
        "eval_start",
        "run_started",
        "eval_complete",
    ]


async def test_two_subscribers_see_the_same_stream():
    job = evals_jobs.EvalJob("eval-2")
    job.emit(EvalStartEvent(evaluation_id="eval-2", kind="grade", n=1))

    async def drain() -> list[dict]:
        return [json.loads(line) async for line in job.follow()]

    first = asyncio.create_task(drain())
    second = asyncio.create_task(drain())
    await asyncio.sleep(0)

    job.emit(EvalCompleteEvent(status="completed", result=None))
    job.close()

    assert await first == await second


async def test_a_finished_job_replays_its_log_and_stops():
    job = evals_jobs.EvalJob("eval-3")
    job.emit(EvalStartEvent(evaluation_id="eval-3", kind="grade", n=1))
    job.emit(EvalCompleteEvent(status="completed", result=None))
    job.close()

    lines = [line async for line in job.follow()]

    assert len(lines) == 2
    assert job.subscribe()[1] is None  # no queue handed to a late subscriber


def test_the_registry_prunes_finished_jobs(monkeypatch):
    monkeypatch.setattr(evals_jobs, "MAX_RETAINED_JOBS", 3)
    evals_jobs.clear()

    for index in range(3):
        job = evals_jobs.register(f"eval-{index}")
        job.close()
    evals_jobs.register("eval-new")

    assert evals_jobs.get("eval-0") is None
    assert evals_jobs.get("eval-new") is not None
    evals_jobs.clear()
