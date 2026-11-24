"""The EventBridge target that stores Updraft's ``Job Finished`` events.

Invoked asynchronously by the rule ``AgentJobIngestFunction`` subscribes to
(``infra/template.yaml``). Every failure *raises*, on purpose:

* A transient one (DynamoDB throttled or unreachable) is retried by Lambda, and
  the retry is safe because storing is an idempotent ``PutItem`` keyed on the
  job (:mod:`nimbus.agentjobs.store`).
* An event this version cannot read -- the wrong source, a ``schemaVersion``
  other than 1, a missing identity -- will not read any better on a retry, but
  dropping it silently would lose a job nobody knew was lost. After its retries
  it lands on the function's dead-letter queue, whose alarm is the signal that
  Updraft's contract moved.

The handler logs identities and outcomes only, never the job's contents.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from pydantic import ValidationError

from nimbus.agentjobs.schemas import EVENT_DETAIL_TYPE, EVENT_SOURCE, JobFinished, is_synthetic
from nimbus.agentjobs.store import AgentJobStore
from nimbus.store import ddb_items

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_store: AgentJobStore | None = None


class UnreadableEventError(Exception):
    """An event this version of Nimbus cannot store. Raised so it reaches the DLQ."""


def build_store() -> AgentJobStore:
    """The store for this function's environment (``TABLE_NAME``, retention)."""
    region = os.environ.get("NIMBUS_AWS_REGION") or os.environ.get("AWS_REGION")
    table = boto3.resource("dynamodb", region_name=region).Table(os.environ["TABLE_NAME"])
    retention = int(os.environ.get("NIMBUS_HISTORY_RETENTION_DAYS") or ddb_items.KEEP_FOREVER)
    return AgentJobStore(table, retention_days=retention)


def _get_store() -> AgentJobStore:
    global _store
    if _store is None:
        _store = build_store()
    return _store


def parse(event: dict[str, Any]) -> JobFinished:
    """The ``Job Finished`` detail of an EventBridge event, or raise."""
    source = event.get("source")
    detail_type = event.get("detail-type")
    if source != EVENT_SOURCE or detail_type != EVENT_DETAIL_TYPE:
        raise UnreadableEventError(
            f"not a {EVENT_SOURCE}/{EVENT_DETAIL_TYPE} event: {source!r}/{detail_type!r}"
        )
    detail = event.get("detail")
    if isinstance(detail, str):
        detail = json.loads(detail)
    try:
        return JobFinished.model_validate(detail)
    except ValidationError as exc:
        # Field locations and error types only: the input may hold repository
        # names and the like, which do not belong in a log line.
        problems = [f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors()]
        raise UnreadableEventError(f"unreadable Job Finished detail: {problems}") from None


def handle(
    event: dict[str, Any], store: AgentJobStore, own_stack: str | None = None
) -> dict[str, Any]:
    """Store one event. Separate from :func:`handler` so tests pass a store.

    A synthetic (smoke-test) event is stored only by the stack it targets: every
    Nimbus stack in the account receives it from the default bus, and the others
    must neither keep it nor fail on it.
    """
    job = parse(event)
    if is_synthetic(job.stack) and (own_stack is None or job.smoke_target != own_stack):
        logger.info(
            json.dumps({"skipped": event.get("id"), "reason": "synthetic for another stack"})
        )
        return {"skipped": job.job_id}
    item = store.put(job)
    logger.info(
        json.dumps(
            {
                "stored": item["id"],
                "event_id": event.get("id"),
                "outcome": job.outcome,
                "model": item["model"],
            }
        )
    )
    return {"stored": item["id"]}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ARG001 - Lambda signature
    """Lambda entry point."""
    try:
        return handle(event, _get_store(), os.environ.get("STACK_NAME"))
    except UnreadableEventError as exc:
        logger.error(json.dumps({"rejected": event.get("id"), "reason": str(exc)}))
        raise
