"""Tests for the CLI's terminal rendering (evalharness.cli.render).

Every function here is pure -- one event in, at most one line out -- so these
assert on the exact strings rather than on a captured terminal. The strings are
the user interface, and a silently reworded progress line is a real change.
"""

from datetime import UTC, datetime

from evalharness.cli import render
from evalharness.engine.events import (
    ErrorEvent,
    GuardrailTraceEvent,
    MessageEvent,
    MetricsEvent,
    RunCompleteEvent,
    RunStartEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseStartEvent,
)

TS = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


class TestRunProgressLine:
    def test_run_start_names_the_run_and_the_model(self):
        event = RunStartEvent(run_id="r1", model_id="anthropic.claude", ts=TS)
        assert render.run_progress_line(event) == "| run r1  model=anthropic.claude"

    def test_text_deltas_have_no_progress_line(self):
        """Text is the product; it belongs on stdout, not in the commentary."""
        assert render.run_progress_line(TextDeltaEvent(text="hello")) is None

    def test_events_without_a_rendering_are_silent(self):
        assert render.run_progress_line(MessageEvent(role="assistant")) is None
        assert render.run_progress_line(ToolUseStartEvent(tool_use_id="t1", name="x")) is None

    def test_tool_result_reports_name_and_duration(self):
        event = ToolResultEvent(tool_use_id="t1", name="freeze_account", duration_ms=840)
        assert render.run_progress_line(event) == "| tool freeze_account (840ms)"

    def test_tool_result_switches_to_seconds_past_a_second(self):
        event = ToolResultEvent(tool_use_id="t1", name="slow", duration_ms=12400)
        assert render.run_progress_line(event) == "| tool slow (12.4s)"

    def test_a_failed_tool_says_why(self):
        event = ToolResultEvent(
            tool_use_id="t1",
            name="freeze_account",
            duration_ms=10,
            error={"type": "ValueError", "message": "no such account"},
        )
        assert render.run_progress_line(event) == (
            "| tool freeze_account (10ms) failed: no such account"
        )

    def test_a_failed_tool_with_no_message_still_renders(self):
        event = ToolResultEvent(tool_use_id="t1", name="x", duration_ms=1, error={"type": "E"})
        assert render.run_progress_line(event) == "| tool x (1ms) failed: unknown error"

    def test_metrics_are_thousands_separated(self):
        event = MetricsEvent(
            input_tokens=1234,
            output_tokens=56789,
            total_tokens=58023,
            latency_ms=2300,
            cycle_count=3,
        )
        assert render.run_progress_line(event) == (
            "| tokens in=1,234 out=56,789 total=58,023  latency=2.3s  cycles=3"
        )

    def test_guardrail_trace_is_announced_without_dumping_the_assessment(self):
        event = GuardrailTraceEvent(assessment={"topicPolicy": {"topics": ["x"]}})
        assert render.run_progress_line(event) == "| guardrail assessment recorded"

    def test_error_reports_code_and_message(self):
        event = ErrorEvent(code="model_throttled", message="slow down")
        assert render.run_progress_line(event) == "| error [model_throttled] slow down"

    def test_a_retryable_error_says_so(self):
        event = ErrorEvent(code="model_throttled", message="slow down", retryable=True)
        assert render.run_progress_line(event).endswith("slow down  (retryable)")

    def test_run_complete_reports_the_status(self):
        event = RunCompleteEvent(run_id="r1", status="cancelled")
        assert render.run_progress_line(event) == "| cancelled  run=r1"


class TestEvalProgressLine:
    def test_eval_start(self):
        entry = {"type": "eval_start", "evaluation_id": "e1", "kind": "determinism", "n": 5}
        assert render.eval_progress_line(entry) == "| evaluation e1  kind=determinism  n=5"

    def test_run_started(self):
        assert render.eval_progress_line({"type": "run_started", "index": 2}) == "| run 2 started"

    def test_run_completed_summarizes_the_repeat(self):
        entry = {
            "type": "run_completed",
            "index": 1,
            "run_id": "r9",
            "status": "completed",
            "summary": {"output_chars": 4200, "tool_calls": 3, "duration_ms": 1500},
        }
        assert render.eval_progress_line(entry) == (
            "| run 1 completed  id=r9  4,200 chars, 3 tools, 1.5s"
        )

    def test_run_completed_without_a_summary_falls_back_to_zeros(self):
        entry = {"type": "run_completed", "index": 0, "run_id": "r1", "status": "completed"}
        assert render.eval_progress_line(entry) == (
            "| run 0 completed  id=r1  0 chars, 0 tools, 0ms"
        )

    def test_run_failed_prefers_the_error_message(self):
        entry = {"type": "run_failed", "index": 4, "error": {"message": "throttled", "code": "t"}}
        assert render.eval_progress_line(entry) == "| run 4 failed: throttled"

    def test_run_failed_falls_back_to_the_code_then_to_the_raw_value(self):
        by_code = {"type": "run_failed", "index": 1, "error": {"code": "model_throttled"}}
        assert render.eval_progress_line(by_code) == "| run 1 failed: model_throttled"
        raw = {"type": "run_failed", "index": 1, "error": "exploded"}
        assert render.eval_progress_line(raw) == "| run 1 failed: exploded"

    def test_an_empty_error_dict_renders_the_dict(self):
        entry = {"type": "run_failed", "index": 1, "error": {}}
        assert render.eval_progress_line(entry) == "| run 1 failed: {}"

    def test_grading_and_completion(self):
        assert render.eval_progress_line({"type": "grading_started"}) == "| grading"
        assert render.eval_progress_line({"type": "eval_complete", "status": "error"}) == "| error"

    def test_grading_completed_is_silent_because_the_result_is_printed_whole(self):
        assert render.eval_progress_line({"type": "grading_completed", "result": {}}) is None

    def test_an_unknown_event_type_is_silent_rather_than_a_crash(self):
        assert render.eval_progress_line({"type": "something_new"}) is None
        assert render.eval_progress_line({}) is None


class TestPartialEvents:
    """Every field the renderers read has a fallback; these pin what it renders.

    The engine populates these dicts fully, so the fallbacks only matter when
    something upstream changes shape. That is exactly when a renderer must
    degrade to a readable line instead of raising KeyError mid-run.
    """

    def test_a_bare_eval_start_renders_empty_fields_and_zero(self):
        assert render.eval_progress_line({"type": "eval_start"}) == "| evaluation   kind=  n=0"

    def test_a_bare_run_started_renders_index_zero(self):
        assert render.eval_progress_line({"type": "run_started"}) == "| run 0 started"

    def test_a_bare_run_completed_renders_blanks_and_zeros(self):
        assert render.eval_progress_line({"type": "run_completed"}) == (
            "| run 0   id=  0 chars, 0 tools, 0ms"
        )

    def test_a_bare_run_failed_renders_the_missing_error_as_none(self):
        assert render.eval_progress_line({"type": "run_failed"}) == "| run 0 failed: None"

    def test_a_bare_eval_complete_renders_an_empty_status(self):
        assert render.eval_progress_line({"type": "eval_complete"}) == "| "


class TestDurationFormatting:
    """The ms/seconds boundary, pinned from both sides."""

    def _rendered(self, milliseconds: int) -> str:
        event = ToolResultEvent(tool_use_id="t", name="n", duration_ms=milliseconds)
        return render.run_progress_line(event).split("(")[1].rstrip(")")

    def test_zero_is_milliseconds(self):
        assert self._rendered(0) == "0ms"

    def test_just_under_a_second_is_milliseconds(self):
        assert self._rendered(999) == "999ms"

    def test_exactly_a_second_is_seconds(self):
        assert self._rendered(1000) == "1.0s"

    def test_seconds_are_divided_not_multiplied(self):
        assert self._rendered(90_000) == "90.0s"


class TestEvalResultLines:
    def test_no_result_produces_nothing(self):
        assert render.eval_result_lines(None) == []
        assert render.eval_result_lines({}) == []

    def test_grade_and_score(self):
        assert render.eval_result_lines({"grade": "A", "score": 95}) == ["| grade A  score=95"]

    def test_missing_grade_and_score_render_as_unknown(self):
        assert render.eval_result_lines({"metrics": {}}) == ["| grade ?  score=?"]

    def test_reasoning_is_collapsed_onto_one_line(self):
        result = {"grade": "B", "score": 80, "reasoning": "first line\n\nsecond   line"}
        assert render.eval_result_lines(result)[1] == "|        first line second line"

    def test_long_reasoning_is_truncated_with_an_ellipsis(self):
        result = {"grade": "C", "score": 70, "reasoning": "x" * 300}
        line = render.eval_result_lines(result)[1]
        assert line.endswith("…")
        assert len(line.removeprefix("|").strip()) == 160

    def test_reasoning_of_exactly_the_limit_is_not_truncated(self):
        """The boundary: 160 characters fit, so nothing is dropped and no ellipsis."""
        line = render.eval_result_lines({"grade": "A", "score": 1, "reasoning": "x" * 160})[1]
        assert line.removeprefix("|").strip() == "x" * 160

    def test_non_string_reasoning_is_still_rendered(self):
        """`reasoning` comes off a judge payload, so it is not guaranteed a str."""
        assert render.eval_result_lines({"grade": "A", "score": 1, "reasoning": 42})[1] == (
            "|        42"
        )

    def test_empty_reasoning_adds_no_second_line(self):
        assert len(render.eval_result_lines({"grade": "A", "score": 9, "reasoning": ""})) == 1


class TestTable:
    def test_no_rows_renders_nothing_so_a_lone_header_is_impossible(self):
        assert render.table([], ["A", "B"]) == ""

    def test_columns_are_padded_to_the_widest_cell(self):
        rendered = render.table([["a", "1"], ["long", "2"]], ["H", "N"])
        assert rendered.splitlines() == ["H     N", "a     1", "long  2"]

    def test_a_header_wider_than_its_cells_sets_the_width(self):
        rendered = render.table([["a", "b"]], ["HEADER", "X"])
        assert rendered.splitlines() == ["HEADER  X", "a       b"]

    def test_the_last_column_is_never_padded(self):
        """Trailing whitespace is noise the moment this is piped into anything."""
        for line in render.table([["a", "b"], ["cc", "d"]], ["H1", "H2"]).splitlines():
            assert line == line.rstrip()


def test_dumps_stringifies_values_json_cannot_encode():
    assert render.dumps({"ts": TS}) == '{\n  "ts": "2026-01-02 03:04:05+00:00"\n}'
