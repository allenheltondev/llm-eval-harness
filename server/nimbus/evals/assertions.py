"""Deterministic checks on a suite case's answer: the ``assert:`` list.

Some requirements do not need a judge. "Must mention the refund policy", "must
be valid JSON", "must call ``freeze_account``" and "must never call
``delete_*``" are settled by looking, and a string check is instant, free and
gives the same answer every time. A suite case can list any number of them::

    assert:
      - contains: refund
      - type: max_length
        value: 200
        unit: words
      - tool_called:
          name: freeze_account
          args: {account_id: A1234}

Every entry is one of the models below, discriminated by ``type``. The
single-key form (``- contains: refund``) is shorthand for the canonical one
(``- type: contains`` / ``value: refund``); see :func:`expand_shorthand`.

Everything that can be wrong with an assertion -- an unknown type, a regex that
does not compile, a JSON Schema that is not one -- is a validation error when
the suite is *submitted*, never a failure discovered after the runs were paid
for.

Each check evaluates to ``{"type", "passed", "detail"}``: pass or fail, and a
short human-readable account of what was seen. :func:`evaluate` runs a case's
list against one answer. It only reads the answer's text, tool transcript and
wall-clock duration, so it is the same in every lane.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase
from typing import Annotated, Any, Literal

from jsonschema import exceptions as jsonschema_exceptions
from jsonschema import validators as jsonschema_validators
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: At most this many checks on one case. Every check is recorded on every
#: repeat, and the whole result has a byte budget (``engine.MAX_RESULT_BYTES``).
MAX_CASE_ASSERTIONS = 10

#: The regex flags an assertion may set, by letter.
_REGEX_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}

#: How much of a quoted value a detail shows before eliding the rest.
_QUOTE_CHARS = 60


class _Assertion(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)


def _regex_flags(flags: str) -> int:
    value = 0
    for letter in flags:
        value |= _REGEX_FLAGS[letter]
    return value


def _compile(pattern: str, flags: int = 0) -> re.Pattern[str]:
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc


# --------------------------------------------------------------------------- #
# Output checks
# --------------------------------------------------------------------------- #


class ContainsAssertion(_Assertion):
    """The answer contains ``value`` (case-sensitive unless told otherwise)."""

    type: Literal["contains"]
    value: str = Field(min_length=1)
    case_sensitive: bool = True


class NotContainsAssertion(_Assertion):
    """The answer does not contain ``value``."""

    type: Literal["not_contains"]
    value: str = Field(min_length=1)
    case_sensitive: bool = True


class RegexAssertion(_Assertion):
    """``pattern`` matches somewhere in the answer (``re.search``).

    ``flags`` is any of ``i`` (ignore case), ``m`` (multiline), ``s`` (dot
    matches newline) and ``x`` (verbose). Anchor with ``^``/``$`` (or ``\\A`` /
    ``\\Z``) to match the whole answer.
    """

    type: Literal["regex"]
    pattern: str = Field(min_length=1)
    flags: str = Field(default="", pattern=r"^[imsx]*$")

    @model_validator(mode="after")
    def _compiles(self) -> RegexAssertion:
        _compile(self.pattern, _regex_flags(self.flags))
        return self


class EqualsAssertion(_Assertion):
    """The answer is exactly ``value``, ignoring surrounding whitespace unless ``strip`` is off."""

    type: Literal["equals"]
    value: str
    case_sensitive: bool = True
    strip: bool = True


class JsonValidAssertion(_Assertion):
    """The whole answer (surrounding whitespace aside) parses as JSON."""

    type: Literal["json_valid"]


class JsonSchemaAssertion(_Assertion):
    """The answer parses as JSON and validates against ``schema``.

    The schema's own ``$schema`` picks the draft; without one it is Draft 2020-12.
    """

    type: Literal["json_schema"]
    schema_: dict[str, Any] = Field(alias="schema")

    @field_validator("schema_")
    @classmethod
    def _is_a_schema(cls, schema: dict[str, Any]) -> dict[str, Any]:
        validator = jsonschema_validators.validator_for(schema)
        try:
            validator.check_schema(schema)
        except jsonschema_exceptions.SchemaError as exc:
            raise ValueError(f"invalid JSON Schema: {exc.message}") from exc
        return schema


class MaxLengthAssertion(_Assertion):
    """The answer is at most ``value`` characters (or words)."""

    type: Literal["max_length"]
    value: int = Field(ge=0)
    unit: Literal["chars", "words"] = "chars"


class MinLengthAssertion(_Assertion):
    """The answer is at least ``value`` characters (or words)."""

    type: Literal["min_length"]
    value: int = Field(ge=0)
    unit: Literal["chars", "words"] = "chars"


class MaxLatencyAssertion(_Assertion):
    """The run took at most ``value`` milliseconds, wall clock, tool calls included."""

    type: Literal["max_latency_ms"]
    value: int = Field(ge=1)


# --------------------------------------------------------------------------- #
# Tool-call checks
# --------------------------------------------------------------------------- #


def _check_arg_patterns(value: Any) -> None:
    """Compile every ``{regex: ...}`` matcher inside ``args``, so a bad one is a 422."""
    if isinstance(value, Mapping):
        if _is_regex_matcher(value):
            _compile(value["regex"])
            return
        for item in value.values():
            _check_arg_patterns(item)
    elif isinstance(value, list):
        for item in value:
            _check_arg_patterns(item)


def _is_regex_matcher(value: Any) -> bool:
    return (
        isinstance(value, Mapping) and set(value) == {"regex"} and isinstance(value["regex"], str)
    )


class ToolCalledAssertion(_Assertion):
    """A tool matching ``name`` (a glob) was called, with ``args`` if given.

    ``args`` is a *partial* match: every key given must be in the call's input
    with a matching value, and keys not given are ignored. Nested objects match
    the same way; lists match element by element and must be the same length.
    A value written ``{regex: <pattern>}`` matches any string that the pattern
    is found in.

    How many matching calls are required: ``times`` (exactly), or
    ``min_times`` / ``max_times``. With none of them, at least one.
    """

    type: Literal["tool_called"]
    name: str = Field(min_length=1)
    args: dict[str, Any] | None = None
    times: int | None = Field(default=None, ge=0)
    min_times: int | None = Field(default=None, ge=0)
    max_times: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _counts_are_consistent(self) -> ToolCalledAssertion:
        if self.times is not None and (self.min_times is not None or self.max_times is not None):
            raise ValueError("tool_called takes `times` or `min_times`/`max_times`, not both")
        if (
            self.min_times is not None
            and self.max_times is not None
            and self.min_times > self.max_times
        ):
            raise ValueError("tool_called: min_times is greater than max_times")
        _check_arg_patterns(self.args)
        return self

    def bounds(self) -> tuple[int, int | None]:
        """``(at least, at most)`` matching calls; ``None`` is unbounded."""
        if self.times is not None:
            return self.times, self.times
        if self.min_times is None and self.max_times is None:
            return 1, None
        return self.min_times or 0, self.max_times


class ToolNotCalledAssertion(_Assertion):
    """No tool matching ``name`` (a glob, e.g. ``delete_*``) was called."""

    type: Literal["tool_not_called"]
    name: str = Field(min_length=1)


class ToolSequenceAssertion(_Assertion):
    """The tools were called in this order.

    ``subsequence`` (the default): these calls happened in this order, with any
    other calls allowed in between. ``exact``: these were *all* the calls, in
    exactly this order. Each entry is a glob.
    """

    type: Literal["tool_sequence"]
    tools: list[str] = Field(min_length=1)
    mode: Literal["exact", "subsequence"] = "subsequence"


class MaxToolCallsAssertion(_Assertion):
    """At most ``value`` tool calls in the run, of any tool."""

    type: Literal["max_tool_calls"]
    value: int = Field(ge=0)


class NoToolErrorsAssertion(_Assertion):
    """No tool call in the run returned an error."""

    type: Literal["no_tool_errors"]


Assertion = Annotated[
    ContainsAssertion
    | NotContainsAssertion
    | RegexAssertion
    | EqualsAssertion
    | JsonValidAssertion
    | JsonSchemaAssertion
    | MaxLengthAssertion
    | MinLengthAssertion
    | MaxLatencyAssertion
    | ToolCalledAssertion
    | ToolNotCalledAssertion
    | ToolSequenceAssertion
    | MaxToolCallsAssertion
    | NoToolErrorsAssertion,
    Field(discriminator="type"),
]

#: For the single-key shorthand: which field the key's value fills. ``None``
#: means the check takes no value (``- json_valid: true``).
_SHORTHAND_FIELD: dict[str, str | None] = {
    "contains": "value",
    "not_contains": "value",
    "regex": "pattern",
    "equals": "value",
    "json_valid": None,
    "json_schema": "schema",
    "max_length": "value",
    "min_length": "value",
    "max_latency_ms": "value",
    "tool_called": "name",
    "tool_not_called": "name",
    "tool_sequence": "tools",
    "max_tool_calls": "value",
    "no_tool_errors": None,
}

ASSERTION_TYPES = tuple(_SHORTHAND_FIELD)


def expand_shorthand(entry: Any) -> Any:
    """``{contains: refund}`` -> ``{type: contains, value: refund}``.

    An entry without a ``type`` whose keys include exactly one assertion type
    is shorthand: that key's value fills the type's main field, and any other
    keys stay as they are (``{contains: refund, case_sensitive: false}``). For a
    check with several fields the value may instead be a mapping of them
    (``{tool_called: {name: x, times: 1}}``). A check with no value takes
    ``true``. Anything that is not a mapping is left for validation to reject.
    """
    if not isinstance(entry, Mapping) or "type" in entry:
        return entry
    keys = [key for key in entry if key in _SHORTHAND_FIELD]
    if len(keys) != 1:
        found = f" (found {', '.join(keys)})" if keys else ""
        raise ValueError(
            "each assertion needs a `type`, or exactly one of these as its key"
            f"{found}: {', '.join(ASSERTION_TYPES)}"
        )
    kind = keys[0]
    value = entry[kind]
    expanded: dict[str, Any] = {key: item for key, item in entry.items() if key != kind}
    expanded["type"] = kind
    main = _SHORTHAND_FIELD[kind]
    if main is None:
        if value is not True:
            raise ValueError(f"`{kind}` takes no value; write `- {kind}: true`")
    elif isinstance(value, Mapping) and kind != "json_schema":
        expanded.update(value)
    else:
        expanded[main] = value
    return expanded


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def _quote(text: str) -> str:
    """``text`` as a short JSON string literal, elided in the middle when long."""
    if len(text) > _QUOTE_CHARS:
        half = _QUOTE_CHARS // 2
        text = f"{text[:half]}…{text[-half:]}"
    return json.dumps(text, ensure_ascii=False)


def _verdict(check: Any, passed: bool, detail: str) -> dict[str, Any]:
    return {"type": check.type, "passed": passed, "detail": detail}


def _fold(text: str, case_sensitive: bool) -> str:
    return text if case_sensitive else text.casefold()


def _length(text: str, unit: str) -> int:
    return len(text.split()) if unit == "words" else len(text)


def _parse_json(output: str) -> tuple[bool, Any, str]:
    try:
        return True, json.loads(output), ""
    except ValueError as exc:
        return False, None, f"not valid JSON: {exc}"


def _json_path(path: Iterable[Any]) -> str:
    return "$" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in path)


def _check_output(check: Any, output: str, duration_ms: int) -> dict[str, Any]:
    match check:
        case ContainsAssertion():
            at = _fold(output, check.case_sensitive).find(_fold(check.value, check.case_sensitive))
            if at < 0:
                return _verdict(check, False, f"output does not contain {_quote(check.value)}")
            return _verdict(check, True, f"found {_quote(check.value)} at char {at}")
        case NotContainsAssertion():
            at = _fold(output, check.case_sensitive).find(_fold(check.value, check.case_sensitive))
            if at >= 0:
                return _verdict(check, False, f"output contains {_quote(check.value)} at char {at}")
            return _verdict(check, True, f"{_quote(check.value)} not found")
        case RegexAssertion():
            match = _compile(check.pattern, _regex_flags(check.flags)).search(output)
            shown = f"/{check.pattern}/{check.flags}"
            if match is None:
                return _verdict(check, False, f"no match for {_quote(shown)}")
            return _verdict(
                check, True, f"matched {_quote(match.group(0))} at char {match.start()}"
            )
        case EqualsAssertion():
            actual, expected = output, check.value
            if check.strip:
                actual, expected = actual.strip(), expected.strip()
            if _fold(actual, check.case_sensitive) == _fold(expected, check.case_sensitive):
                return _verdict(check, True, "output equals the expected value")
            return _verdict(check, False, f"expected {_quote(expected)}, got {_quote(actual)}")
        case JsonValidAssertion():
            ok, _value, problem = _parse_json(output)
            return _verdict(check, ok, "valid JSON" if ok else problem)
        case JsonSchemaAssertion():
            ok, value, problem = _parse_json(output)
            if not ok:
                return _verdict(check, False, problem)
            validator = jsonschema_validators.validator_for(check.schema_)(check.schema_)
            error = jsonschema_exceptions.best_match(validator.iter_errors(value))
            if error is None:
                return _verdict(check, True, "matches the schema")
            return _verdict(check, False, f"at {_json_path(error.absolute_path)}: {error.message}")
        case MaxLengthAssertion() | MinLengthAssertion():
            length = _length(output, check.unit)
            if isinstance(check, MaxLengthAssertion):
                passed, sign = length <= check.value, "<=" if length <= check.value else ">"
            else:
                passed, sign = length >= check.value, ">=" if length >= check.value else "<"
            return _verdict(check, passed, f"{length} {check.unit} {sign} {check.value}")
        case MaxLatencyAssertion():
            passed = duration_ms <= check.value
            sign = "<=" if passed else ">"
            return _verdict(check, passed, f"took {duration_ms} ms {sign} {check.value}")
    raise TypeError(f"not an output assertion: {check!r}")  # pragma: no cover


def args_match(expected: Any, actual: Any) -> bool:
    """Whether ``actual`` (a tool call's input) partially matches ``expected``."""
    if _is_regex_matcher(expected):
        return isinstance(actual, str) and re.search(expected["regex"], actual) is not None
    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual and args_match(value, actual[key]) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(args_match(e, a) for e, a in zip(expected, actual, strict=True))
        )
    # bool is an int in Python; `True == 1` must not match.
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    return expected == actual


def _record_name(record: Any) -> str:
    return str(record.get("name", "")) if isinstance(record, Mapping) else ""


def _record_input(record: Any) -> Any:
    return record.get("input") if isinstance(record, Mapping) else None


def _record_error(record: Any) -> Any:
    return record.get("error") if isinstance(record, Mapping) else None


def _times_text(count: int) -> str:
    return "once" if count == 1 else f"{count} times"


def _expected_count(low: int, high: int | None) -> str:
    if high is None:
        return f"at least {low}"
    if low == high:
        return f"exactly {low}"
    if low == 0:
        return f"at most {high}"
    return f"{low}-{high}"


def _names_text(names: list[str]) -> str:
    return ", ".join(names) if names else "no tools"


def _check_tools(check: Any, transcript: list[Any]) -> dict[str, Any]:
    names = [_record_name(record) for record in transcript]
    match check:
        case ToolCalledAssertion():
            by_name = [r for r in transcript if fnmatchcase(_record_name(r), check.name)]
            matching = (
                by_name
                if check.args is None
                else [r for r in by_name if args_match(check.args, _record_input(r))]
            )
            low, high = check.bounds()
            count = len(matching)
            passed = count >= low and (high is None or count <= high)
            detail = f"{check.name} called {_times_text(len(by_name))}"
            if check.args is not None:
                detail += f", {count} with matching args"
            detail += f" (expected {_expected_count(low, high)})"
            return _verdict(check, passed, detail)
        case ToolNotCalledAssertion():
            called = sorted({name for name in names if fnmatchcase(name, check.name)})
            if called:
                counts = ", ".join(f"{name} x{names.count(name)}" for name in called)
                return _verdict(check, False, f"{check.name} was called: {counts}")
            return _verdict(check, True, f"{check.name} was not called")
        case ToolSequenceAssertion():
            if check.mode == "exact":
                passed = len(names) == len(check.tools) and all(
                    fnmatchcase(name, pattern)
                    for name, pattern in zip(names, check.tools, strict=True)
                )
            else:
                remaining = iter(names)
                passed = all(
                    any(fnmatchcase(name, pattern) for name in remaining) for pattern in check.tools
                )
            detail = f"expected {' -> '.join(check.tools)} ({check.mode}), got {_names_text(names)}"
            return _verdict(check, passed, detail)
        case MaxToolCallsAssertion():
            passed = len(names) <= check.value
            sign = "<=" if passed else ">"
            return _verdict(check, passed, f"{len(names)} tool calls {sign} {check.value}")
        case NoToolErrorsAssertion():
            failed = [record for record in transcript if _record_error(record)]
            if not failed:
                return _verdict(check, True, f"{len(names)} tool calls, none errored")
            first = _record_error(failed[0])
            message = first.get("message") if isinstance(first, Mapping) else first
            detail = (
                f"{len(failed)} of {len(names)} tool calls errored; "
                f"first: {_record_name(failed[0])}: {message}"
            )
            return _verdict(check, False, detail)
    raise TypeError(f"not a tool assertion: {check!r}")  # pragma: no cover


_TOOL_ASSERTIONS = (
    ToolCalledAssertion,
    ToolNotCalledAssertion,
    ToolSequenceAssertion,
    MaxToolCallsAssertion,
    NoToolErrorsAssertion,
)


def evaluate(
    checks: Iterable[Any],
    *,
    output: str,
    tool_transcript: list[Any] | None = None,
    duration_ms: int = 0,
) -> list[dict[str, Any]]:
    """Run each check against one answer: ``[{"type", "passed", "detail"}]``, in order."""
    transcript = list(tool_transcript or [])
    return [
        _check_tools(check, transcript)
        if isinstance(check, _TOOL_ASSERTIONS)
        else _check_output(check, output or "", duration_ms)
        for check in checks
    ]
