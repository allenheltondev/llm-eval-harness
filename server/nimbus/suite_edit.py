"""Turning a recorded run into a suite case, and adding a case to a suite file.

A suite file is written by hand and often commented, so it is edited as *text*:
a case is appended after the last one and everything above it is left exactly
as it was. Round-tripping the file through a YAML library would drop its
comments and reflow it.

Text edits are easy to get subtly wrong, so none is trusted. After appending,
the new text is parsed again and must satisfy three things: the original text
is an unchanged prefix, the cases before the new one are unchanged, and the new
last case is exactly the one asked for. Anything else is refused with a message
saying how to add the case by hand. Only then is the whole suite validated as
the engine would validate it.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml
from pydantic import ValidationError

from nimbus.errors import AppError, BadRequestError
from nimbus.evals.schemas import EvaluationRequest

#: Top-level keys a suite *file* carries that belong to the evaluation request,
#: not to the suite proper (mirrors ``commands.build_suite_request``).
_REQUEST_LEVEL_KEYS = ("rubric", "grader", "panel", "max_cost_usd")

_CASE_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class SuiteEditError(AppError):
    """A case could not be added to the suite safely."""

    status_code = 400
    code = "suite_edit_failed"


class _Dumper(yaml.SafeDumper):
    """A safe YAML dumper that writes multi-line text as readable ``|`` blocks."""


def _represent_text(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _represent_text)


def default_case_id(run_id: str) -> str:
    """``run-`` and the first eight characters of the run's id."""
    return f"run-{_CASE_ID_UNSAFE.sub('', run_id)[:8]}"


def case_from_run(
    run: dict[str, Any],
    *,
    case_id: str | None = None,
    expected: bool = False,
    criteria: str | None = None,
) -> dict[str, Any]:
    """A suite case from a stored run's prompt.

    ``expected`` copies the run's *output* in as the reference answer. That is
    opt-in because a recorded answer is what the model said, not what is right;
    promoting one blindly would grade future runs against a mistake.
    """
    case: dict[str, Any] = {
        "id": case_id or default_case_id(str(run["id"])),
        "input": run["user_prompt"],
    }
    if expected:
        output = run.get("output")
        if not output:
            raise BadRequestError(
                f"run {run['id']} has no output to use as the expected answer "
                f"(status: {run.get('status')})",
                code="promote_no_output",
            )
        case["expected"] = output
    if criteria:
        case["criteria"] = criteria
    return case


def render_case(case: dict[str, Any]) -> str:
    """The case as a YAML list item, indented to sit under ``cases:``."""
    body = yaml.dump(
        [case], Dumper=_Dumper, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return "".join(f"  {line}" if line.strip() else line for line in body.splitlines(True))


def new_suite_text(run: dict[str, Any], case: dict[str, Any], *, as_json: bool = False) -> str:
    """A whole suite file holding just ``case``, configured as the run was."""
    config = run.get("config") or {}
    run_config: dict[str, Any] = {"model_id": run["model_id"]}
    if config.get("provider") and config["provider"] != "bedrock":
        run_config["provider"] = config["provider"]
    if run.get("system_prompt"):
        run_config["system_prompt"] = run["system_prompt"]
    if config.get("inference"):
        run_config["inference"] = config["inference"]
    for key in ("toolset", "mcp_servers", "max_tool_iterations"):
        if config.get(key) not in (None, [], ""):
            run_config[key] = config[key]
    header = yaml.dump(
        {"run_config": run_config},
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    text = f"{header}cases:\n{render_case(case)}"
    validate_suite_text(text)
    return (
        json.dumps(yaml.safe_load(text), indent=2, ensure_ascii=False) + "\n" if as_json else text
    )


def append_case(text: str, case: dict[str, Any], *, as_json: bool = False) -> str:
    """``text`` (a suite file) with ``case`` added as its last case.

    Raises :class:`SuiteEditError` when the case cannot be added without
    risking the file: an id already in use, ``cases`` not being the last key of a
    YAML file, or an edit that does not read back as intended.
    """
    data = _parse(text)
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise SuiteEditError("the suite has no `cases` list to add to")
    taken = {entry.get("id") for entry in cases if isinstance(entry, dict)}
    if case["id"] in taken:
        raise SuiteEditError(f"the suite already has a case with id {case['id']!r}; pass --id")

    if as_json:
        data["cases"] = [*cases, case]
        updated = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    else:
        if list(data)[-1] != "cases":
            raise SuiteEditError(
                "`cases` is not the last key in the suite file, so a case cannot be "
                "appended without moving things around"
            )
        updated = text if text.endswith("\n") else text + "\n"
        updated += render_case(case)
        _verify_append(text, updated, cases, case)

    validate_suite_text(updated)
    return updated


def validate_suite_text(text: str) -> None:
    """Raise unless ``text`` is a suite the evaluation engine accepts."""
    data = _parse(text)
    request_level = {key: data.pop(key) for key in _REQUEST_LEVEL_KEYS if key in data}
    try:
        EvaluationRequest.model_validate({"kind": "suite", "suite": data, **request_level})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise SuiteEditError(f"the suite would not be valid: {problems}") from None
    except AppError as exc:
        raise SuiteEditError(f"the suite would not be valid: {exc.message}") from None


def _parse(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SuiteEditError("the suite file is not valid YAML or JSON") from exc
    if not isinstance(data, dict):
        raise SuiteEditError("the suite file must be a mapping at the top level")
    return data


def _verify_append(
    original: str, updated: str, cases_before: list[Any], case: dict[str, Any]
) -> None:
    """Refuse an append that does not read back exactly as asked (see the module doc)."""
    stem = original if original.endswith("\n") else original + "\n"
    if not updated.startswith(stem):
        raise SuiteEditError("the edit would have altered the existing file")
    try:
        reread = yaml.safe_load(updated)
    except yaml.YAMLError as exc:
        raise SuiteEditError(
            "the case does not fit this file's layout (is `cases` written inline as [ ... ]?)"
        ) from exc
    after = reread.get("cases") if isinstance(reread, dict) else None
    if after != [*cases_before, case]:
        raise SuiteEditError(
            "the case does not fit this file's layout (is `cases` written inline as [ ... ]?)"
        )
