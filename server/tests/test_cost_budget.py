"""Cost tracking and ``max_cost_usd`` budgets, through the real engine.

Every run here is a :class:`FakeModel` with a fake price, set through the same
``NIMBUS_PRICING_FILE`` override a user would write: one run of ``fake.model``
reports exactly 1M input tokens at $1/M, so it costs exactly $1.00. That makes
the budget arithmetic checkable to the cent, in the local lane and in the cloud
worker alike.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlmodel import Session

from nimbus import pricing
from nimbus.config import Settings
from nimbus.engine import runner
from nimbus.engine.events import MetricsEvent
from nimbus.engine.fake_model import FakeModel, Text
from nimbus.engine.mapper import EventMapper
from nimbus.engine.schemas import RunRequest
from nimbus.errors import BadRequestError
from nimbus.evals import budget
from nimbus.evals import engine as evals_engine
from nimbus.evals import jobs as evals_jobs
from nimbus.evals.judge import FakeJudgeModel
from nimbus.evals.schemas import EvaluationRequest
from nimbus.store import db, history
from tests.test_evals_seam import Recorder, RecordingStore

MILLION = 1_000_000
FAKE_MODEL = "fake.model"
FAKE_JUDGE = "fake.judge"


@pytest.fixture(autouse=True)
def fake_prices(tmp_path, monkeypatch):
    """$1/M input on the application's model, $2/M input + $10/M output on the judge."""
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps(
            {
                "models": {
                    FAKE_MODEL: {"input": 1, "output": 0},
                    FAKE_JUDGE: {"input": 2, "output": 10},
                }
            }
        )
    )
    monkeypatch.setenv(pricing.PRICING_FILE_ENV, str(path))
    pricing.reset_cache()
    monkeypatch.setattr(evals_engine, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    yield
    pricing.reset_cache()


@pytest.fixture
def initialized_db(tmp_path):
    return db.init_db(str(tmp_path / "cost.db"))


@pytest.fixture
def one_at_a_time(monkeypatch):
    """One run in flight, so which run the budget stops at is deterministic."""
    monkeypatch.setattr(evals_engine, "MAX_CONCURRENT_RUNS", 1)


def dollar_run(_request: RunRequest) -> FakeModel:
    """One agent turn of exactly 1M input tokens: $1.00 at the fake price."""
    return FakeModel(script=[Text("done")], usage_per_turn=(MILLION, 0))


def judge(_model_id: str, _provider: str = "bedrock") -> FakeJudgeModel:
    return FakeJudgeModel(model_id=FAKE_JUDGE, usage_per_turn=(1000, 100))


#: What one judge call costs at the fake judge price.
JUDGE_CALL_USD = (1000 * 2 + 100 * 10) / MILLION


def deps() -> evals_engine.EvalDeps:
    return evals_engine.EvalDeps(settings=Settings(), model_factory=dollar_run, judge_factory=judge)


def determinism(n: int, **fields: Any) -> EvaluationRequest:
    return EvaluationRequest.model_validate(
        {
            "kind": "determinism",
            "n": n,
            "run_config": {"model_id": FAKE_MODEL, "user_prompt": "hi"},
            "grader": {"model_id": FAKE_JUDGE},
            **fields,
        }
    )


def suite(case_count: int, **fields: Any) -> EvaluationRequest:
    return EvaluationRequest.model_validate(
        {
            "kind": "suite",
            "suite": {
                "run_config": {"model_id": FAKE_MODEL},
                "cases": [{"id": f"case-{i}", "input": f"q{i}"} for i in range(case_count)],
            },
            "grader": {"model_id": FAKE_JUDGE},
            **fields,
        }
    )


async def run(request: EvaluationRequest) -> tuple[dict[str, Any], Recorder]:
    recorder = Recorder()
    terminal = await evals_engine.execute_evaluation_with_seam(
        request, recorder.emit, RecordingStore("eval-cost"), recorder.cancelled, deps=deps()
    )
    return terminal, recorder


# --------------------------------------------------------------------------- #
# Cost per run
# --------------------------------------------------------------------------- #


async def test_a_run_records_its_estimated_cost_in_its_metrics(initialized_db):
    events = [
        event
        async for event in runner.execute_run(
            RunRequest(model_id=FAKE_MODEL, user_prompt="hi"), model_factory=dollar_run
        )
    ]
    metrics = next(event for event in events if isinstance(event, MetricsEvent))
    assert metrics.cost_usd == 1.0

    with Session(db.get_engine()) as session:
        stored = history.get_run(session, events[0].run_id)
    assert stored.metrics["cost_usd"] == 1.0


async def test_an_unpriced_run_records_an_unknown_cost_not_zero(initialized_db):
    events = [
        event
        async for event in runner.execute_run(
            RunRequest(model_id="some.unpriced-model", user_prompt="hi"),
            model_factory=dollar_run,
        )
    ]
    metrics = next(event for event in events if isinstance(event, MetricsEvent))
    assert metrics.cost_usd is None
    assert '"cost_usd":null' in metrics.to_json_line()


def test_prompt_cache_tokens_reach_the_metrics_event():
    class Result:
        class metrics:  # noqa: N801 - mimics the AgentResult attribute
            accumulated_usage = {
                "inputTokens": 5,
                "outputTokens": 6,
                "totalTokens": 11,
                "cacheReadInputTokens": 100,
                "cacheWriteInputTokens": 50,
            }
            accumulated_metrics = {"latencyMs": 9}
            cycle_count = 1

    [event] = EventMapper().map({"result": Result()})
    assert event.cache_read_input_tokens == 100
    assert event.cache_write_input_tokens == 50


# --------------------------------------------------------------------------- #
# Cost per evaluation
# --------------------------------------------------------------------------- #


async def test_an_evaluation_reports_runs_and_judge_separately(initialized_db):
    terminal, _ = await run(determinism(2))

    cost = terminal["result"]["cost"]
    assert cost["runs_usd"] == 2.0
    assert cost["judge_usd"] == pytest.approx(2 * JUDGE_CALL_USD)
    assert cost["total_usd"] == pytest.approx(2.0 + 2 * JUDGE_CALL_USD)
    assert cost["judge_tokens"]["input_tokens"] == 2000
    assert cost["estimate"] is True
    assert cost["pricing_as_of"] == pricing.PRICING_AS_OF
    assert cost["max_cost_usd"] is None
    assert terminal["result"]["budget_exhausted"] is False
    assert "skipped_runs" not in terminal["result"]


async def test_an_unpriced_model_under_test_makes_the_total_unknown(initialized_db):
    request = determinism(2)
    request.run_config = request.run_config.model_copy(update={"model_id": "unpriced.model"})
    terminal, _ = await run(request)

    cost = terminal["result"]["cost"]
    assert cost["runs_usd"] is None
    assert cost["judge_usd"] == pytest.approx(2 * JUDGE_CALL_USD)
    assert cost["total_usd"] is None


async def test_an_unpriced_judge_makes_the_total_unknown(initialized_db):
    request = determinism(2)
    request.grader = request.grader.model_copy(update={"model_id": "unpriced.judge"})
    terminal, _ = await run(request)

    cost = terminal["result"]["cost"]
    assert cost["runs_usd"] == 2.0
    assert cost["judge_usd"] is None
    assert cost["total_usd"] is None


async def test_grading_stored_runs_costs_only_the_judge(initialized_db):
    first, _ = await run(determinism(2))
    graded = EvaluationRequest(
        kind="grade", run_ids=first["run_ids"], grader={"model_id": FAKE_JUDGE}
    )
    terminal, _ = await run(graded)

    cost = terminal["result"]["cost"]
    assert cost["runs_usd"] == 0.0
    assert cost["judge_usd"] == pytest.approx(2 * JUDGE_CALL_USD)


async def test_throttled_attempts_still_count_toward_the_spend(initialized_db, monkeypatch):
    """A retried run's failed attempt consumed tokens too; the ledger counts them."""
    attempts: list[int] = []

    async def fake_once(job, _deps, _store=None):
        attempts.append(job.index)
        outcome = evals_engine.RunOutcome(index=job.index, cost_usd=0.25)
        if len(attempts) == 1:
            outcome.error = {"code": evals_engine.THROTTLE_ERROR_CODE, "message": "slow down"}
        else:
            outcome.status, outcome.run_id = "completed", f"run-{len(attempts)}"
        return outcome

    monkeypatch.setattr(evals_engine, "_execute_once", fake_once)
    outcome = await evals_engine._execute_with_retries(evals_engine._Job(0, None), deps())
    assert outcome.attempts == 2
    assert outcome.cost_usd == 0.5


# --------------------------------------------------------------------------- #
# Budgets
# --------------------------------------------------------------------------- #


async def test_a_budget_stops_scheduling_and_completes_with_partial_results(
    initialized_db, one_at_a_time
):
    """$2.50 buys two $1 runs: after two, the third is projected to $3.00 and refused."""
    terminal, recorder = await run(determinism(5, max_cost_usd=2.5))

    assert terminal["status"] == "completed"
    assert terminal["error"] is None
    assert len(terminal["run_ids"]) == 2
    result = terminal["result"]
    assert result["budget_exhausted"] is True
    assert result["skipped_runs"] == 3
    assert result["cost"]["runs_usd"] == 2.0
    assert result["cost"]["max_cost_usd"] == 2.5
    assert result["metrics"]["runs_analyzed"] == 2
    assert recorder.types.count("run_started") == 2
    assert recorder.types[-1] == "eval_complete"


async def test_a_budget_that_is_never_reached_changes_nothing(initialized_db, one_at_a_time):
    terminal, _ = await run(determinism(3, max_cost_usd=100))
    assert len(terminal["run_ids"]) == 3
    assert terminal["result"]["budget_exhausted"] is False


async def test_a_suite_marks_the_cases_the_budget_skipped(initialized_db, one_at_a_time):
    terminal, _ = await run(suite(3, max_cost_usd=1.5))

    assert terminal["status"] == "completed"
    cases = {case["id"]: case for case in terminal["result"]["cases"]}
    assert cases["case-0"]["status"] == "passed"
    for skipped in ("case-1", "case-2"):
        assert cases[skipped]["runs"]["total"] == 0
        assert cases[skipped]["error"]["code"] == "budget_exhausted"
    assert terminal["result"]["skipped_runs"] == 2


async def test_the_local_lane_stops_at_the_budget_and_persists_it(initialized_db, one_at_a_time):
    with Session(db.get_engine()) as session:
        record = history.create_evaluation(
            session, kind="determinism", run_ids=[], config={}, status="pending"
        )
    job = evals_jobs.EvalJob(record.id)
    await evals_engine.run_evaluation(job, record.id, determinism(4, max_cost_usd=1.5), deps())

    with Session(db.get_engine()) as session:
        stored = history.get_evaluation(session, record.id)
    assert stored.status == "completed"
    assert len(stored.run_ids) == 1
    assert stored.result["budget_exhausted"] is True
    assert stored.result["cost"]["runs_usd"] == 1.0


def test_a_budget_on_an_unpriced_model_is_refused_up_front():
    with pytest.raises(BadRequestError) as caught:
        EvaluationRequest.model_validate(
            {
                "kind": "determinism",
                "run_config": {"model_id": "unpriced.model", "user_prompt": "hi"},
                "max_cost_usd": 1,
            }
        )
    assert caught.value.code == "budget_model_unpriced"


def test_a_budget_on_a_grade_needs_no_price():
    request = EvaluationRequest(kind="grade", run_ids=["r1"], max_cost_usd=1)
    assert request.stored_config()["max_cost_usd"] == 1


@pytest.mark.parametrize("value", [0, -1])
def test_a_budget_must_be_positive(value):
    with pytest.raises(ValueError):
        determinism(2, max_cost_usd=value)


def test_the_ledger_counts_in_flight_runs_at_the_running_average():
    ledger = budget.CostLedger(max_cost_usd=4.5)
    assert [ledger.admit() for _ in range(3)] == [True, True, True]  # no estimate yet
    ledger.settle(1.0)
    # spent 1 + (2 in flight + 1 new) x $1 = $4 <= $4.50
    assert ledger.admit() is True
    # spent 1 + (3 in flight + 1 new) x $1 = $5 > $4.50
    assert ledger.admit() is False
    assert ledger.exhausted and ledger.skipped == 1
    ledger.settle(0.1)
    assert ledger.admit() is False  # refusing is permanent
    assert ledger.skipped == 2


def test_the_ledger_stops_once_the_budget_is_spent_even_by_free_runs():
    ledger = budget.CostLedger(max_cost_usd=1.0)
    assert ledger.admit()
    ledger.settle(1.0)
    ledger.settle(None)  # an unmeasured run counts as nothing
    assert ledger.spent == 1.0
    assert ledger.admit() is False


def test_a_ledger_without_a_budget_admits_everything():
    ledger = budget.CostLedger()
    for _ in range(50):
        assert ledger.admit()
        ledger.settle(100.0)
    assert not ledger.exhausted


def test_the_judge_meter_counts_an_instance_it_is_handed_twice_only_once():
    model = FakeJudgeModel(model_id=FAKE_JUDGE)
    meter = budget.JudgeMeter(lambda _model_id: model)
    assert meter.factory(FAKE_JUDGE) is model
    assert meter.factory(FAKE_JUDGE) is model
    meter._add({"inputTokens": 1})
    assert meter.usage["input_tokens"] == 1
    assert budget.JudgeMeter(judge).cost_usd() == 0.0  # never called: nothing spent


def test_a_judge_that_could_not_be_metered_has_an_unknown_cost():
    meter = budget.JudgeMeter(judge)
    meter.factory(FAKE_JUDGE)
    meter.metered = False
    assert meter.cost_usd() is None


# --------------------------------------------------------------------------- #
# The CLI
# --------------------------------------------------------------------------- #

CLI_SUITE = f"""\
run_config:
  model_id: {FAKE_MODEL}
max_cost_usd: 3
cases:
  - id: a
    input: hi
"""


def cli_request(tmp_path, *argv: str) -> EvaluationRequest:
    from nimbus.cli import commands
    from nimbus.cli.main import build_parser, prepare
    from tests.test_cli import _Stdin

    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(CLI_SUITE, encoding="utf-8")
    args = build_parser().parse_args([arg.replace("SUITE", str(suite_path)) for arg in argv])
    prepare(args, _Stdin("", tty=True))
    return commands.build_eval_request(args)


def test_a_suite_file_can_set_a_budget(tmp_path):
    request = cli_request(tmp_path, "eval", "--suite", "SUITE")
    assert request.max_cost_usd == 3


def test_max_cost_overrides_the_suite_files_budget(tmp_path):
    request = cli_request(tmp_path, "eval", "--suite", "SUITE", "--max-cost", "0.5")
    assert request.max_cost_usd == 0.5


def test_max_cost_bounds_a_determinism_experiment(tmp_path):
    request = cli_request(tmp_path, "eval", "-m", FAKE_MODEL, "-p", "hi", "--max-cost", "2")
    assert request.max_cost_usd == 2


def test_the_cli_summary_reports_the_cost_and_a_spent_budget():
    from nimbus.cli import render

    lines = render.eval_result_lines(
        {
            "grade": "A",
            "score": 95,
            "cost": {"total_usd": 12.5, "runs_usd": 12.0, "judge_usd": 0.5, "max_cost_usd": 12},
            "budget_exhausted": True,
            "skipped_runs": 3,
        }
    )
    assert "cost ~$12.50  (runs ~$12.00, judge ~$0.5000; estimate)" in lines[1]
    assert "budget of ~$12.00 reached: 3 runs not started" in lines[2]
    unknown = render.suite_result_lines({"cases": [], "cost": {"total_usd": None}})
    assert "cost unknown" in unknown[-1]
    assert render.cost_lines({}) == []


# --------------------------------------------------------------------------- #
# The cloud worker
# --------------------------------------------------------------------------- #


async def test_the_cloud_worker_stops_at_the_budget_and_completes(
    tmp_path, monkeypatch, one_at_a_time
):
    """The same stop, through the worker's own entry point and DynamoDB store."""
    from nimbus.store import repo as store_repo
    from nimbus.worker import lambda_app
    from nimbus.worker.ddb import DynamoEvalStore
    from tests.fake_dynamodb import FakeDynamoDBClient

    db.init_db(str(tmp_path / "worker.db"))
    store_repo.reset_cache()
    monkeypatch.setattr(evals_engine, "default_deps", lambda settings=None: deps())
    evaluation_id = "0123456789abcdef0123456789abcdef"
    request = determinism(4, max_cost_usd=2.5).model_dump(mode="json", exclude={"execution"})
    client = FakeDynamoDBClient()
    store = DynamoEvalStore("table", evaluation_id, client=client)
    store.begin(request)
    store.mark_running()

    try:
        status = await lambda_app.execute(evaluation_id, request, store, None)
    finally:
        store_repo.reset_cache()

    assert status == "completed"
    meta = client.item(f"EVAL#{evaluation_id}", "META")
    assert meta["status"] == {"S": "completed"}
    result = json.loads(meta["result"]["S"])
    assert result["budget_exhausted"] is True
    assert result["skipped_runs"] == 2
    assert result["cost"]["runs_usd"] == 2.0
    assert len(json.loads(meta["run_ids"]["S"])) == 2
