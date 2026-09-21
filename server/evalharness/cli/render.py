"""Turning engine and evaluation events into terminal output.

Two streams, always, and the split is the whole design:

**stdout carries the product** -- the model's text, a listing, a JSON document.
**stderr carries the progress** -- run ids, tool calls, token counts, grades.

That is what makes the obvious pipeline do the obvious thing::

    evalharness run -m <model> -p 'summarise this' > summary.txt

``summary.txt`` holds the answer and nothing else, while the operator still
watches tool calls and token counts scroll past on the terminal. ``--json``
moves the raw NDJSON events onto stdout instead, unchanged from what the API
streams, which is the seam every wrapper (scripts, an MCP server) should read.

Everything here is a pure function from one event to at most one line, so the
rendering is tested directly rather than through a captured terminal.
"""

from __future__ import annotations

import json
from typing import Any

from evalharness.engine.events import (
    ErrorEvent,
    GuardrailTraceEvent,
    MetricsEvent,
    RunCompleteEvent,
    RunEvent,
    RunStartEvent,
    ToolResultEvent,
)

#: Prefix on every progress line, so stderr from a run is visually distinct
#: from whatever else is sharing the terminal and is trivial to grep away.
MARKER = "|"


def _line(text: str) -> str:
    return f"{MARKER} {text}"


def _count(value: int) -> str:
    """Thousands-separated, because six-figure token counts are unreadable raw."""
    return f"{value:,}"


def _duration(milliseconds: int) -> str:
    """``840ms`` under a second, ``12.4s`` above it."""
    if milliseconds < 1000:
        return f"{milliseconds}ms"
    return f"{milliseconds / 1000:.1f}s"


def _one_line(text: object, limit: int) -> str:
    """Collapse to a single line, then truncate.

    Judge reasoning arrives as a multi-line block. Printed raw it breaks out of
    the progress column and every line after the first loses its marker, so the
    summary stops being one grep-able line per fact.
    """
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def run_progress_line(event: RunEvent) -> str | None:
    """The stderr progress line for one run event, or ``None`` if it has none.

    Text deltas deliberately return ``None``: they are the product and belong
    on stdout, which is the caller's job, not this function's.
    """
    match event:
        case RunStartEvent():
            return _line(f"run {event.run_id}  model={event.model_id}")
        case ToolResultEvent():
            return _line(_tool_summary(event))
        case MetricsEvent():
            return _line(
                f"tokens in={_count(event.input_tokens)} "
                f"out={_count(event.output_tokens)} "
                f"total={_count(event.total_tokens)}  "
                f"latency={_duration(event.latency_ms)}  "
                f"cycles={event.cycle_count}"
            )
        case GuardrailTraceEvent():
            return _line("guardrail assessment recorded")
        case ErrorEvent():
            retryable = "  (retryable)" if event.retryable else ""
            return _line(f"error [{event.code}] {event.message}{retryable}")
        case RunCompleteEvent():
            return _line(f"{event.status}  run={event.run_id}")
        case _:
            return None


def _tool_summary(event: ToolResultEvent) -> str:
    """``tool <name> (<duration>)``, or the failure when the call errored."""
    head = f"tool {event.name} ({_duration(event.duration_ms)})"
    if event.error:
        return f"{head} failed: {event.error.get('message', 'unknown error')}"
    return head


def eval_progress_line(entry: dict[str, Any]) -> str | None:
    """The stderr progress line for one evaluation event.

    Takes the plain dict the engine's emitter seam publishes -- the same shape
    that goes to DynamoDB and down the NDJSON stream -- rather than a model, so
    the CLI reads exactly what every other consumer reads.
    """
    match entry.get("type"):
        case "eval_start":
            return _line(
                f"evaluation {entry.get('evaluation_id', '')}  "
                f"kind={entry.get('kind', '')}  n={entry.get('n', 0)}"
            )
        case "run_started":
            return _line(f"run {entry.get('index', 0)} started")
        case "run_completed":
            return _line(_repeat_summary(entry))
        case "run_failed":
            return _line(f"run {entry.get('index', 0)} failed: {_error_text(entry.get('error'))}")
        case "grading_started":
            return _line("grading")
        case "eval_complete":
            return _line(str(entry.get("status", "")))
        case _:
            return None


def _repeat_summary(entry: dict[str, Any]) -> str:
    summary = entry.get("summary") or {}
    return (
        f"run {entry.get('index', 0)} {entry.get('status', '')}  "
        f"id={entry.get('run_id', '')}  "
        f"{_count(summary.get('output_chars', 0))} chars, "
        f"{summary.get('tool_calls', 0)} tools, "
        f"{_duration(summary.get('duration_ms', 0))}"
    )


def _error_text(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error)
    return str(error)


def eval_result_lines(result: dict[str, Any] | None) -> list[str]:
    """The grade summary printed to stderr once an evaluation settles.

    The machine-readable result still goes to stdout as JSON; this is the
    human's two-line version of it.
    """
    if not result:
        return []
    lines = [_line(f"grade {result.get('grade', '?')}  score={result.get('score', '?')}")]
    reasoning = result.get("reasoning")
    if reasoning:
        lines.append(_line(f"       {_one_line(reasoning, 160)}"))
    return lines


def table(rows: list[list[str]], headers: list[str]) -> str:
    """A plain column-aligned table, no borders -- greppable and cut-friendly.

    Returns the empty string for no rows so a caller can print a "nothing here"
    line instead of a lone header.
    """
    if not rows:
        return ""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    # The last column is never padded: trailing whitespace is noise in a pipe.
    def render(cells: list[str]) -> str:
        return "  ".join(
            cell.ljust(widths[index]) if index < len(cells) - 1 else cell
            for index, cell in enumerate(cells)
        )

    return "\n".join([render(headers)] + [render(row) for row in rows])


def dumps(value: Any) -> str:
    """Pretty JSON for stdout, with non-JSON values (datetimes) stringified."""
    return json.dumps(value, indent=2, default=str)
