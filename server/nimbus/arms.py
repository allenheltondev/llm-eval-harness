"""Reading a suite file's arms: the model-and-prompt variants to run and compare.

A suite file describes one run (``run_config``) and its cases. It can also
declare the *arms* to compare, either as a list::

    arms:
      - {name: primary,  model_id: claude-sonnet-4-5, system_prompt_file: prompts/main.md}
      - {name: fallback, model_id: amazon.nova-pro-v1:0, system_prompt_file: prompts/nova.md}
    baseline: primary          # default: an arm marked `baseline: true`, else the first

or as a models-by-prompts grid, which lets a comparison tell whether the model
or the prompt is what moves the score::

    matrix:
      models:  [{name: sonnet, model_id: ...}, {name: nova, model_id: ...}]
      prompts: [{name: main, system_prompt_file: prompts/main.md}, {name: terse, ...}]
      baseline: {model: sonnet, prompt: main}

An arm overrides only what it names; everything else is the suite's
``run_config``. ``readiness:`` is the bar a fallback must meet (see
:mod:`nimbus.evals.readiness`).

None of these keys belong to the suite the engine runs, so they are split off
before validation (:func:`split_file_keys`) and a file that has them still works
with a plain ``nimbus eval --suite``, which runs just the ``run_config``.

Prompt files
------------
``system_prompt_file`` is read from disk and sent to a model provider, and suite
files get copied around. A path is therefore confined to the suite file's own
directory: an absolute path, a ``..`` that climbs out, or a symlink that points
elsewhere is refused, so a borrowed suite cannot name ``~/.aws/credentials``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nimbus.errors import AppError
from nimbus.evals.readiness import ReadinessBar
from nimbus.evals.schemas import ArmTag

#: The most arms one comparison holds (the API's limit too).
MAX_ARMS = 12

#: Keys a suite file may carry that describe arms rather than the suite itself.
FILE_KEYS = ("arms", "matrix", "baseline", "readiness")

_RUN_KEYS = ("model_id", "provider", "inference", "toolset", "mcp_servers", "max_tool_iterations")
_ARM_KEYS = {"name", "baseline", "system_prompt", "system_prompt_file", *_RUN_KEYS}
_MODEL_KEYS = {"name", *_RUN_KEYS}
_PROMPT_KEYS = {"name", "system_prompt", "system_prompt_file"}
_MATRIX_KEYS = {"models", "prompts", "baseline"}


class ArmsError(AppError):
    """The arms a suite file declares are not usable."""

    status_code = 400
    code = "suite_arms_invalid"


@dataclass(frozen=True)
class ArmSpec:
    """One arm to run: a name, what it overrides in ``run_config``, and its place in a grid."""

    name: str
    #: Fields to lay over the suite's ``run_config``.
    overrides: dict[str, Any]
    baseline: bool = False
    axes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ArmPlan:
    """The arms a suite file declares, and the bar they are judged against."""

    arms: list[ArmSpec]
    #: The ``readiness:`` mapping, validated; ``None`` when the file states no bar.
    readiness: dict[str, Any] | None = None

    @property
    def baseline(self) -> ArmSpec:
        return next(arm for arm in self.arms if arm.baseline)


def split_file_keys(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(suite, arm_keys)``: the mapping without the arm keys, and those keys alone."""
    suite = {key: value for key, value in spec.items() if key not in FILE_KEYS}
    return suite, {key: spec[key] for key in FILE_KEYS if key in spec}


def readiness_bar(keys: dict[str, Any]) -> ReadinessBar | None:
    """The file's ``readiness:`` as a bar, or ``None`` when it has none."""
    if keys.get("readiness") is None:
        return None
    try:
        return ReadinessBar.model_validate(keys["readiness"])
    except ValidationError as exc:
        raise ArmsError(f"readiness: {_problems(exc)}") from None


def expand(keys: dict[str, Any], base_dir: Path) -> ArmPlan | None:
    """The plan a file's arm keys describe; ``None`` when it declares no arms.

    ``base_dir`` is the suite file's directory, which prompt files are read from
    and confined to.
    """
    has_arms, has_matrix = keys.get("arms") is not None, keys.get("matrix") is not None
    if has_arms and has_matrix:
        raise ArmsError("declare `arms` or `matrix`, not both")
    if not has_arms and not has_matrix:
        return None
    if has_matrix and keys.get("baseline") is not None:
        raise ArmsError("with `matrix`, name the baseline inside it: `matrix.baseline`")
    arms = (
        _from_list(keys["arms"], base_dir) if has_arms else _from_matrix(keys["matrix"], base_dir)
    )
    if len(arms) < 2:
        raise ArmsError("a comparison needs at least two arms")
    if len(arms) > MAX_ARMS:
        raise ArmsError(f"a comparison holds at most {MAX_ARMS} arms; this declares {len(arms)}")
    names = [arm.name for arm in arms]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise ArmsError(f"arm names must be unique; repeated: {', '.join(repeated)}")

    bar = readiness_bar(keys)
    arms = _with_baseline(arms, keys.get("baseline") if has_arms else _matrix_baseline(keys, arms))
    return ArmPlan(arms, bar.model_dump(exclude_none=True) if bar else None)


def with_baseline(plan: ArmPlan, name: str) -> ArmPlan:
    """The same plan measured against a different arm (``--baseline NAME``)."""
    return ArmPlan(_with_baseline(plan.arms, name), plan.readiness)


# --------------------------------------------------------------------------- #
# Reading entries
# --------------------------------------------------------------------------- #


def _problems(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
        if item["loc"]
        else item["msg"]
        for item in error.errors()
    )


def _mapping(entry: Any, where: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ArmsError(f"{where}: expected a mapping, not {type(entry).__name__}")
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ArmsError(
            f"{where}: unknown key(s) {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}"
        )
    return entry


def _name(entry: dict[str, Any], where: str) -> str:
    if "name" not in entry:
        raise ArmsError(f"{where}: every entry needs a `name`")
    try:
        return ArmTag(name=entry["name"]).name
    except ValidationError:
        raise ArmsError(
            f"{where}: {entry['name']!r} is not a usable name (letters, digits and . _ / : + -)"
        ) from None


def _prompt_text(entry: dict[str, Any], where: str, base_dir: Path) -> str | None:
    """The entry's system prompt: inline, or read from a file confined to ``base_dir``."""
    inline, filename = entry.get("system_prompt"), entry.get("system_prompt_file")
    if inline is not None and filename is not None:
        raise ArmsError(f"{where}: give `system_prompt` or `system_prompt_file`, not both")
    if filename is None:
        return inline
    if not isinstance(filename, str) or not filename:
        raise ArmsError(f"{where}: `system_prompt_file` must be a path")
    root = base_dir.resolve()
    target = (root / filename).resolve()
    if Path(filename).is_absolute() or not target.is_relative_to(root):
        raise ArmsError(
            f"{where}: `system_prompt_file` {filename!r} must stay inside the suite file's "
            "directory"
        )
    try:
        return target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ArmsError(f"{where}: cannot read {filename!r}: {exc.strerror or exc}") from None


def _overrides(entry: dict[str, Any], where: str, base_dir: Path) -> dict[str, Any]:
    overrides = {key: entry[key] for key in _RUN_KEYS if key in entry}
    prompt = _prompt_text(entry, where, base_dir)
    if prompt is not None:
        overrides["system_prompt"] = prompt
    return overrides


def _from_list(entries: Any, base_dir: Path) -> list[ArmSpec]:
    if not isinstance(entries, list):
        raise ArmsError("arms: expected a list")
    arms = []
    for index, raw in enumerate(entries):
        where = f"arms[{index}]"
        entry = _mapping(raw, where, _ARM_KEYS)
        arms.append(
            ArmSpec(
                name=_name(entry, where),
                overrides=_overrides(entry, where, base_dir),
                baseline=bool(entry.get("baseline")),
            )
        )
    return arms


def _from_matrix(matrix: Any, base_dir: Path) -> list[ArmSpec]:
    matrix = _mapping(matrix, "matrix", _MATRIX_KEYS)
    models = _axis(matrix.get("models"), "matrix.models", _MODEL_KEYS)
    prompts = _axis(matrix.get("prompts"), "matrix.prompts", _PROMPT_KEYS)
    arms = []
    for model_index, model in enumerate(models):
        for prompt_index, prompt in enumerate(prompts):
            model_where = f"matrix.models[{model_index}]"
            prompt_where = f"matrix.prompts[{prompt_index}]"
            model_name, prompt_name = _name(model, model_where), _name(prompt, prompt_where)
            arms.append(
                ArmSpec(
                    name=f"{model_name}/{prompt_name}",
                    overrides={
                        **_overrides(model, model_where, base_dir),
                        **_overrides(prompt, prompt_where, base_dir),
                    },
                    axes={"model": model_name, "prompt": prompt_name},
                )
            )
    return arms


def _axis(entries: Any, where: str, allowed: set[str]) -> list[dict[str, Any]]:
    if not isinstance(entries, list) or len(entries) < 2:
        raise ArmsError(f"{where}: give a list of at least two, or there is nothing to compare")
    return [_mapping(entry, f"{where}[{index}]", allowed) for index, entry in enumerate(entries)]


def _matrix_baseline(keys: dict[str, Any], arms: list[ArmSpec]) -> str | None:
    """The matrix's baseline cell as an arm name; the first model and prompt by default."""
    chosen = (keys["matrix"] or {}).get("baseline")
    if chosen is None:
        return None
    if not isinstance(chosen, dict) or set(chosen) - {"model", "prompt"}:
        raise ArmsError("matrix.baseline: expected {model: NAME, prompt: NAME}")
    name = f"{chosen.get('model')}/{chosen.get('prompt')}"
    if name not in {arm.name for arm in arms}:
        raise ArmsError(f"matrix.baseline: no cell {name!r} in the matrix")
    return name


def _with_baseline(arms: list[ArmSpec], requested: Any) -> list[ArmSpec]:
    """Mark exactly one arm as the baseline: the requested name, a flagged arm, or the first."""
    flagged = [arm.name for arm in arms if arm.baseline]
    if len(flagged) > 1:
        raise ArmsError(f"only one arm can be the baseline; flagged: {', '.join(flagged)}")
    if requested is not None:
        if not isinstance(requested, str) or requested not in {arm.name for arm in arms}:
            names = ", ".join(arm.name for arm in arms)
            raise ArmsError(f"baseline {requested!r} is not one of the arms ({names})")
        chosen = requested
    else:
        chosen = flagged[0] if flagged else arms[0].name
    return [ArmSpec(arm.name, arm.overrides, arm.name == chosen, arm.axes) for arm in arms]
