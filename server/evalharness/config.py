"""Application settings, sourced from the environment (EVALHARNESS_ prefix)."""

from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the evalharness server.

    All fields are overridable via environment variables prefixed with
    ``EVALHARNESS_`` (e.g. ``EVALHARNESS_AWS_REGION=us-west-2``).

    The three non-Bedrock model providers are the exception to "prefix
    everything": their credentials also have well-known standard env names
    (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ``OLLAMA_HOST``) that developers
    already have exported, so each one accepts the prefixed name *or* the
    standard one, prefixed winning.
    """

    model_config = SettingsConfigDict(
        env_prefix="EVALHARNESS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # The aliased provider fields would otherwise be settable only by their
        # env-var names, which makes `Settings(ollama_base_url=...)` in a test
        # silently do nothing.
        populate_by_name=True,
    )

    aws_region: str = "us-east-1"
    db_path: str = "./data/evalharness.db"
    cors_origins: list[str] = ["http://localhost:3000"]
    fake_model: bool = False

    # -- non-Bedrock model providers ---------------------------------------- #
    #: Anthropic API key. Presence is what makes the provider "configured".
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("EVALHARNESS_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )
    #: OpenAI API key. Presence is what makes the provider "configured".
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("EVALHARNESS_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    #: Base URL of an Ollama server, e.g. ``http://localhost:11434``. Deliberately
    #: has no default: an unset value means "the provider is not offered", which
    #: is different from "there might be an Ollama on the usual port".
    ollama_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("EVALHARNESS_OLLAMA_BASE_URL", "OLLAMA_HOST"),
    )

    # -- the cloud evaluation lane (docs/cloud-evals.md) -------------------- #
    #: Name (or ARN) of the evaluation worker Lambda. None = lane unavailable.
    #: The deployed template injects it; locally, copy it from the stack's
    #: ``EvalWorkerFunctionName`` output.
    eval_function_name: str | None = None
    #: DynamoDB table holding cloud evaluation state and deployed history
    #: (the stack's ``TableName`` output). None = lane unavailable.
    eval_table: str | None = None

    # -- deployment shape (docs/serverless-deploy.md) ----------------------- #
    #: Where run/evaluation history lives. ``auto`` picks ``dynamodb`` inside
    #: Lambda and ``sqlite`` everywhere else; see
    #: :mod:`evalharness.deployment` for the full resolution matrix.
    history_backend: Literal["sqlite", "dynamodb", "auto"] = "auto"
    #: Whether ``POST /evaluations`` may execute an evaluation in this process.
    #: ``auto`` means "off inside Lambda, on everywhere else" -- a deployed
    #: server has no durable place to run a multi-minute job.
    local_evals: Literal["auto", "on", "off"] = "auto"
    #: How long deployed history (evaluations and their runs, in DynamoDB) is
    #: kept, in days. ``0`` keeps it forever. Set per stack by the
    #: ``HistoryRetentionDays`` template parameter; SQLite history is never
    #: expired by the harness.
    history_retention_days: int = Field(default=0, ge=0)

    # -- authentication (evalharness.auth) ---------------------------------- #
    #: Cognito user pool the deployed server verifies bearer tokens against.
    #: The deployed template injects both of these from its own resources;
    #: unset (the local default) means every route is open.
    auth_user_pool_id: str | None = None
    #: The pool's app client id -- what the SPA signs in with, and the
    #: ``aud``/``client_id`` every accepted token must carry.
    auth_client_id: str | None = None

    @field_validator("anthropic_api_key", "openai_api_key", "auth_user_pool_id", "auth_client_id")
    @classmethod
    def _blank_key_is_unset(cls, value: str | None) -> str | None:
        """``FOO_API_KEY=`` in a shell profile means unset, not "empty key"."""
        if value is None:
            return None
        return value.strip() or None

    @field_validator("ollama_base_url")
    @classmethod
    def _normalize_ollama_base_url(cls, value: str | None) -> str | None:
        """Accept ``OLLAMA_HOST``'s schemeless ``host:port`` form as a base URL."""
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if "://" not in value:
            value = f"http://{value}"
        return value.rstrip("/")


def get_settings() -> Settings:
    """Return a fresh Settings instance (re-reads the environment)."""
    return Settings()
