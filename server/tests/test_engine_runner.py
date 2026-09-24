"""Tests for the run engine (nimbus.engine.runner).

Everything runs against the scripted ``FakeModel`` driven through a *real*
``strands.Agent`` with the real ported tools -- no AWS calls anywhere.
"""

import asyncio
import json
from datetime import datetime

import pytest
from botocore.exceptions import ClientError
from sqlmodel import Session
from strands.types.exceptions import ModelThrottledException

from nimbus.config import Settings
from nimbus.engine import runner
from nimbus.engine.events import (
    ErrorEvent,
    MetricsEvent,
    RunCompleteEvent,
    RunStartEvent,
    TextDeltaEvent,
    ToolInputDeltaEvent,
    ToolResultEvent,
    ToolUseStartEvent,
)
from nimbus.engine.fake_model import (
    Error,
    FakeModel,
    GuardrailTrace,
    Reasoning,
    Text,
    ToolUseStep,
)
from nimbus.engine.model_factory import build_model, classify_error
from nimbus.engine.schemas import RunRequest
from nimbus.errors import BadRequestError
from nimbus.store import db, history

FREEZE_INPUT = {
    "account_id": "A1234",
    "transaction_ids": ["T1", "T2"],
    "reason": "velocity spike",
    "severity": "high",
    "freeze_duration": "temporary",
}

FRAUD_SCRIPT = [
    Text("Hello "),
    Text("world"),
    ToolUseStep("freeze_account", FREEZE_INPUT),
    Text(" done"),
]


@pytest.fixture
def initialized_db(tmp_path):
    return db.init_db(str(tmp_path / "engine.db"))


@pytest.fixture
def stored_run(initialized_db):
    def _get(run_id: str):
        with Session(db.get_engine()) as session:
            return history.get_run(session, run_id)

    return _get


def make_request(**overrides) -> RunRequest:
    payload = {"model_id": "anthropic.claude-3-sonnet", "user_prompt": "hi"}
    payload.update(overrides)
    return RunRequest(**payload)


async def collect(request: RunRequest, model: FakeModel, **kwargs) -> list:
    return [
        event
        async for event in runner.execute_run(request, model_factory=lambda _r: model, **kwargs)
    ]


def types_of(events) -> list[str]:
    return [event.type for event in events]


# --------------------------------------------------------------------------- #
# Happy path: full tool round trip
# --------------------------------------------------------------------------- #


async def test_tool_round_trip_event_sequence_and_persistence(initialized_db, stored_run):
    model = FakeModel(script=FRAUD_SCRIPT)
    request = make_request(toolset="fraud-detection")

    events = await collect(request, model)
    sequence = types_of(events)

    assert sequence[0] == "run_start"
    assert sequence[-1] == "run_complete"
    assert sequence[-2] == "metrics"

    assert sequence.count("text_delta") >= 2
    assert sequence.index("tool_use_start") > sequence.index("text_delta")
    assert sequence.index("tool_result") > sequence.index("tool_use_start")
    # ... and more text after the tool result (the model's second turn)
    last_text = max(index for index, kind in enumerate(sequence) if kind == "text_delta")
    assert last_text > sequence.index("tool_result")

    start = events[0]
    assert isinstance(start, RunStartEvent)
    assert start.model_id == "anthropic.claude-3-sonnet"

    tool_use_start = next(e for e in events if isinstance(e, ToolUseStartEvent))
    tool_result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert tool_use_start.name == "freeze_account"
    assert tool_result.tool_use_id == tool_use_start.tool_use_id
    assert tool_result.input == FREEZE_INPUT
    assert tool_result.error is None
    assert tool_result.duration_ms >= 0
    # Real tool output, straight from the fraud-detection toolset.
    assert tool_result.output["status"] == "frozen"
    assert tool_result.output["affected_transactions"] == 2

    metrics = next(e for e in events if isinstance(e, MetricsEvent))
    assert metrics.total_tokens == 36
    assert metrics.cycle_count == 2

    complete = events[-1]
    assert isinstance(complete, RunCompleteEvent)
    assert complete.status == "completed"
    assert complete.final_text == "Hello world done"

    row = stored_run(start.run_id)
    assert row.status == "completed"
    assert row.output == "Hello world done"
    assert len(row.tool_transcript) == 1
    assert row.tool_transcript[0]["name"] == "freeze_account"
    assert row.tool_transcript[0]["tool_use_id"] == tool_use_start.tool_use_id
    assert row.metrics["total_tokens"] == 36
    assert row.error is None


async def test_tool_input_deltas_reassemble_into_the_tool_input(initialized_db):
    model = FakeModel(script=FRAUD_SCRIPT)
    request = make_request(toolset="fraud-detection")

    events = await collect(request, model)

    partials = [e for e in events if e.type == "tool_input_delta"]
    assert len(partials) >= 2
    assert json.loads("".join(p.json_text for p in partials)) == FREEZE_INPUT


async def test_message_events_carry_the_full_transcript(initialized_db):
    model = FakeModel(script=FRAUD_SCRIPT)
    request = make_request(toolset="fraud-detection")

    events = await collect(request, model)

    messages = [e for e in events if e.type == "message"]
    assert [m.role for m in messages] == ["assistant", "user", "assistant"]
    assert any("toolResult" in block for block in messages[1].content)


async def test_run_row_is_created_before_the_model_is_invoked(initialized_db, stored_run):
    model = FakeModel(script=[Text("hi")])
    request = make_request()

    events = runner.execute_run(request, model_factory=lambda _r: model)
    start = await anext(events)

    assert stored_run(start.run_id).status == "running"
    assert model.calls == []

    async for _event in events:
        pass


async def test_config_records_inference_tools_guardrail_and_stream(initialized_db, stored_run):
    model = FakeModel(script=[Text("hi")])
    request = make_request(
        inference={"temperature": 0.2, "max_tokens": 512},
        toolset="fraud-detection",
        guardrail={"id": "gr-1", "version": "2"},
        stream=False,
        max_tool_iterations=3,
    )

    events = await collect(request, model)

    row = stored_run(events[0].run_id)
    assert row.config == {
        "provider": "bedrock",
        "inference": {"temperature": 0.2, "max_tokens": 512},
        "toolset": "fraud-detection",
        "max_tool_iterations": 3,
        "guardrail": {"id": "gr-1", "version": "2", "trace": True},
        "stream": False,
    }


async def test_tools_are_only_registered_when_enabled(initialized_db):
    model = FakeModel(script=[Text("hi")])
    request = make_request()

    await collect(request, model)

    assert model.calls[0].tool_specs is None


async def test_tools_are_registered_when_enabled(initialized_db):
    model = FakeModel(script=[Text("hi")])
    request = make_request(toolset="fraud-detection")

    await collect(request, model)

    names = {spec["name"] for spec in model.calls[0].tool_specs}
    assert "freeze_account" in names


# --------------------------------------------------------------------------- #
# Persistence ordering
# --------------------------------------------------------------------------- #


async def test_row_is_complete_at_the_moment_run_complete_arrives(initialized_db, stored_run):
    model = FakeModel(script=FRAUD_SCRIPT)
    request = make_request(toolset="fraud-detection")

    observed = None
    async for event in runner.execute_run(request, model_factory=lambda _r: model):
        if isinstance(event, RunCompleteEvent):
            observed = stored_run(event.run_id)

    assert observed is not None
    assert observed.status == "completed"
    assert observed.output == "Hello world done"
    assert observed.metrics is not None
    assert len(observed.tool_transcript) == 1


async def test_row_is_still_running_just_before_completion(initialized_db, stored_run):
    model = FakeModel(script=[Text("a"), Text("b")])
    request = make_request()

    statuses = []
    async for event in runner.execute_run(request, model_factory=lambda _r: model):
        if isinstance(event, TextDeltaEvent):
            statuses.append(stored_run(_only_run_id(initialized_db)).status)

    assert statuses == ["running", "running"]


def _only_run_id(_engine) -> str:
    with Session(db.get_engine()) as session:
        rows, _cursor = history.list_runs(session)
    return rows[0].id


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


async def test_error_mid_stream_persists_and_reports_in_band(initialized_db, stored_run):
    model = FakeModel(script=[Text("partial "), Error(RuntimeError("kaboom"))])
    request = make_request()

    events = await collect(request, model)

    assert types_of(events) == ["run_start", "text_delta", "error", "run_complete"]
    error = events[-2]
    assert isinstance(error, ErrorEvent)
    assert error.code == "internal_error"
    assert "kaboom" in error.message
    assert error.retryable is False

    complete = events[-1]
    assert complete.status == "error"
    assert complete.final_text == "partial "

    row = stored_run(events[0].run_id)
    assert row.status == "error"
    assert row.output == "partial "
    assert row.error == {"code": "internal_error", "message": error.message, "retryable": False}


async def test_throttling_is_reported_as_retryable(initialized_db, stored_run):
    model = FakeModel(script=[Error(ModelThrottledException("slow down"))])
    request = make_request()

    events = await collect(request, model)

    error = events[-2]
    assert error.code == "model_throttled"
    assert error.retryable is True
    assert stored_run(events[0].run_id).error["retryable"] is True
    # Reported immediately rather than swallowed by minutes of strands-internal
    # backoff: the model is invoked exactly once.
    assert len(model.calls) == 1


async def test_model_construction_failure_is_reported_in_band(initialized_db, stored_run):
    def explode(_request):
        raise ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "too many requests"}},
            "Converse",
        )

    events = [
        event
        async for event in runner.execute_run(make_request(), model_factory=explode)
    ]

    assert types_of(events) == ["run_start", "error", "run_complete"]
    assert events[1].code == "model_throttled"
    assert events[1].retryable is True
    assert stored_run(events[0].run_id).status == "error"


def test_classify_error_covers_the_bedrock_failure_modes():
    assert classify_error(ModelThrottledException("x")) == ("model_throttled", "x", True)

    code, _message, retryable = classify_error(
        ClientError({"Error": {"Code": "ServiceUnavailableException"}}, "Converse")
    )
    assert (code, retryable) == ("model_error", True)

    code, _message, retryable = classify_error(
        ClientError({"Error": {"Code": "AccessDeniedException"}}, "Converse")
    )
    assert (code, retryable) == ("model_error", False)

    code, _message, retryable = classify_error(ValueError("nope"))
    assert (code, retryable) == ("internal_error", False)


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #


async def test_generator_close_mid_stream_persists_cancelled(initialized_db, stored_run):
    model = FakeModel(script=[Text("a"), Text("b"), Text("c")])
    events = runner.execute_run(make_request(), model_factory=lambda _r: model)

    start = await anext(events)
    await anext(events)  # first text delta
    await events.aclose()  # what a client disconnect looks like to the generator

    row = stored_run(start.run_id)
    assert row.status == "cancelled"
    assert row.output == "a"


async def test_task_cancellation_mid_stream_persists_cancelled(initialized_db, stored_run):
    model = FakeModel(script=[Text("a"), Text("b"), Text("c")])
    events = runner.execute_run(make_request(), model_factory=lambda _r: model)
    started = asyncio.Event()
    seen: list = []

    async def drain():
        async for event in events:
            seen.append(event)
            if isinstance(event, TextDeltaEvent):
                started.set()
                await asyncio.sleep(3600)

    task = asyncio.create_task(drain())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await events.aclose()

    row = stored_run(seen[0].run_id)
    assert row.status == "cancelled"


# --------------------------------------------------------------------------- #
# Reasoning, guardrails, iteration limits
# --------------------------------------------------------------------------- #


async def test_reasoning_deltas_are_emitted(initialized_db):
    model = FakeModel(script=[Reasoning("let me think"), Text("answer")])

    events = await collect(make_request(), model)

    reasoning = [e for e in events if e.type == "reasoning_delta"]
    assert [e.text for e in reasoning] == ["let me think"]
    assert events[-1].final_text == "answer"


async def test_guardrail_trace_is_emitted_and_persisted(initialized_db, stored_run):
    assessment = {"inputAssessment": {"gr-1": {"contentPolicy": {"filters": []}}}}
    model = FakeModel(script=[Text("hi"), GuardrailTrace(assessment)])

    events = await collect(make_request(), model)

    traces = [e for e in events if e.type == "guardrail_trace"]
    assert [t.assessment for t in traces] == [assessment]
    assert stored_run(events[0].run_id).guardrail_trace == [assessment]


async def test_max_tool_iterations_caps_the_agent_loop(initialized_db):
    model = FakeModel(
        script=[
            ToolUseStep("freeze_account", FREEZE_INPUT),
            Text("this second turn never happens"),
        ]
    )
    request = make_request(
        toolset="fraud-detection", max_tool_iterations=1
    )

    events = await collect(request, model)

    assert len(model.calls) == 1
    assert events[-1].final_text == ""
    assert "tool_result" in types_of(events)


async def test_higher_iteration_budget_allows_the_second_turn(initialized_db):
    model = FakeModel(
        script=[ToolUseStep("freeze_account", FREEZE_INPUT), Text("wrapped up")]
    )
    request = make_request(
        toolset="fraud-detection", max_tool_iterations=2
    )

    events = await collect(request, model)

    assert len(model.calls) == 2
    assert events[-1].final_text == "wrapped up"


async def test_failing_tool_is_recorded_with_its_error_shape(initialized_db, stored_run):
    # Missing required arguments make the real tool fail validation.
    model = FakeModel(
        script=[ToolUseStep("freeze_account", {"account_id": "A1234"}), Text("sorry")]
    )
    request = make_request(toolset="fraud-detection")

    events = await collect(request, model)

    tool_result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert tool_result.name == "freeze_account"
    assert tool_result.error is not None
    transcript = stored_run(events[0].run_id).tool_transcript[0]
    assert transcript["input"] == {"account_id": "A1234"}
    assert transcript["error"] is not None


async def test_unknown_toolset_is_a_bad_request_before_any_row_exists(initialized_db):
    request = make_request(toolset="does-not-exist")

    with pytest.raises(BadRequestError) as excinfo:
        await collect(request, FakeModel(script=[Text("ok")]))

    assert excinfo.value.code == "unknown_toolset"
    assert excinfo.value.detail == {"toolset": "does-not-exist", "available": ["fraud-detection"]}


# --------------------------------------------------------------------------- #
# Model construction
# --------------------------------------------------------------------------- #


def test_build_model_returns_the_fake_model_when_configured():
    settings = Settings(fake_model=True)

    model = build_model(make_request(inference={"temperature": 0.3}), settings)

    assert isinstance(model, FakeModel)
    assert model.get_config()["temperature"] == 0.3


async def test_fake_model_settings_produce_a_visible_canned_answer(initialized_db):
    settings = Settings(fake_model=True)
    request = make_request(user_prompt="hello")

    events = [
        event async for event in runner.execute_run(request, settings=settings)
    ]

    assert events[-1].status == "completed"
    assert events[-1].final_text.startswith("[fake-model] ")


def test_to_json_line_applies_field_aliases():
    """``by_alias=True`` -- ``ToolInputDeltaEvent.json_text`` must serialize
    under its wire alias ``json``, not its Python attribute name."""
    event = ToolInputDeltaEvent(tool_use_id="tu-1", json_text='{"a": 1}')

    line = event.to_json_line()

    assert line.endswith("\n")
    payload = json.loads(line)
    assert payload["json"] == '{"a": 1}'
    assert "json_text" not in payload


async def test_run_start_timestamp_is_a_utc_iso8601_instant(initialized_db):
    events = await collect(make_request(), FakeModel(script=[Text("ok")]))

    serialized = json.loads(events[0].to_json_line())
    assert serialized["ts"].endswith("Z")
    assert datetime.fromisoformat(serialized["ts"]).tzinfo is not None


async def test_a_custom_session_factory_is_used_for_persistence(initialized_db, stored_run):
    opened: list[int] = []

    def factory():
        opened.append(1)
        return Session(db.get_engine())

    events = await collect(make_request(), FakeModel(script=[Text("ok")]), session_factory=factory)

    # One session to create the row, one to finalize it.
    assert len(opened) == 2
    assert stored_run(events[0].run_id).status == "completed"


# --------------------------------------------------------------------------- #
# Tool-result content-block unwrapping (nimbus.engine.runner._tool_output
# / _tool_error / _stringify): the mapping from a Strands ToolResult's content
# blocks onto the flat value the transcript actually stores.
# --------------------------------------------------------------------------- #


def test_tool_output_prefers_a_json_content_block():
    assert runner._tool_output({"content": [{"json": {"a": 1}}]}) == {"a": 1}


def test_tool_output_decodes_a_json_looking_text_block():
    assert runner._tool_output({"content": [{"text": '{"b": 2}'}]}) == {"b": 2}


def test_tool_output_keeps_non_json_text_verbatim():
    assert runner._tool_output({"content": [{"text": "plain text, not json"}]}) == (
        "plain text, not json"
    )


def test_tool_output_falls_back_to_the_raw_block_for_unknown_shapes():
    block = {"unknownField": "whatever"}
    assert runner._tool_output({"content": [block]}) == block


def test_tool_output_of_no_content_blocks_is_none():
    assert runner._tool_output({"content": []}) is None
    assert runner._tool_output({}) is None


def test_tool_output_of_multiple_blocks_is_a_list():
    result = runner._tool_output({"content": [{"json": 1}, {"json": 2}]})
    assert result == [1, 2]


def test_tool_error_prefers_the_raised_exception():
    exc = ValueError("bad input")
    error = runner._tool_error({"status": "success"}, exc)
    assert error == {"type": "ValueError", "message": "bad input"}


def test_tool_error_reads_an_error_status_result_when_no_exception():
    result = {"status": "error", "content": [{"text": "not found"}]}
    error = runner._tool_error(result, None)
    assert error == {"type": "tool_error", "message": "not found"}


def test_tool_error_is_none_on_a_successful_result():
    assert runner._tool_error({"status": "success", "content": []}, None) is None


def test_stringify_passes_strings_through_and_json_encodes_everything_else():
    assert runner._stringify("already a string") == "already a string"
    assert runner._stringify({"a": 1}) == '{"a": 1}'
    assert runner._stringify([1, 2, 3]) == "[1, 2, 3]"


def test_build_model_builds_a_bedrock_model_without_calling_aws(monkeypatch):
    captured = {}

    class _StubBedrockModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import strands.models.bedrock as bedrock_module

    monkeypatch.setattr(bedrock_module, "BedrockModel", _StubBedrockModel)

    request = make_request(
        inference={"temperature": 0.1, "top_p": 0.5, "max_tokens": 100},
        guardrail={"id": "gr-7", "version": "3", "trace": False},
    )
    build_model(request, Settings(fake_model=False, aws_region="us-west-2"))

    assert captured == {
        "region_name": "us-west-2",
        "model_id": "anthropic.claude-3-sonnet",
        "streaming": True,
        "temperature": 0.1,
        "top_p": 0.5,
        "max_tokens": 100,
        "guardrail_id": "gr-7",
        "guardrail_version": "3",
        "guardrail_trace": "disabled",
    }
