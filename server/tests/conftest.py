"""Shared pytest fixtures for the evalharness test suite."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from evalharness import auth, models_catalog, stack_discovery
from evalharness.errors import BadRequestError
from evalharness.main import create_app

#: Every env var that can switch a non-Bedrock provider on. Cleared for every
#: test so the suite behaves identically on a laptop that happens to export
#: ANTHROPIC_API_KEY or run an Ollama -- no test may reach the network.
PROVIDER_ENV_VARS = (
    "EVALHARNESS_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "EVALHARNESS_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "EVALHARNESS_OLLAMA_BASE_URL",
    "OLLAMA_HOST",
)

#: The two settings that switch bearer-token auth on (evalharness.auth). Also
#: cleared per test: a developer with a deployed stack's values exported must
#: not see every router test fail with 401.
AUTH_ENV_VARS = ("EVALHARNESS_AUTH_USER_POOL_ID", "EVALHARNESS_AUTH_CLIENT_ID")


@pytest.fixture(autouse=True)
def isolated_providers(monkeypatch):
    """No provider is configured, and no listing is carried between tests."""
    for name in PROVIDER_ENV_VARS + AUTH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    models_catalog.catalog.clear()
    auth.jwks_cache.clear()
    yield
    models_catalog.catalog.clear()
    auth.jwks_cache.clear()


@pytest.fixture(autouse=True)
def isolated_stack_discovery(monkeypatch):
    """No test talks to CloudFormation/API Gateway unless it opts in.

    ``EVALHARNESS_STACK_DISCOVERY`` defaults on in real usage, but every test
    that doesn't explicitly enable it (via ``Settings(stack_discovery=True)``
    or by overriding this env var) must stay offline -- the sandbox's AWS
    credentials are real enough for boto3 to attempt a live call otherwise.
    The process-lifetime cache is also cleared on both sides so no test's
    discovery result leaks into another's.
    """
    monkeypatch.setenv("EVALHARNESS_STACK_DISCOVERY", "false")
    stack_discovery.refresh()
    yield
    stack_discovery.refresh()


@pytest.fixture(autouse=True)
def isolated_tool_state():
    """Reset the ported tools' module-level "in-memory database" dicts.

    ``fraud_detection._ACCOUNT_RISK`` and ``shipping_logistics._ACTIONS`` are
    intentional process-lifetime state (they mirror what the legacy JS tools
    kept in memory), so a plain single ``pytest`` process never notices: every
    test's account/idempotency-key literal is exercised at most once per run.
    That assumption breaks under anything that re-executes the suite within
    one interpreter without restarting it -- e.g. mutmut's in-process test
    runner, which runs a "gather stats" pass and then a "clean run" pass back
    to back -- surfacing as a test seeing another run's leftover state under
    the same literal id. Clearing both before every test removes the
    dependency on process lifetime entirely.
    """
    from evalharness.tools import fraud_detection, shipping_logistics

    fraud_detection._ACCOUNT_RISK.clear()
    shipping_logistics._ACTIONS.clear()
    yield
    fraud_detection._ACCOUNT_RISK.clear()
    shipping_logistics._ACTIONS.clear()


@pytest.fixture
def app() -> FastAPI:
    """A fresh FastAPI app instance for each test, with a test-only error route."""
    application = create_app()

    @application.get("/api/v1/_test/boom")
    async def _boom():
        raise BadRequestError("deliberate test error", detail={"field": "value"})

    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An async HTTP client wired directly to the app via ASGI transport."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
