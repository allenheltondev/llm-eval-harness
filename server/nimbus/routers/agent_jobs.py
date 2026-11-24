"""``/agent-jobs``: finished Updraft jobs, and how their models compare.

Read-only. The jobs are written by the ingest Lambda (:mod:`nimbus.agentjobs.lambda_app`)
into the shared table; a server without that table (``NIMBUS_EVAL_TABLE`` unset,
e.g. a laptop on SQLite) has none to serve and answers 400 ``agent_jobs_unavailable``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from nimbus.agentjobs.compare import DEFAULT_KIND, GroupBy, compare_jobs
from nimbus.agentjobs.store import AgentJobStore
from nimbus.config import Settings, get_settings
from nimbus.errors import AppError, BadRequestError, NotFoundError
from nimbus.evals.ddb_reader import build_table

router = APIRouter(tags=["agent-jobs"])

#: The comparison's window when none is given, and the longest one allowed.
DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 366
#: ``?kind=all``: compare every kind mixed together (an explicit opt-in).
ALL_KINDS = "all"
#: The kinds ``?kind=`` accepts: Updraft's job kinds (src/db/stats.ts), or
#: ``all``. Anything else -- a blank ``?kind=`` included -- is a 422, so mixing
#: kinds can only ever be asked for by name.
KindParam = Literal["implement", "revision", "review", "all"]


class AgentJobsUnavailableError(AppError):
    status_code = 400
    code = "agent_jobs_unavailable"


def get_agent_job_store(settings: Settings = Depends(get_settings)) -> AgentJobStore | None:
    """FastAPI dependency: the store, or ``None`` when this server has no table."""
    if not settings.eval_table:
        return None
    return AgentJobStore(
        build_table(settings.eval_table, settings.aws_region),
        retention_days=settings.history_retention_days,
    )


def _require(store: AgentJobStore | None) -> AgentJobStore:
    if store is None:
        raise AgentJobsUnavailableError(
            "Agent jobs are stored in the deployed table; this server has none configured"
        )
    return store


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@router.get("/agent-jobs")
def list_agent_jobs(
    cursor: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> dict[str, Any]:
    """Finished agent jobs, newest first."""
    items, next_cursor = _require(store).page(limit=limit, cursor=cursor)
    return {"items": items, "next_cursor": next_cursor}


@router.get("/agent-jobs/compare")
def compare_agent_jobs(
    since: datetime | None = None,
    until: datetime | None = None,
    group_by: GroupBy = "model",
    repository: str | None = None,
    tenant_id: str | None = None,
    stack: str | None = None,
    kind: KindParam = Query(
        default=DEFAULT_KIND,
        description="Job kind to compare (implement, revision, review), or `all` to mix them.",
    ),
    include_routed: bool = False,
    include_benchmarks: bool = False,
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> dict[str, Any]:
    """Compare the models of jobs finished in a window (default: the last 30 days)."""
    store = _require(store)
    end = _aware(until) if until else datetime.now(UTC)
    start = _aware(since) if since else end - timedelta(days=DEFAULT_WINDOW_DAYS)
    if start > end:
        raise BadRequestError(
            "`since` is after `until`",
            detail={"since": start.isoformat(), "until": end.isoformat()},
        )
    if end - start > timedelta(days=MAX_WINDOW_DAYS):
        raise BadRequestError(f"The window is at most {MAX_WINDOW_DAYS} days")

    jobs, truncated = store.between(start, end)
    comparison = compare_jobs(
        jobs,
        group_by=group_by,
        include_routed=include_routed,
        include_benchmarks=include_benchmarks,
        repository=repository,
        tenant_id=tenant_id,
        stack=stack,
        kind=None if kind == ALL_KINDS else kind,
    )
    comparison["window"] = {"since": start.isoformat(), "until": end.isoformat()}
    comparison["truncated"] = truncated
    if truncated:
        comparison["warnings"].append(
            "The window held more jobs than one comparison reads; the newest were left out. "
            "Narrow it with since/until."
        )
    return comparison


@router.get("/agent-jobs/{job_ref:path}")
def get_agent_job(
    job_ref: str,
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> dict[str, Any]:
    """One job by its ``{stack}#{jobId}`` id (URL-encode the ``#`` as ``%23``)."""
    record = _require(store).get(job_ref)
    if record is None:
        raise NotFoundError("Agent job not found", detail={"id": job_ref})
    return record
