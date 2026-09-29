"""Reading a suite file's arms: lists, grids, the baseline, and where prompts may come from."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nimbus import arms
from nimbus.arms import MAX_ARMS, ArmsError, expand, readiness_bar, split_file_keys, with_baseline


@pytest.fixture
def directory(tmp_path) -> Path:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "main.md").write_text("Answer in full.", encoding="utf-8")
    (tmp_path / "prompts" / "terse.md").write_text("Be terse.\n", encoding="utf-8")
    return tmp_path


def plan(keys: dict, directory: Path):
    result = expand(keys, directory)
    assert result is not None
    return result


# --------------------------------------------------------------------------- #
# A suite file's keys
# --------------------------------------------------------------------------- #


def test_the_arm_keys_are_split_off_the_suite():
    spec = {"run_config": {}, "cases": [], "arms": [1], "baseline": "a", "readiness": {}, "x": 1}

    suite, keys = split_file_keys(spec)

    assert suite == {"run_config": {}, "cases": [], "x": 1}
    assert keys == {"arms": [1], "baseline": "a", "readiness": {}}


def test_a_file_that_declares_no_arms_has_no_plan(directory):
    assert expand({}, directory) is None
    assert expand({"readiness": {"max_regression_rate": 0.1}}, directory) is None


# --------------------------------------------------------------------------- #
# Lists
# --------------------------------------------------------------------------- #


def test_an_arm_overrides_only_what_it_names(directory):
    result = plan(
        {
            "arms": [
                {"name": "a", "model_id": "m1"},
                {
                    "name": "b",
                    "model_id": "m2",
                    "provider": "openai",
                    "inference": {"temperature": 0},
                },
            ]
        },
        directory,
    )

    assert result.arms[0].overrides == {"model_id": "m1"}
    assert result.arms[1].overrides == {
        "model_id": "m2",
        "provider": "openai",
        "inference": {"temperature": 0},
    }


def test_a_prompt_can_be_inline_or_from_a_file_next_to_the_suite(directory):
    result = plan(
        {
            "arms": [
                {"name": "a", "system_prompt": "Inline."},
                {"name": "b", "system_prompt_file": "prompts/main.md"},
            ]
        },
        directory,
    )

    assert result.arms[0].overrides["system_prompt"] == "Inline."
    assert result.arms[1].overrides["system_prompt"] == "Answer in full."


def test_the_first_arm_is_the_baseline_unless_told_otherwise(directory):
    result = plan({"arms": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}, directory)

    assert [arm.baseline for arm in result.arms] == [True, False, False]
    assert result.baseline.name == "a"


def test_an_arm_can_be_flagged_as_the_baseline(directory):
    result = plan({"arms": [{"name": "a"}, {"name": "b", "baseline": True}]}, directory)

    assert result.baseline.name == "b"


def test_the_baseline_can_be_named_at_the_top_level(directory):
    result = plan({"arms": [{"name": "a"}, {"name": "b"}], "baseline": "b"}, directory)

    assert result.baseline.name == "b"


def test_a_named_baseline_beats_a_flagged_one(directory):
    result = plan(
        {"arms": [{"name": "a", "baseline": True}, {"name": "b"}], "baseline": "b"}, directory
    )

    assert result.baseline.name == "b"


def test_two_flagged_baselines_are_refused(directory):
    with pytest.raises(ArmsError, match="only one arm can be the baseline"):
        expand(
            {"arms": [{"name": "a", "baseline": True}, {"name": "b", "baseline": True}]}, directory
        )


def test_a_baseline_that_is_not_an_arm_is_refused_and_the_choices_named(directory):
    with pytest.raises(ArmsError, match=r"'x' is not one of the arms \(a, b\)"):
        expand({"arms": [{"name": "a"}, {"name": "b"}], "baseline": "x"}, directory)


def test_the_baseline_can_be_changed_afterwards(directory):
    original = plan({"arms": [{"name": "a"}, {"name": "b"}]}, directory)

    changed = with_baseline(original, "b")

    assert changed.baseline.name == "b"
    assert original.baseline.name == "a"  # the original is not mutated
    with pytest.raises(ArmsError):
        with_baseline(original, "nope")


@pytest.mark.parametrize(
    ("keys", "message"),
    [
        ({"arms": "nope"}, "arms: expected a list"),
        ({"arms": [{"name": "a"}]}, "at least two arms"),
        ({"arms": [{"name": "a"}, "b"]}, r"arms\[1\]: expected a mapping"),
        ({"arms": [{"name": "a"}, {"model_id": "m"}]}, r"arms\[1\]: every entry needs a `name`"),
        ({"arms": [{"name": "a"}, {"name": "b c"}]}, r"'b c' is not a usable name"),
        ({"arms": [{"name": "a"}, {"name": "a"}]}, "repeated: a"),
        (
            {"arms": [{"name": "a"}, {"name": "b", "modle_id": "m"}]},
            r"arms\[1\]: unknown key\(s\) modle_id",
        ),
        (
            {
                "arms": [
                    {"name": "a"},
                    {"name": "b", "system_prompt": "x", "system_prompt_file": "y"},
                ]
            },
            "not both",
        ),
        ({"arms": [{"name": "a"}, {"name": "b"}], "matrix": {}}, "`arms` or `matrix`, not both"),
    ],
)
def test_a_malformed_list_says_exactly_what_is_wrong(keys, message, directory):
    with pytest.raises(ArmsError, match=message):
        expand(keys, directory)


def test_at_most_twelve_arms(directory):
    many = [{"name": f"a{n}"} for n in range(MAX_ARMS + 1)]

    with pytest.raises(ArmsError, match="at most 12 arms; this declares 13"):
        expand({"arms": many}, directory)
    assert len(plan({"arms": many[:MAX_ARMS]}, directory).arms) == MAX_ARMS


# --------------------------------------------------------------------------- #
# Grids
# --------------------------------------------------------------------------- #

MATRIX = {
    "matrix": {
        "models": [
            {"name": "sonnet", "model_id": "claude-s"},
            {"name": "nova", "model_id": "nova-p"},
        ],
        "prompts": [
            {"name": "main", "system_prompt_file": "prompts/main.md"},
            {"name": "terse", "system_prompt_file": "prompts/terse.md"},
        ],
    }
}


def test_a_matrix_is_every_model_with_every_prompt(directory):
    result = plan(MATRIX, directory)

    assert [arm.name for arm in result.arms] == [
        "sonnet/main",
        "sonnet/terse",
        "nova/main",
        "nova/terse",
    ]
    nova_terse = result.arms[3]
    assert nova_terse.overrides == {"model_id": "nova-p", "system_prompt": "Be terse.\n"}
    assert nova_terse.axes == {"model": "nova", "prompt": "terse"}


def test_the_matrix_baseline_defaults_to_the_first_model_and_prompt(directory):
    assert plan(MATRIX, directory).baseline.name == "sonnet/main"


def test_the_matrix_baseline_can_be_any_cell(directory):
    keys = {"matrix": {**MATRIX["matrix"], "baseline": {"model": "nova", "prompt": "terse"}}}

    assert plan(keys, directory).baseline.name == "nova/terse"


def test_a_matrix_baseline_that_is_not_a_cell_is_refused(directory):
    keys = {"matrix": {**MATRIX["matrix"], "baseline": {"model": "nova", "prompt": "verbose"}}}

    with pytest.raises(ArmsError, match="no cell 'nova/verbose'"):
        expand(keys, directory)


def test_with_a_matrix_the_baseline_belongs_inside_it(directory):
    with pytest.raises(ArmsError, match="`matrix.baseline`"):
        expand({**MATRIX, "baseline": "sonnet/main"}, directory)


@pytest.mark.parametrize(
    ("matrix", "message"),
    [
        (
            {"models": [{"name": "a", "model_id": "m"}], "prompts": [{"name": "p"}, {"name": "q"}]},
            "matrix.models",
        ),
        ({"models": [{"name": "a"}, {"name": "b"}], "prompts": [{"name": "p"}]}, "matrix.prompts"),
        ({"models": [{"name": "a"}, {"name": "b"}]}, "matrix.prompts"),
        (
            {
                "models": [{"name": "a"}, {"name": "b"}],
                "prompts": [{"name": "p"}, {"name": "q"}],
                "x": 1,
            },
            "unknown key",
        ),
        (
            {
                "models": [{"name": "a", "system_prompt": "x"}, {"name": "b"}],
                "prompts": [{"name": "p"}, {"name": "q"}],
            },
            r"matrix.models\[0\]: unknown key\(s\) system_prompt",
        ),
    ],
)
def test_a_malformed_matrix_says_what_is_wrong(matrix, message, directory):
    with pytest.raises(ArmsError, match=message):
        expand({"matrix": matrix}, directory)


def test_a_grid_too_big_to_compare_is_refused(directory):
    models = [{"name": f"m{n}", "model_id": f"m{n}"} for n in range(4)]
    prompts = [{"name": f"p{n}"} for n in range(4)]

    with pytest.raises(ArmsError, match="at most 12 arms; this declares 16"):
        expand({"matrix": {"models": models, "prompts": prompts}}, directory)


# --------------------------------------------------------------------------- #
# Prompt files stay where the suite is
# --------------------------------------------------------------------------- #


def with_file(filename) -> dict:
    return {"arms": [{"name": "a", "system_prompt_file": filename}, {"name": "b"}]}


def test_a_prompt_file_in_a_subdirectory_is_fine(directory):
    assert plan(with_file("prompts/main.md"), directory).arms[0].overrides["system_prompt"]


def test_a_dot_slash_path_inside_the_directory_is_fine(directory):
    assert plan(with_file("./prompts/../prompts/main.md"), directory).arms[0].overrides


def test_a_path_that_climbs_out_of_the_directory_is_refused(directory):
    secret = directory.parent / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")

    with pytest.raises(ArmsError, match="must stay inside the suite file's directory"):
        expand(with_file("../secret.txt"), directory)


def test_an_absolute_path_is_refused_even_when_it_exists(directory, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "creds"
    outside.write_text("AKIA...", encoding="utf-8")

    with pytest.raises(ArmsError, match="must stay inside"):
        expand(with_file(str(outside)), directory)


def test_a_path_inside_the_directory_given_absolutely_is_also_refused(directory):
    with pytest.raises(ArmsError, match="must stay inside"):
        expand(with_file(str(directory / "prompts" / "main.md")), directory)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_a_symlink_that_points_outside_is_refused(directory, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "creds"
    outside.write_text("AKIA...", encoding="utf-8")
    (directory / "prompts" / "innocent.md").symlink_to(outside)

    with pytest.raises(ArmsError, match="must stay inside"):
        expand(with_file("prompts/innocent.md"), directory)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_a_symlink_that_stays_inside_is_fine(directory):
    (directory / "prompts" / "alias.md").symlink_to(directory / "prompts" / "main.md")

    assert plan(with_file("prompts/alias.md"), directory).arms[0].overrides["system_prompt"] == (
        "Answer in full."
    )


def test_the_secret_is_never_read_when_the_path_is_refused(directory, monkeypatch):
    secret = directory.parent / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    read: list[Path] = []
    original = Path.read_text
    monkeypatch.setattr(
        Path, "read_text", lambda self, *a, **k: read.append(self) or original(self, *a, **k)
    )

    with pytest.raises(ArmsError):
        expand(with_file("../secret.txt"), directory)

    assert secret.resolve() not in [path.resolve() for path in read]


def test_a_missing_prompt_file_is_named(directory):
    with pytest.raises(ArmsError, match=r"cannot read 'prompts/gone.md'"):
        expand(with_file("prompts/gone.md"), directory)


@pytest.mark.parametrize("bad", ["", None, 7, ["a"]])
def test_a_prompt_file_that_is_not_a_path_is_refused(bad, directory):
    keys = {"arms": [{"name": "a", "system_prompt_file": bad}, {"name": "b"}]}
    if bad is None:  # `system_prompt_file: null` is the same as not giving one
        assert expand(keys, directory) is not None
        return

    with pytest.raises(ArmsError):
        expand(keys, directory)


# --------------------------------------------------------------------------- #
# The bar
# --------------------------------------------------------------------------- #


def test_the_bar_is_validated_and_kept_with_the_plan(directory):
    result = plan(
        {
            "arms": [{"name": "a"}, {"name": "b"}],
            "readiness": {"max_regression_rate": 0.05, "max_latency_ratio": 2},
        },
        directory,
    )

    assert result.readiness == {"max_regression_rate": 0.05, "max_latency_ratio": 2.0}


def test_no_bar_means_none(directory):
    assert plan({"arms": [{"name": "a"}, {"name": "b"}]}, directory).readiness is None


def test_a_nonsense_bar_is_refused_with_the_field(directory):
    with pytest.raises(ArmsError, match="readiness: max_regression_rate"):
        readiness_bar({"readiness": {"max_regression_rate": 3}})


def test_a_bar_alone_is_still_read_for_a_single_run():
    assert readiness_bar({"readiness": {"max_cost_ratio": 2}}).max_cost_ratio == 2
    assert readiness_bar({}) is None
    assert arms.FILE_KEYS == ("arms", "matrix", "baseline", "readiness")
