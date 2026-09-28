"""Whether a caller may start an evaluation, and what it is counted against.

The router hands over the verified token claims. This module strips whatever
``principal`` the request body carried (identity is the server's to state, never
the client's), attributes the evaluation to the caller, and reserves its
estimated cost against the caller's spend window (:mod:`nimbus.spend`). The
engine settles that reservation when the evaluation ends.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from nimbus import pricing, spend
from nimbus.config import Settings
from nimbus.engine.events import MetricsEvent, RunEvent
from nimbus.engine.schemas import RunRequest
from nimbus.errors import BadRequestError
from nimbus.evals import budget
from nimbus.evals.schemas import EvaluationRequest

logger = logging.getLogger(__name__)


def estimated_cost_usd(request: EvaluationRequest) -> float:
    """A pre-flight guess at what the evaluation's runs will cost (judge spend excluded)."""
    # Imported here: the engine imports this package's schemas at load time.
    from nimbus.evals import engine

    jobs = engine.planned_jobs(request)
    return budget.prior_per_run(job.run_config for job in jobs) * len(jobs)


def admit(
    payload: EvaluationRequest,
    claims: dict[str, Any] | None,
    settings: Settings,
    store: spend.SpendStore | None = None,
) -> EvaluationRequest:
    """The request to run, attributed to the caller with its spend reserved.

    Without a verified identity (auth not configured) the request runs
    unattributed and uncounted. Raises ``402 spend_limit_reached`` when the
    reservation would pass the caller's ceiling; a refused request has cost the
    caller nothing.
    """
    payload = payload.model_copy(update={"principal": None})
    principal = spend.principal_from_claims(claims)
    if principal is None:
        return payload

    if settings.spend_limit_usd is not None:
        config = payload.suite.run_config if payload.suite is not None else payload.run_config
        if config is not None and not pricing.is_priced(config.provider, config.model_id):
            raise BadRequestError(
                f"A spend limit needs a price for {config.model_id!r}, and there is none; "
                f"add one with {pricing.PRICING_FILE_ENV} (see docs/suites.md)",
                detail={"provider": config.provider, "model_id": config.model_id},
                code="spend_model_unpriced",
            )

    store = store or spend.get_spend_store(settings)
    reserved = spend.reserve(store, settings, principal, estimated_cost_usd(payload))
    return payload.model_copy(update={"principal": reserved})


def release(
    payload: EvaluationRequest, settings: Settings, store: spend.SpendStore | None = None
) -> None:
    """Give back a reservation for an evaluation that never started."""
    if payload.principal is not None:
        spend.settle(store or spend.get_spend_store(settings), payload.principal, 0.0)


def admit_run(
    payload: RunRequest,
    claims: dict[str, Any] | None,
    settings: Settings,
    store: spend.SpendStore | None = None,
) -> spend.Principal | None:
    """Reserve a single run's estimated cost for the caller; ``None`` when unattributed.

    A direct run is counted like an evaluation, or the ceiling could be walked
    around one ``POST /runs`` at a time. Its estimate is prompt size plus
    ``max_tokens``; tool loops cost more, and settlement charges the difference.
    """
    principal = spend.principal_from_claims(claims)
    if principal is None:
        return None
    if settings.spend_limit_usd is not None and not pricing.is_priced(
        payload.provider, payload.model_id
    ):
        raise BadRequestError(
            f"A spend limit needs a price for {payload.model_id!r}, and there is none; "
            f"add one with {pricing.PRICING_FILE_ENV} (see docs/suites.md)",
            detail={"provider": payload.provider, "model_id": payload.model_id},
            code="spend_model_unpriced",
        )
    estimate = pricing.estimate_run_cost(
        payload.provider,
        payload.model_id,
        len(payload.system_prompt) + len(payload.user_prompt),
        payload.inference.max_tokens,
    )
    return spend.reserve(
        store or spend.get_spend_store(settings), settings, principal, estimate or 0.0
    )


def settle_stream(
    events: AsyncIterator[RunEvent],
    principal: spend.Principal | None,
    settings: Settings,
    store: spend.SpendStore | None = None,
) -> AsyncIterator[RunEvent]:
    """``events``, settling the run's spend when the stream ends however it ends.

    A client that disconnects, a run that errors and one that finishes all
    reach the ``finally``, so a reservation is never left standing. Accounting
    failures are logged, never raised into the caller's stream.
    """
    if principal is None:
        return events

    async def stream() -> AsyncIterator[RunEvent]:
        cost = 0.0
        try:
            async with aclosing(events):
                async for event in events:
                    if isinstance(event, MetricsEvent) and event.cost_usd:
                        cost += event.cost_usd
                    yield event
        finally:
            try:
                spend.settle(store or spend.get_spend_store(settings), principal, cost)
            except Exception:
                logger.exception("could not settle spend for a run")

    return stream()
