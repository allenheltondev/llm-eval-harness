"""Turning a run into a suite case, and adding a case to a suite file safely."""

from __future__ import annotations

import json

import pytest
import yaml

from nimbus import suite_edit
from nimbus.cli import commands
from nimbus.errors import BadRequestError
from nimbus.evals.schemas import SuiteCase
from nimbus.suite_edit import SuiteEditError, append_case, case_from_run, render_case

RUN = {
    "id": "0123456789abcdef0123456789abcdef",
    "model_id": "anthropic.claude-3",
    "system_prompt": "You are terse.",
    "user_prompt": "How long do I have to return shoes?",
    "output": "30 days.",
    "status": "completed",
    "config": {"provider": "bedrock", "inference": {"temperature": 0.2}, "toolset": None},
}

MINIMAL = "run_config:\n  model_id: m\ncases:\n  - id: first\n    input: hello\n"


def case(**fields):
    return {"id": "new-case", "input": "a question", **fields}


# --------------------------------------------------------------------------- #
# The case a run makes
# --------------------------------------------------------------------------- #


def test_a_case_takes_the_runs_prompt_and_a_default_id():
    built = case_from_run(RUN)

    assert built == {"id": "run-01234567", "input": "How long do I have to return shoes?"}


def test_the_recorded_answer_is_only_the_reference_when_asked_for():
    assert "expected" not in case_from_run(RUN)
    assert case_from_run(RUN, expected=True)["expected"] == "30 days."


def test_a_run_with_no_output_cannot_supply_an_expected_answer():
    failed = {**RUN, "output": None, "status": "error"}

    with pytest.raises(BadRequestError) as caught:
        case_from_run(failed, expected=True)

    assert caught.value.code == "promote_no_output"


def test_an_id_and_criteria_are_taken_as_given():
    built = case_from_run(RUN, case_id="returns", criteria="Must say 30 days.")

    assert built == {
        "id": "returns",
        "input": "How long do I have to return shoes?",
        "criteria": "Must say 30 days.",
    }


@pytest.mark.parametrize("run_id", ["ab/../cd-12345", "  spaces and/slashes ", "ÄÖ-üñí-1234", "x"])
def test_the_default_id_is_always_one_the_suite_accepts(run_id):
    SuiteCase.model_validate({"id": suite_edit.default_case_id(run_id), "input": "q"})


# --------------------------------------------------------------------------- #
# Appending to a file
# --------------------------------------------------------------------------- #


def test_the_existing_file_is_kept_byte_for_byte_above_the_new_case():
    original = commands.starter_suite()

    updated = append_case(original, case())

    assert updated.startswith(original)  # every comment and block scalar survives
    assert yaml.safe_load(updated)["cases"][-1] == case()
    assert len(yaml.safe_load(updated)["cases"]) == len(yaml.safe_load(original)["cases"]) + 1


def test_the_starter_suite_with_a_promoted_case_still_validates():
    updated = append_case(commands.starter_suite(), case_from_run(RUN, expected=True))

    suite_edit.validate_suite_text(updated)


def test_a_file_without_a_trailing_newline_is_handled():
    updated = append_case(MINIMAL.rstrip("\n"), case())

    assert [entry["id"] for entry in yaml.safe_load(updated)["cases"]] == ["first", "new-case"]


@pytest.mark.parametrize(
    "text",
    [
        "line one\nline two: with a colon\n  and #hash and 'quotes' and \"doubles\"",
        "ends with spaces   ",
        "unicode: café ☕ 日本語",
        "- looks like a list\n- item",
        "yes",  # a YAML boolean if left bare
        "123",  # a YAML number if left bare
        "null",
        "{not: a mapping}",
    ],
)
def test_awkward_prompt_text_reads_back_exactly(text):
    updated = append_case(MINIMAL, case(input=text))

    assert yaml.safe_load(updated)["cases"][-1]["input"] == text


def test_multiline_text_is_written_as_a_readable_block():
    updated = append_case(MINIMAL, case(input="first line\nsecond line\n"))

    assert "input: |" in updated


def test_a_case_id_already_in_the_suite_is_refused():
    with pytest.raises(SuiteEditError, match="already has a case with id 'first'"):
        append_case(MINIMAL, case(id="first"))


def test_a_suite_whose_cases_are_not_last_is_refused_not_rearranged():
    text = MINIMAL + "pass_threshold: 0.5\n"

    with pytest.raises(SuiteEditError, match="not the last key"):
        append_case(text, case())


def test_cases_written_inline_are_refused_because_they_cannot_be_appended_to_safely():
    text = "run_config: {model_id: m}\ncases: [{id: first, input: hello}]\n"

    with pytest.raises(SuiteEditError, match="does not fit this file's layout"):
        append_case(text, case())


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "just text", "run_config: {model_id: m}\n"])
def test_a_file_that_is_not_a_suite_is_refused(text):
    with pytest.raises(SuiteEditError):
        append_case(text, case())


def test_an_invalid_case_is_refused_before_anything_is_written():
    with pytest.raises(SuiteEditError, match="would not be valid"):
        append_case(MINIMAL, case(id="has spaces"))


def test_the_file_is_never_returned_in_a_state_the_engine_would_reject():
    """Whatever comes back validates: the safety net does not depend on the case."""
    for candidate in (case(), case(criteria="be brief"), case(expected="x")):
        suite_edit.validate_suite_text(append_case(MINIMAL, candidate))


def test_a_json_suite_is_extended_as_json():
    original = json.dumps(
        {"run_config": {"model_id": "m"}, "cases": [{"id": "first", "input": "hello"}]}
    )

    updated = append_case(original, case(), as_json=True)

    parsed = json.loads(updated)
    assert [entry["id"] for entry in parsed["cases"]] == ["first", "new-case"]


def test_request_level_keys_in_the_file_do_not_stop_it_validating():
    text = "rubric: Be accurate.\ngrader: {model_id: j}\n" + MINIMAL

    updated = append_case(text, case())

    assert updated.startswith(text)


# --------------------------------------------------------------------------- #
# A new file
# --------------------------------------------------------------------------- #


def test_a_new_suite_is_configured_as_the_run_was():
    text = suite_edit.new_suite_text(RUN, case_from_run(RUN, case_id="returns"))

    data = yaml.safe_load(text)
    assert data["run_config"] == {
        "model_id": "anthropic.claude-3",
        "system_prompt": "You are terse.",
        "inference": {"temperature": 0.2},
    }
    assert data["cases"] == [{"id": "returns", "input": "How long do I have to return shoes?"}]


def test_a_new_suite_carries_a_non_default_provider_and_toolset():
    run = {**RUN, "config": {"provider": "openai", "toolset": "fraud-detection"}}

    data = yaml.safe_load(suite_edit.new_suite_text(run, case_from_run(run)))

    assert data["run_config"]["provider"] == "openai"
    assert data["run_config"]["toolset"] == "fraud-detection"


def test_a_new_json_suite_is_json():
    text = suite_edit.new_suite_text(RUN, case_from_run(RUN), as_json=True)

    assert json.loads(text)["cases"][0]["id"] == "run-01234567"


def test_rendering_a_case_indents_it_under_cases():
    assert render_case(case()).splitlines()[0] == "  - id: new-case"


# --------------------------------------------------------------------------- #
# A suite that declares arms
# --------------------------------------------------------------------------- #


def test_a_case_can_be_added_to_a_suite_that_declares_arms_and_a_bar():
    text = (
        "run_config: {model_id: m}\n"
        "readiness: {max_regression_rate: 0.1}\n"
        "arms:\n  - {name: a}\n  - {name: b, model_id: n}\n"
        "baseline: a\n"
        "cases:\n  - id: first\n    input: hello\n"
    )

    updated = append_case(text, case_from_run(RUN, case_id="added", critical=True))

    assert updated.startswith(text)
    assert yaml.safe_load(updated)["cases"][-1] == {
        "id": "added",
        "input": "How long do I have to return shoes?",
        "critical": True,
    }


def test_a_promoted_case_is_critical_only_when_asked():
    assert "critical" not in case_from_run(RUN)
    assert case_from_run(RUN, critical=True)["critical"] is True
