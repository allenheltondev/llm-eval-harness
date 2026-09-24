"""Application settings, sourced from the environment (NIMBUS_ prefix).

The project was called ``evalharness`` before it was Nimbus. Every
``EVALHARNESS_*`` variable is still read -- after its ``NIMBUS_*`` name, which
wins when both are set -- so an existing shell profile, ``.env`` file or
deployed stack keeps working across the rename.
"""

import os
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

#: The environment prefix before the rename; read after ``NIMBUS_``.
LEGACY_ENV_PREFIX = "EVALHARNESS_"
#: History files kept in the working directory by earlier versions. When one
#: exists where Nimbus is started, it stays the default: a repo checkout's
#: ``server/data`` keeps its history, before and after the rename.
LOCAL_DB_PATH = "./data/nimbus.db"
LEGACY_DB_PATH = "./data/evalharness.db"
#: The history file's name inside the user data directory.
HISTORY_FILE_NAME = "history.db"


def user_data_dir() -> Path:
    """``$XDG_DATA_HOME/nimbus``, else ``~/.local/share/nimbus``.

    Where an installed ``nimbus`` keeps its history, so every directory it is
    run from shares one history instead of each growing its own ``./data``.
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "nimbus"


def default_db_path() -> str:
    """The history file used when none is named (``NIMBUS_DB_PATH`` / ``--db``)."""
    for candidate in (LOCAL_DB_PATH, LEGACY_DB_PATH):
        if Path(candidate).exists():
            return candidate
    return str(user_data_dir() / HISTORY_FILE_NAME)


class Settings(BaseSettings):
    """Runtime configuration for the nimbus server.

    All fields are overridable via environment variables prefixed with
    ``NIMBUS_`` (e.g. ``NIMBUS_AWS_REGION=us-west-2``).

    The three non-Bedrock model providers are the exception to "prefix
    everything": their credentials also have well-known standard env names
    (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ``OLLAMA_HOST``) that developers
    already have exported, so each one accepts the prefixed name *or* the
    standard one, prefixed winning.
    """

    model_config = SettingsConfigDict(
        env_prefix="NIMBUS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # The aliased provider fields would otherwise be settable only by their
        # env-var names, which makes `Settings(ollama_base_url=...)` in a test
        # silently do nothing.
        populate_by_name=True,
    )

    aws_region: str = "us-east-1"
    #: Resolved by :func:`default_db_path` when not set.
    db_path: str = ""
    cors_origins: list[str] = ["http://localhost:3000"]
    fake_model: bool = False

    # -- non-Bedrock model providers ---------------------------------------- #
    #: Anthropic API key. Presence is what makes the provider "configured".
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "NIMBUS_ANTHROPIC_API_KEY", "EVALHARNESS_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"
        ),
    )
    #: OpenAI API key. Presence is what makes the provider "configured".
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "NIMBUS_OPENAI_API_KEY", "EVALHARNESS_OPENAI_API_KEY", "OPENAI_API_KEY"
        ),
    )
    #: Base URL of an Ollama server, e.g. ``http://localhost:11434``. Deliberately
    #: has no default: an unset value means "the provider is not offered", which
    #: is different from "there might be an Ollama on the usual port".
    ollama_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "NIMBUS_OLLAMA_BASE_URL", "EVALHARNESS_OLLAMA_BASE_URL", "OLLAMA_HOST"
        ),
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
    #: :mod:`nimbus.deployment` for the full resolution matrix.
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

    # -- authentication (nimbus.auth) ---------------------------------------- #
    #: Cognito user pool the deployed server verifies bearer tokens against.
    #: The deployed template injects both of these from its own resources;
    #: unset (the local default) means every route is open.
    auth_user_pool_id: str | None = None
    #: The pool's app client id -- what the SPA signs in with, and the
    #: ``aud``/``client_id`` every accepted token must carry.
    auth_client_id: str | None = None
    #: The Cognito group a signed-in user must belong to. With a pool that
    #: several apps share -- and that anyone can sign up to -- a valid token
    #: only proves who someone is; membership of this group is what grants
    #: them this stack. Unset: every valid token is accepted.
    auth_required_group: str | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Read the pre-rename ``EVALHARNESS_*`` names after the ``NIMBUS_*`` ones."""
        legacy_env = EnvSettingsSource(settings_cls, env_prefix=LEGACY_ENV_PREFIX)
        legacy_dotenv = DotEnvSettingsSource(settings_cls, env_prefix=LEGACY_ENV_PREFIX)
        return (
            init_settings,
            env_settings,
            legacy_env,
            dotenv_settings,
            legacy_dotenv,
            file_secret_settings,
        )

    @model_validator(mode="after")
    def _default_the_history_file(self) -> "Settings":
        """Fill in the history file when none was named; a named one is taken as given."""
        if not self.db_path:
            self.db_path = default_db_path()
        return self

    @field_validator(
        "anthropic_api_key",
        "openai_api_key",
        "auth_user_pool_id",
        "auth_client_id",
        "auth_required_group",
    )
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
