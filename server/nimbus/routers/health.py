"""Health check endpoint."""

import logging
from typing import Any

from fastapi import APIRouter, Depends

from nimbus import auth, deployment
from nimbus.config import Settings, get_settings
from nimbus.evals import cloud as evals_cloud
from nimbus.models_catalog import catalog as provider_catalog
from nimbus.providers import CredentialStatus, aws_credentials_status

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

#: The same check ``providers.bedrock.configured`` is derived from, kept under
#: its original name because ``aws.credentials`` reports all three states.
_check_aws_credentials = aws_credentials_status


async def _check_providers(settings: Settings) -> dict[str, Any]:
    """The ``providers`` block, identical to the one ``GET /models`` returns.

    Shares the catalog singleton, so a health poll reuses (and warms) the same
    per-provider caches rather than re-probing Ollama every few seconds.
    """
    return await provider_catalog.provider_status(settings)


@router.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Report overall service health. This endpoint should never raise."""
    credentials: CredentialStatus
    try:
        credentials = _check_aws_credentials()
    except Exception:
        logger.exception("Unexpected error during AWS credentials check")
        credentials = "error"

    try:
        provider_block = await _check_providers(settings)
    except Exception:
        logger.exception("Unexpected error during provider check")
        provider_block = {
            "bedrock": {"configured": credentials == "ok"},
            "anthropic": {"configured": False},
            "openai": {"configured": False},
            "ollama": {"configured": False, "reachable": None},
        }

    return {
        "status": "ok",
        "aws": {
            "region": settings.aws_region,
            "credentials": credentials,
        },
        # Which model providers this server can actually run against -- the same
        # object `GET /models` returns, so the UI can read it from either.
        "providers": provider_block,
        # Both a worker function name and a DynamoDB table are needed before
        # the UI may offer "Cloud — persisted" (docs/cloud-evals.md).
        "cloud_evals": {
            "configured": evals_cloud.is_configured(settings),
        },
        # Whether this server may execute an evaluation in its own process. A
        # deployed (Lambda) server cannot, so the UI disables "This machine" and
        # defaults to the cloud lane (docs/serverless-deploy.md).
        "local_evals": {
            "available": deployment.local_evals_available(settings),
        },
        # Whether every other route wants a bearer token, and (when it does)
        # the Cognito pool the SPA signs in against. This endpoint is the one
        # deliberately left open so that a signed-out browser can read it.
        "auth": auth.health_block(settings),
    }
