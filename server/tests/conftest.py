"""Shared pytest fixtures for the nimbus test suite."""

import os
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from nimbus import auth, models_catalog
from nimbus.errors import BadRequestError
from nimbus.main import create_app

#: Every env var that can switch a non-Bedrock provider on. Cleared for every
#: test so the suite behaves identically on a laptop that happens to export
#: ANTHROPIC_API_KEY or run an Ollama -- no test may reach the network.
PROVIDER_ENV_VARS = (
    "NIMBUS_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "NIMBUS_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "NIMBUS_OLLAMA_BASE_URL",
    "OLLAMA_HOST",
    # The pre-rename names, still read (nimbus.config).
    "EVALHARNESS_ANTHROPIC_API_KEY",
    "EVALHARNESS_OPENAI_API_KEY",
    "EVALHARNESS_OLLAMA_BASE_URL",
)

#: The two settings that switch bearer-token auth on (nimbus.auth). Also
#: cleared per test: a developer with a deployed stack's values exported must
#: not see every router test fail with 401.
AUTH_ENV_VARS = (
    "NIMBUS_AUTH_USER_POOL_ID",
    "NIMBUS_AUTH_CLIENT_ID",
    "EVALHARNESS_AUTH_USER_POOL_ID",
    "EVALHARNESS_AUTH_CLIENT_ID",
)


@pytest.fixture(autouse=True)
def restored_environment():
    """Put ``os.environ`` back after every test.

    ``nimbus --db`` exports NIMBUS_DB_PATH and NIMBUS_HISTORY_BACKEND into the
    real environment on purpose (it is the only channel ``serve``'s uvicorn
    child can read), so any test that invokes the CLI with ``--db`` would
    otherwise leave the next test running with a pinned SQLite backend.
    """
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def isolated_user_data(monkeypatch, tmp_path):
    """The default history file and the saved login live in the test's own directory.

    Never in ~: a developer signed in to a stack would otherwise have every
    local-CLI test quietly run against that stack.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("NIMBUS_CONFIG_DIR", str(tmp_path / "nimbus-config"))
    monkeypatch.delenv("EVALHARNESS_CONFIG_DIR", raising=False)


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
def isolated_tool_state():
    """Reset the ported tools' module-level "in-memory database" dicts.

    ``fraud_detection._ACCOUNT_RISK`` is
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
    from nimbus.tools import fraud_detection

    fraud_detection._ACCOUNT_RISK.clear()
    yield
    fraud_detection._ACCOUNT_RISK.clear()


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
