"""What is set up on this machine, and what to do about what is not.

Shared by ``nimbus models`` (which explains an empty or partial listing) and
``nimbus doctor`` (which checks everything). Each check says what it found and,
when it found a problem, the one thing to do next -- the command to run or the
variable to set -- rather than an exception's repr.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nimbus import providers
from nimbus.cli import remote
from nimbus.config import Settings
from nimbus.models_catalog import CatalogResult

#: ``ok`` works; ``off`` is optional and not set up; ``fail`` is set up (or
#: required) and broken. Only ``fail`` makes ``doctor`` exit non-zero.
Status = Literal["ok", "off", "fail"]

#: What switches each non-Bedrock provider on.
_PROVIDER_SETTING = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "ollama": "OLLAMA_HOST",
}


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    #: The next thing to do, when there is one.
    fix: str | None = None


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _bedrock_fix(error: str) -> str:
    if "Expired" in error or "expired" in error:
        return "your AWS session expired: `aws sso login` (or refresh your credentials)"
    if "AccessDenied" in error:
        return "the AWS identity needs bedrock:ListFoundationModels (and model access)"
    return "check your AWS credentials: `aws sts get-caller-identity`, AWS_PROFILE"


def bedrock_check(settings: Settings, result: CatalogResult) -> Check:
    credentials = providers.aws_credentials_status()
    count = sum(1 for entry in result.models if entry.get("source") == "bedrock")
    if credentials == "missing":
        return Check(
            "bedrock",
            "off",
            "no AWS credentials found",
            "`aws configure` or `aws sso login`, or set AWS_PROFILE",
        )
    if credentials == "error":
        return Check(
            "bedrock",
            "fail",
            "AWS credentials could not be read",
            "check ~/.aws/config and AWS_PROFILE",
        )
    if result.bedrock_error:
        return Check("bedrock", "fail", result.bedrock_error, _bedrock_fix(result.bedrock_error))
    return Check("bedrock", "ok", f"{_plural(count, 'model')} in {settings.aws_region}")


def provider_checks(settings: Settings, result: CatalogResult) -> list[Check]:
    """One check per model provider, from a catalog listing already collected."""
    checks = [bedrock_check(settings, result)]
    for name, variable in _PROVIDER_SETTING.items():
        count = sum(1 for entry in result.models if entry.get("source") == name)
        block = result.providers.get(name) or {}
        if not block.get("configured"):
            checks.append(Check(name, "off", "not configured", f"set {variable}"))
        elif name == "ollama" and not block.get("reachable", True):
            where = providers.credential_for("ollama", settings)
            checks.append(
                Check(name, "fail", f"not answering at {where}", "start it with `ollama serve`")
            )
        elif count == 0:
            checks.append(
                Check(name, "fail", "configured, but listed no models", f"check {variable}")
            )
        else:
            checks.append(Check(name, "ok", _plural(count, "model")))
    return checks


def history_check(settings: Settings) -> Check:
    """Whether the history store can be written, without opening it."""
    backend = settings.history_backend
    if backend == "dynamodb":
        table = settings.eval_table or "(NIMBUS_EVAL_TABLE is not set)"
        return Check("history", "ok" if settings.eval_table else "fail", f"DynamoDB {table}")
    path = Path(settings.db_path).expanduser()
    if settings.db_path == ":memory:":
        return Check("history", "ok", "in memory (not kept)")
    existing = (
        path
        if path.exists()
        else next((parent for parent in path.parents if parent.exists()), Path("/"))
    )
    if not os.access(existing, os.W_OK):
        return Check(
            "history",
            "fail",
            f"{path} is not writable",
            "set NIMBUS_DB_PATH (or pass --db) to a writable file",
        )
    return Check("history", "ok", str(path))


async def stack_checks(login: remote.Login | None) -> list[Check]:
    """The signed-in stack: reachable, still accepting the sign-in, and what it offers."""
    if login is None or not login.signed_in:
        return [
            Check(
                "stack",
                "off",
                "not signed in; commands run on this machine",
                "`nimbus login --url https://<your-stack>` to run on a deployed stack",
            )
        ]
    who = f" as {login.email}" if login.email else ""
    try:
        async with remote.http_client() as http:
            api = remote.RemoteApi(http, login)
            health = await api.health()
            await api.get("/runs", {"limit": 1})
            listed = await api.get("/models")
    except remote.NotSignedInError as exc:
        return [Check("stack", "fail", exc.message, "`nimbus login` to sign in again")]
    except remote.RemoteError as exc:
        return [Check("stack", "fail", exc.message, f"check that {login.url} is up")]
    lane = "cloud" if (health.get("cloud_evals") or {}).get("configured") else "server"
    count = len(listed.get("models") or [])
    detail = f"{login.url}{who}: {lane} evaluations, {_plural(count, 'model')}"
    if count == 0:
        fix = "the stack's AWS account needs Bedrock model access"
        return [Check("stack", "fail", detail, fix)]
    return [Check("stack", "ok", detail)]
