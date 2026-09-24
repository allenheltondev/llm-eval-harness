"""Request models for ``POST /api/v1/evaluations``."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evalharness.engine.schemas import RunRequest
from evalharness.errors import BadRequestError
from evalharness.evals.judge import DEFAULT_JUDGE_MODEL_ID
from evalharness.providers import DEFAULT_PROVIDER, Provider

MIN_RUNS = 2
MAX_RUNS = 25
DEFAULT_DETERMINISM_RUNS = 10

#: Bounds on a test suite. Rejected when exceeded, never clamped: silently
#: dropping cases from a suite would report a pass rate for tests nobody ran.
MAX_SUITE_CASES = 100
MAX_SUITE_REPEATS = 10
MAX_SUITE_RUNS = 200
#: A case passes when its mean judge score (0-1) reaches this, unless the suite
#: sets its own ``pass_threshold``.
DEFAULT_PASS_THRESHOLD = 0.7


class GraderConfig(BaseModel):
    """Which model judges, and with what system prompt.

    ``system_prompt`` reaches the judge agent verbatim (see
    :mod:`evalharness.evals.grader`).
    """

    model_config = ConfigDict(extra="forbid")

    #: Defaults to a *Bedrock* model id, which is why the validator below
    #: exists: the default is only meaningful on the default provider.
    model_id: str = Field(default=DEFAULT_JUDGE_MODEL_ID, min_length=1)
    #: Which SDK runs the judge. Independent of the graded runs' provider -- an
    #: OpenAI judge grading Bedrock runs is a perfectly reasonable setup.
    provider: Provider = DEFAULT_PROVIDER
    system_prompt: str | None = None

    @model_validator(mode="after")
    def _judge_model_belongs_to_its_provider(self) -> GraderConfig:
        """A non-Bedrock judge must name its own model.

        ``model_id`` defaults to a Bedrock model id, and
        :func:`~evalharness.evals.judge.build_judge_model` hands whatever it is
        given straight to the named provider. Inheriting the default onto
        OpenAI, Anthropic or Ollama therefore builds a judge that can only fail
        at the provider, minutes into an evaluation, after the repeats have
        already been executed and paid for.

        Rejected up front rather than defaulted per provider: model ids churn,
        and for a local Ollama the right judge depends entirely on what the
        caller has pulled. Guessing here would rot; asking cannot.
        """
        if self.provider != "bedrock" and "model_id" not in self.model_fields_set:
            raise BadRequestError(
                f"A {self.provider!r} judge needs an explicit grader.model_id: "
                f"the default ({DEFAULT_JUDGE_MODEL_ID}) is a Bedrock model id",
                detail={"provider": self.provider, "default_model_id": DEFAULT_JUDGE_MODEL_ID},
                code="judge_model_requires_provider",
            )
        return self


class SuiteRunConfig(RunRequest):
    """What every case in a suite runs with: a ``RunRequest`` minus the prompt.

    A subclass rather than a copy of the fields, so a setting added to runs is
    automatically a setting suites accept -- and the guardrail/provider rule
    and every other ``RunRequest`` validator apply here too.

    ``user_prompt`` is each case's ``input``. Setting it here is rejected rather
    than ignored: a suite file that sets one is written by someone who thinks it
    does something.

    It is excluded from serialization *at the field*, not at each call site.
    Otherwise every ``model_dump()`` carries the default ``user_prompt: ""``,
    and whatever re-validates that dump -- the cloud worker, parsing the payload
    the server sent -- sees the field as set and rejects the suite. Excluding it
    here means no serialization path can leak it.
    """

    user_prompt: str = Field(default="", exclude=True)

    @model_validator(mode="after")
    def _prompt_comes_from_the_cases(self) -> SuiteRunConfig:
        if "user_prompt" in self.model_fields_set:
            raise BadRequestError(
                "A suite's run_config cannot set user_prompt: each case's `input` is the prompt",
                code="suite_user_prompt",
            )
        return self

    def for_case(self, case: SuiteCase) -> RunRequest:
        """The run one execution of ``case`` performs. Never streamed."""
        settings = self.model_dump(exclude={"user_prompt", "stream"})
        return RunRequest(**settings, user_prompt=case.input, stream=False)


class SuiteCase(BaseModel):
    """One test: a prompt, and what a good answer looks like.

    ``expected`` is a reference answer the judge compares against (facts, not
    wording). ``criteria`` is what *this* case must satisfy, in plain language,
    added to the suite's rubric for this case alone. Either, both, or neither --
    a case with neither is judged on the rubric by itself.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    input: str = Field(min_length=1)
    expected: str | None = None
    criteria: str | None = None


class Suite(BaseModel):
    """A named set of cases, run against one shared configuration."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    run_config: SuiteRunConfig
    cases: list[SuiteCase] = Field(min_length=1)
    #: How many times each case runs. More than one exposes a case that passes
    #: only some of the time; the case's score is the mean across repeats.
    repeats: int = Field(default=1, ge=1, le=MAX_SUITE_REPEATS)
    pass_threshold: float = Field(default=DEFAULT_PASS_THRESHOLD, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _cases_are_distinct_and_bounded(self) -> Suite:
        counts = Counter(case.id for case in self.cases)
        duplicates = sorted(case_id for case_id, count in counts.items() if count > 1)
        if duplicates:
            raise BadRequestError(
                f"Case ids must be unique within a suite; repeated: {', '.join(duplicates)}",
                detail={"duplicates": duplicates},
                code="suite_duplicate_case",
            )
        if len(self.cases) > MAX_SUITE_CASES:
            raise BadRequestError(
                f"A suite may have at most {MAX_SUITE_CASES} cases; this one has {len(self.cases)}",
                detail={"cases": len(self.cases), "max_cases": MAX_SUITE_CASES},
                code="suite_too_large",
            )
        if self.planned_runs > MAX_SUITE_RUNS:
            raise BadRequestError(
                f"{len(self.cases)} cases x {self.repeats} repeats is {self.planned_runs} runs; "
                f"a suite may make at most {MAX_SUITE_RUNS}",
                detail={"runs": self.planned_runs, "max_runs": MAX_SUITE_RUNS},
                code="suite_too_large",
            )
        return self

    @property
    def planned_runs(self) -> int:
        return len(self.cases) * self.repeats


class EvaluationRequest(BaseModel):
    """Body of ``POST /api/v1/evaluations``.

    ``kind="determinism"``
        Execute ``run_config`` ``n`` times and grade the batch for determinism.
    ``kind="grade"``
        Grade the already-stored runs named by ``run_ids``.
    ``kind="suite"``
        Run every case of ``suite`` (``repeats`` times each) and grade each
        answer against that case's expectations. See ``docs/suites.md``.

    Unknown fields are ignored (same policy as ``RunRequest``), and ``n`` is
    clamped into ``[2, 25]`` rather than rejected.
    """

    model_config = ConfigDict(extra="ignore")

    kind: Literal["determinism", "grade", "suite"]
    run_config: RunRequest | None = None
    suite: Suite | None = None
    n: int = DEFAULT_DETERMINISM_RUNS
    run_ids: list[str] = Field(default_factory=list)
    rubric: str | None = None
    grader: GraderConfig = Field(default_factory=GraderConfig)
    #: Which lane executes this evaluation -- in-process ("local", the default)
    #: or the worker Lambda ("cloud"). See ``docs/cloud-evals.md``.
    execution: Literal["local", "cloud"] = "local"
    #: Which front door started it: the CLI, the web UI, or anything else
    #: calling the API directly. Descriptive only -- nothing branches on it --
    #: and stored with the evaluation so its history can say where it came from.
    source: Literal["cli", "ui", "api"] = "api"

    @model_validator(mode="after")
    def _check_kind_requirements(self) -> EvaluationRequest:
        if self.kind == "determinism":
            if self.run_config is None:
                raise BadRequestError("run_config is required when kind is 'determinism'")
            # The runs are consumed internally, never streamed to a client.
            self.run_config = self.run_config.model_copy(update={"stream": False})
            self.n = max(MIN_RUNS, min(self.n, MAX_RUNS))
        elif self.kind == "suite":
            if self.suite is None:
                raise BadRequestError("suite is required when kind is 'suite'")
        elif not self.run_ids:
            raise BadRequestError("run_ids is required when kind is 'grade'")
        return self

    def stored_config(self) -> dict:
        """The ``config`` JSON persisted on the evaluation row."""
        config = {
            "kind": self.kind,
            "n": self.planned_runs,
            "run_config": self.run_config.model_dump() if self.run_config else None,
            "rubric": self.rubric,
            "grader": self.grader.model_dump(),
            "source": self.source,
        }
        if self.suite is not None:
            # The whole suite, so a stored evaluation says exactly what was tested.
            config["suite"] = self.suite.model_dump()
            config["run_config"] = config["suite"]["run_config"]
        return config

    @property
    def planned_runs(self) -> int:
        """How many runs this evaluation will report on."""
        if self.kind == "determinism":
            return self.n
        if self.kind == "suite":
            assert self.suite is not None
            return self.suite.planned_runs
        return len(self.run_ids)
