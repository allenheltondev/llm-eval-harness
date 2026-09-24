"""Request models for ``POST /api/v1/runs``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nimbus.errors import BadRequestError
from nimbus.providers import DEFAULT_PROVIDER, Provider


class InferenceConfig(BaseModel):
    """Sampling knobs forwarded to the model provider (all optional)."""

    model_config = ConfigDict(extra="forbid")

    temperature: float | None = Field(default=None, ge=0.0, le=1.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, ge=1)

    def as_model_config(self) -> dict[str, float | int]:
        """Only the keys the caller actually set, ready to splat into a model config."""
        return self.model_dump(exclude_none=True)


class GuardrailConfig(BaseModel):
    """Bedrock guardrail selection for this run."""

    model_config = ConfigDict(extra="ignore")

    id: str
    version: str = "DRAFT"
    trace: bool = True


class RunRequest(BaseModel):
    """Body of ``POST /api/v1/runs``. Unknown fields are a 422."""

    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1)
    #: Which SDK executes this run. ``model_id`` is interpreted in that
    #: provider's namespace, so it is the ``source`` the model came back with
    #: from ``GET /models`` -- not a free choice.
    provider: Provider = DEFAULT_PROVIDER
    system_prompt: str = ""
    user_prompt: str = Field(min_length=1)
    inference: InferenceConfig = Field(default_factory=InferenceConfig)
    #: Name of a registered toolset (``GET /tools``) the agent may call during
    #: this run. ``None`` runs without tools.
    toolset: str | None = None
    max_tool_iterations: int = Field(default=10, ge=1, le=100)
    guardrail: GuardrailConfig | None = None
    stream: bool = True

    @model_validator(mode="after")
    def _guardrails_are_bedrock_only(self) -> RunRequest:
        """Guardrails are an AWS service, not a portable inference setting.

        Rejected up front rather than silently dropped: a run that quietly loses
        its guardrail is worse than one that never starts.
        """
        if self.guardrail is not None and self.provider != "bedrock":
            raise BadRequestError(
                "Guardrails are a Bedrock feature and cannot be applied to "
                f"provider {self.provider!r}",
                detail={"provider": self.provider, "guardrail_id": self.guardrail.id},
                code="guardrail_requires_bedrock",
            )
        return self

    def stored_config(self) -> dict:
        """The ``config`` JSON persisted on the run row."""
        return {
            "provider": self.provider,
            "inference": self.inference.as_model_config(),
            "toolset": self.toolset,
            "max_tool_iterations": self.max_tool_iterations,
            "guardrail": self.guardrail.model_dump() if self.guardrail else None,
            "stream": self.stream,
        }
