"""FastAPI application factory for the nimbus server."""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from nimbus import deployment
from nimbus.auth import require_auth
from nimbus.config import get_settings
from nimbus.errors import register_exception_handlers
from nimbus.routers import guardrails, health, models, runs, tools
from nimbus.store.db import init_db
from nimbus.store.repo import get_history_repo

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepare the history backend this deployment resolved to.

    SQLite gets its file and schema created; DynamoDB gets its repository built
    eagerly so a missing table fails the *startup* rather than the first request
    that touches history. ``init_db`` is deliberately not called in that case:
    inside Lambda the working directory is read-only, and there is nothing to
    create anyway.
    """
    settings = get_settings()
    if deployment.history_backend(settings) == "sqlite":
        init_db(settings.db_path)
    else:
        get_history_repo(settings)
    yield


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(title="Nimbus Server", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    # /health stays open: it is where the SPA learns whether sign-in is
    # required and which user pool to use (nimbus.auth). Everything
    # else needs a bearer token whenever a pool is configured.
    app.include_router(health.router, prefix=API_PREFIX)
    protected = [Depends(require_auth)]
    app.include_router(models.router, prefix=API_PREFIX, dependencies=protected)
    app.include_router(tools.router, prefix=API_PREFIX, dependencies=protected)
    app.include_router(runs.router, prefix=API_PREFIX, dependencies=protected)
    app.include_router(guardrails.router, prefix=API_PREFIX, dependencies=protected)

    return app


app = create_app()
