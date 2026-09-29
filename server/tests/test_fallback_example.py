"""The documented fallback example stays true: it parses, expands, and validates."""

from __future__ import annotations

from pathlib import Path

import yaml

from nimbus import arms, suite_edit



def _find_examples() -> Path:
    """Locate ``docs/examples`` by walking up, not by counting parents.

    mutmut copies ``tests/`` into ``server/mutants/``, one level deeper, so a
    fixed ``parents[2]`` misses the directory and the whole mutation job dies on
    its baseline run.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "examples"
        if candidate.is_dir():
            return candidate
    raise AssertionError("could not find docs/examples above this test")


EXAMPLES = _find_examples()


def test_the_example_declares_two_arms_a_bar_and_a_critical_case():
    text = (EXAMPLES / "fallback-suite.yaml").read_text(encoding="utf-8")
    spec = yaml.safe_load(text)
    suite, keys = arms.split_file_keys(spec)

    plan = arms.expand(keys, EXAMPLES)

    assert plan is not None
    assert [arm.name for arm in plan.arms] == ["primary", "fallback"]
    assert plan.baseline.name == "primary"
    assert plan.arms[0].overrides["system_prompt"].startswith("You are a support agent")
    assert plan.readiness == {
        "max_regression_rate": 0.1,
        "max_latency_ratio": 1.5,
        "max_cost_ratio": 1.0,
    }
    assert [case["id"] for case in suite["cases"] if case.get("critical")] == ["refund-window"]


def test_the_example_is_a_valid_suite():
    suite_edit.validate_suite_text((EXAMPLES / "fallback-suite.yaml").read_text(encoding="utf-8"))


def commented_block(text: str, first_line: str) -> str:
    """The comment block starting at ``first_line``, with the comment markers removed."""
    lines = text.splitlines()
    start = lines.index(first_line)
    block = []
    for line in lines[start:]:
        if not line.startswith("#"):
            break
        block.append(line[2:] if line.startswith("# ") else line[1:])
    return "\n".join(block)


def test_the_matrix_in_the_example_comment_is_valid_too():
    text = (EXAMPLES / "fallback-suite.yaml").read_text(encoding="utf-8")
    keys = yaml.safe_load(commented_block(text, "# matrix:"))

    plan = arms.expand(keys, EXAMPLES)

    assert plan is not None
    assert [arm.name for arm in plan.arms] == [
        "pro/primary",
        "pro/fallback",
        "lite/primary",
        "lite/fallback",
    ]
    assert plan.baseline.name == "pro/primary"
