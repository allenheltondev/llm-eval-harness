"""The Updraft ``Job Finished`` event, v1 (software-factory-cli #110).

Updraft owns this contract; this module is Nimbus's reading of it. Two rules
make it safe to read a contract someone else evolves:

* **Unknown fields are ignored.** v1 changes are additive by agreement, so a
  field Updraft adds later must not fail ingestion here.
* **Only the identity is required.** Everything that describes the job is
  optional, because events published before the step-model fields existed
  (software-factory-cli #111) still carry a real outcome worth keeping. A
  comparison decides what it can use; ingestion keeps what arrived.

A ``schemaVersion`` other than 1 is rejected rather than guessed at: a v2 is by
definition not additive, so reading it as v1 could store wrong numbers.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: The EventBridge envelope's ``source`` and ``detail-type`` this stack consumes.
EVENT_SOURCE = "updraft.cloud"
EVENT_DETAIL_TYPE = "Job Finished"
SCHEMA_VERSION = 1

#: Bounds on what one event may carry, so a malformed or hostile event cannot
#: make an item that DynamoDB refuses (400 KB) or a comparison has to wade through.
MAX_STEPS = 24
_NAME = Field(default=None, max_length=256)


class _Lenient(BaseModel):
    """Ignore fields this version of Nimbus does not know about."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class JobStep(_Lenient):
    """One step of a job: its own work, then reviewer runs and revisions."""

    kind: str = Field(max_length=64)
    #: The agent profile, as the tenant's config names it.
    agent: str | None = _NAME
    #: claude, codex, kiro.
    adapter: str | None = _NAME
    #: The model the agent ran as, as passed to ``--model`` (or as the response
    #: reported it, when they differ). Absent on events from before #111.
    model: str | None = _NAME
    effort: str | None = Field(default=None, max_length=64)
    outcome: str | None = Field(default=None, max_length=64)
    verification_passed: bool | None = Field(default=None, alias="verificationPassed")
    publication: str | None = Field(default=None, max_length=64)


class PullRequest(_Lenient):
    url: str | None = Field(default=None, max_length=2048)
    number: int | None = Field(default=None, ge=0)


class Loop(_Lenient):
    """Where the review loop left the job's pull request."""

    rounds: int | None = Field(default=None, ge=0)
    reviews: int | None = Field(default=None, ge=0)
    verdict: str | None = Field(default=None, max_length=64)
    merged: bool | None = None


class Routing(_Lenient):
    """How the job's agent was chosen.

    ``source`` is what makes an observational comparison honest: ``tier`` means
    the router picked the model *because of* the issue's difficulty or risk, so
    those jobs are not a fair sample of what each model can do.
    """

    source: str | None = Field(default=None, max_length=64)
    pool: str | None = _NAME
    tier: str | None = Field(default=None, max_length=64)
    risk: str | None = Field(default=None, max_length=64)


class Telemetry(_Lenient):
    """The whole job's usage, across every step."""

    input_tokens: int | None = Field(default=None, ge=0, alias="inputTokens")
    output_tokens: int | None = Field(default=None, ge=0, alias="outputTokens")
    cost_usd: float | None = Field(default=None, ge=0, alias="costUsd")
    tool_calls: int | None = Field(default=None, ge=0, alias="toolCalls")
    tool_errors: int | None = Field(default=None, ge=0, alias="toolErrors")
    duration_ms: int | None = Field(default=None, ge=0, alias="durationMs")


class Benchmark(_Lenient):
    """Set when a benchmark dispatched the job (software-factory-cli #112)."""

    run_id: str | None = Field(default=None, max_length=128, alias="runId")
    case_id: str | None = Field(default=None, max_length=256, alias="caseId")
    arm: str | None = _NAME


class JobFinished(_Lenient):
    """The ``detail`` of one ``Job Finished`` event."""

    schema_version: Literal[1] = Field(alias="schemaVersion")
    #: The Updraft stack that ran the job. Part of the job's identity here, so
    #: a staging and a production deployment can share this table.
    stack: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    tenant_id: str = Field(min_length=1, max_length=128, alias="tenantId")
    job_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$", alias="jobId"
    )
    kind: str | None = Field(default=None, max_length=64)
    repository: str | None = Field(default=None, max_length=256)
    issue: int | None = Field(default=None, ge=0)
    pull_request: int | None = Field(default=None, ge=0, alias="pullRequest")
    created_at: str | None = Field(default=None, max_length=64, alias="createdAt")
    finished_at: str | None = Field(default=None, max_length=64, alias="finishedAt")
    duration_seconds: float | None = Field(default=None, ge=0, alias="durationSeconds")
    config_version: int | None = Field(default=None, ge=0, alias="configVersion")
    status: str | None = Field(default=None, max_length=64)
    outcome: str | None = Field(default=None, max_length=64)
    updraft_outcome: str | None = Field(default=None, max_length=64, alias="updraftOutcome")
    verification_passed: bool | None = Field(default=None, alias="verificationPassed")
    pr: PullRequest | None = None
    loop: Loop | None = None
    steps: list[JobStep] = Field(default_factory=list, max_length=MAX_STEPS)
    routing: Routing | None = None
    telemetry: Telemetry | None = None
    retried_as: str | None = Field(default=None, max_length=128, alias="retriedAs")
    benchmark: Benchmark | None = None

    def stored(self) -> dict[str, Any]:
        """The job as Nimbus stores and serves it: snake_case, nulls dropped."""
        return self.model_dump(mode="json", exclude_none=True)
