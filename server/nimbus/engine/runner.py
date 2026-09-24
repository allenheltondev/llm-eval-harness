"""Execute a single evaluation run through a Strands Agent.

:func:`execute_run` is an async generator of :mod:`nimbus.engine.events`
events -- one internal pipeline that the router presents either as an NDJSON
stream or as a single ``RunDetail`` JSON body.

Ordering guarantees
-------------------
1. Nothing is yielded until the toolset is resolved and the run row exists, so a
   caller that primes the generator once (see ``routers/runs.py``) can still turn
   a setup failure into an ordinary HTTP error envelope. Everything after
   ``run_start`` travels in-band as an ``error`` event on a 200 stream.
2. The run row is **fully** updated -- output, tool transcript, metrics,
   guardrail trace, terminal status -- before ``run_complete`` is emitted, so a
   client that fetches ``GET /runs/{id}`` the instant it sees ``run_complete``
   always observes the finished row.
3. Client disconnect (``GeneratorExit``) and task cancellation
   (``CancelledError``) persist status ``cancelled`` plus whatever accumulated,
   then re-raise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager
from typing import Any

from sqlmodel import Session
from strands import Agent
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent

from nimbus.config import Settings, get_settings
from nimbus.engine.events import (
    ErrorEvent,
    GuardrailTraceEvent,
    MetricsEvent,
    RunCompleteEvent,
    RunEvent,
    RunStartEvent,
    TextDeltaEvent,
    ToolResultEvent,
)
from nimbus.engine.mapper import EventMapper
from nimbus.engine.model_factory import ModelFactory, build_model, classify_error
from nimbus.engine.schemas import RunRequest
from nimbus.errors import BadRequestError
from nimbus.store.repo import (
    HistoryRepo,
    SqliteHistoryRepo,
    default_session_factory,
    get_history_repo,
)
from nimbus.tools.registry import get_tools, list_toolsets

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractContextManager[Session]]

#: Re-exported for callers that already imported it from here. The runner writes
#: through a :class:`~nimbus.store.repo.HistoryRepo`, which opens (and
#: closes) its own short session per write instead of borrowing the request's
#: ``Depends`` session: FastAPI tears those down when the endpoint function
#: returns, which for a streaming response is long before the body finishes.
__all__ = ["default_session_factory", "execute_run"]


class _ToolTranscriptRecorder:
    """Collects per-tool timing/input/output/error via the agent's tool hooks.

    ``ToolResultEvent`` is not a callback event, so it never reaches
    ``stream_async`` consumers; the ``Before/AfterToolCallEvent`` hooks are the
    supported way to observe a completed tool call, and they are also the only
    place the wall-clock duration is available.
    """

    def __init__(self) -> None:
        self.pending: list[dict[str, Any]] = []
        self._started: dict[str, float] = {}

    def register(self, agent: Agent) -> None:
        agent.add_hook(self._on_before, BeforeToolCallEvent)
        agent.add_hook(self._on_after, AfterToolCallEvent)

    def drain(self) -> list[dict[str, Any]]:
        drained, self.pending = self.pending, []
        return drained

    def _on_before(self, event: BeforeToolCallEvent) -> None:
        self._started[event.tool_use.get("toolUseId", "")] = time.perf_counter()

    def _on_after(self, event: AfterToolCallEvent) -> None:
        tool_use = event.tool_use or {}
        tool_use_id = tool_use.get("toolUseId", "")
        started = self._started.pop(tool_use_id, None)
        result = event.result or {}
        self.pending.append(
            {
                "tool_use_id": tool_use_id,
                "name": tool_use.get("name", ""),
                "input": tool_use.get("input") or {},
                "output": _tool_output(result),
                "duration_ms": int((time.perf_counter() - started) * 1000) if started else 0,
                "error": _tool_error(result, event.exception),
            }
        )


def _tool_output(result: dict[str, Any]) -> Any:
    """Unwrap a Strands ``ToolResult`` content list into a plain value.

    ``@tool`` functions returning a dict are JSON-encoded into a single ``text``
    block, so text blocks are decoded back to JSON when they parse.
    """
    blocks = result.get("content") or []
    values: list[Any] = []
    for block in blocks:
        if "json" in block:
            values.append(block["json"])
        elif "text" in block:
            try:
                values.append(json.loads(block["text"]))
            except (TypeError, ValueError):
                values.append(block["text"])
        else:
            values.append(block)
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def _tool_error(result: dict[str, Any], exception: BaseException | None) -> dict[str, Any] | None:
    if exception is not None:
        return {"type": exception.__class__.__name__, "message": str(exception)}
    if result.get("status") == "error":
        return {"type": "tool_error", "message": _stringify(_tool_output(result))}
    return None


def _stringify(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


async def execute_run(
    request: RunRequest,
    session_factory: SessionFactory | None = None,
    settings: Settings | None = None,
    *,
    model_factory: ModelFactory | None = None,
    repo: HistoryRepo | None = None,
) -> AsyncIterator[RunEvent]:
    """Run ``request`` through a Strands Agent, yielding the run's event stream.

    The run row is written through ``repo`` — the history backend these settings
    resolve to (SQLite locally, DynamoDB in a deployed server). ``session_factory``
    is the older, SQLite-only form of the same injection and still wins when it
    is passed, so a caller can point the store at a specific database file.
    """
    resolved_settings = settings or get_settings()
    if repo is None:
        repo = (
            SqliteHistoryRepo(session_factory)
            if session_factory is not None
            else get_history_repo(resolved_settings)
        )
    make_model: ModelFactory = model_factory or (lambda req: build_model(req, resolved_settings))

    # --- setup: anything that fails here is a plain HTTP error ----------- #
    tools = get_tools(request.toolset) if request.toolset else []
    if request.toolset and not tools:
        raise BadRequestError(
            f"Unknown toolset {request.toolset!r}",
            detail={"toolset": request.toolset, "available": list_toolsets()},
            code="unknown_toolset",
        )

    record = repo.create_run(
        model_id=request.model_id,
        system_prompt=request.system_prompt,
        user_prompt=request.user_prompt,
        config=request.stored_config(),
        status="running",
    )
    run_id = record.id

    yield RunStartEvent(run_id=run_id, model_id=request.model_id, ts=record.ts)

    # --- execution: everything below reports in-band --------------------- #
    mapper = EventMapper()
    recorder = _ToolTranscriptRecorder()
    text_parts: list[str] = []
    transcript: list[dict[str, Any]] = []
    assessments: list[dict[str, Any]] = []
    metrics: dict[str, Any] | None = None
    status = "completed"
    error_payload: dict[str, Any] | None = None
    error_event: ErrorEvent | None = None

    def _accumulate(event: RunEvent) -> None:
        nonlocal metrics
        match event:
            case TextDeltaEvent():
                text_parts.append(event.text)
            case MetricsEvent():
                metrics = event.model_dump(exclude={"type"})
            case GuardrailTraceEvent():
                assessments.append(event.assessment)
            case _:
                pass

    def _persist() -> None:
        repo.update_run(
            run_id,
            output="".join(text_parts),
            tool_transcript=transcript or None,
            metrics=metrics,
            guardrail_trace=assessments or None,
            status=status,
            error=error_payload,
        )

    try:
        model = make_model(request)
        agent = Agent(
            model=model,
            tools=tools,
            system_prompt=request.system_prompt or None,
            callback_handler=None,
            # Strands' default retry strategy silently swallows throttling for up to
            # ~2 minutes of backoff, which for a live NDJSON stream means a stalled
            # run with no output. The engine's contract is the opposite: report
            # throttling immediately as an ``error`` event with retryable=true and
            # let the caller decide when to retry.
            retry_strategy=None,
        )
        recorder.register(agent)

        # max_tool_iterations is enforced by Strands itself: ``limits={"turns": n}``
        # caps agent loop iterations (one model call + its tool executions), and the
        # loop stops gracefully with stop_reason "limit_turns" rather than raising.
        stream = agent.stream_async(
            request.user_prompt, limits={"turns": request.max_tool_iterations}
        )

        async for raw_event in stream:
            for event in mapper.map(raw_event):
                _accumulate(event)
                yield event
            # Tool calls run *between* agent events; drain what the hooks recorded
            # so a tool_result lands as soon as the tool has actually finished.
            for tool_record in recorder.drain():
                transcript.append(tool_record)
                yield ToolResultEvent(**tool_record)

        for tool_record in recorder.drain():
            transcript.append(tool_record)
            yield ToolResultEvent(**tool_record)

    except (asyncio.CancelledError, GeneratorExit):
        status = "cancelled"
        logger.info("run %s cancelled by client disconnect", run_id)
        # Tools that finished but whose result never got yielded still belong on
        # the row -- "persist whatever accumulated".
        transcript.extend(recorder.drain())
        _persist()
        raise

    except Exception as exc:  # every failure is reported in-band, never raised
        code, message, retryable = classify_error(exc)
        logger.warning("run %s failed | code=%s | %s", run_id, code, message, exc_info=True)
        transcript.extend(recorder.drain())
        status = "error"
        error_payload = {"code": code, "message": message, "retryable": retryable}
        error_event = ErrorEvent(code=code, message=message, retryable=retryable)

    if mapper.unknown_count:
        logger.info("run %s: %d unmapped strands events", run_id, mapper.unknown_count)

    _persist()

    if error_event is not None:
        yield error_event
    yield RunCompleteEvent(run_id=run_id, status=status, final_text="".join(text_parts))
