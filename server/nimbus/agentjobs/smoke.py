"""End-to-end smoke test of agent-job ingestion, with no Updraft job needed (#68).

It proves the harness half of the integration on a deployed stack:

1. ``PutEvents`` one synthetic v1 ``Job Finished`` event onto Updraft's bus --
   the same ``source``/``detail-type`` Updraft's Pipe publishes, so the stack's
   rule cannot tell it from a real one.
2. Poll ``GetItem`` until ``AgentJobIngestFunction`` has stored it as
   ``AGENTJOB#{stack}#{jobId}``, and check the item says what the event said.
3. Check the ingest dead-letter queue gained no message.
4. Delete the item, whatever happened, so nothing synthetic is left to skew a
   comparison.

The event's ``stack`` is ``nimbus-smoke-<timestamp>``
(:data:`nimbus.agentjobs.schemas.SYNTHETIC_STACK_PREFIX`), and that prefix is
what keeps it out of real data. The default bus delivers it to every Nimbus
stack in the account, but only the stack named in its ``smokeTarget`` stores it.
The item gets a one-day TTL, so even a delivery that lands after the cleanup
(late, or retried) expires. And no comparison ever counts it.

What it does **not** prove: anything on Updraft's side (the Pipe, the
enrichment Lambda, tenant opt-in). A real job finishing is the only test of
those. AWS clients are passed in, so the logic is unit-tested without AWS.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nimbus.agentjobs.schemas import (
    EVENT_DETAIL_TYPE,
    EVENT_SOURCE,
    SCHEMA_VERSION,
    SYNTHETIC_STACK_PREFIX,
)
from nimbus.agentjobs.store import job_key
from nimbus.store.ddb_items import META_SK

SMOKE_STACK_PREFIX = SYNTHETIC_STACK_PREFIX
SMOKE_MODEL = "nimbus-smoke"
DEFAULT_TIMEOUT_SECONDS = 60.0
POLL_SECONDS = 2.0


def build_detail(stack: str, job_id: str, now: datetime, target: str) -> dict[str, Any]:
    """A v1 ``Job Finished`` detail, camelCase, as Updraft's enrichment emits it.

    ``target`` is the Nimbus stack meant to store it; every other stack's ingest
    function skips it (:func:`nimbus.agentjobs.lambda_app.handle`).
    """
    at = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "stack": stack,
        "tenantId": "0",
        "jobId": job_id,
        "kind": "review",
        "repository": "nimbus/smoke",
        "createdAt": at,
        "finishedAt": at,
        "durationSeconds": 0,
        "status": "finished",
        "outcome": "succeeded",
        "updraftOutcome": "succeeded",
        "steps": [
            {
                "kind": "review",
                "agent": "smoke",
                "adapter": "smoke",
                "model": SMOKE_MODEL,
                "modelSource": "configured",
                "usage": {"inputTokens": 0, "outputTokens": 0, "costUsd": 0},
            }
        ],
        "routing": {"source": "fixed"},
        "retriedAs": None,
        "benchmark": None,
        "smokeTarget": target,
    }


def build_entry(bus: str, detail: dict[str, Any]) -> dict[str, Any]:
    """The ``PutEvents`` entry carrying ``detail`` onto ``bus``."""
    return {
        "EventBusName": bus,
        "Source": EVENT_SOURCE,
        "DetailType": EVENT_DETAIL_TYPE,
        "Detail": json.dumps(detail),
    }


@dataclass
class SmokeResult:
    ok: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def check(self, name: str, passed: bool, detail: str = "") -> bool:
        self.checks.append((name, passed, detail))
        if not passed:
            self.ok = False
        return passed


def _visible(sqs: Any, queue_url: str) -> int:
    attributes = sqs.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["ApproximateNumberOfMessagesVisible"]
    )["Attributes"]
    return int(attributes["ApproximateNumberOfMessagesVisible"])


def run(
    *,
    events: Any,
    table: Any,
    sqs: Any,
    bus: str,
    queue_url: str,
    target: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    poll: float = POLL_SECONDS,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SmokeResult:
    """Send one synthetic event and follow it into the table.

    Args:
        events: A ``boto3`` EventBridge client.
        table: A ``boto3`` DynamoDB resource ``Table`` (the stack's ``TableName``).
        sqs: A ``boto3`` SQS client.
        bus: Updraft's bus name (``/updraft/<stack>/event-bus-name``).
        queue_url: The stack's ``AgentJobIngestDeadLetterQueueUrl`` output.
        target: The Nimbus stack under test: the only one that stores the event.
    """
    started = now()
    stack = f"{SMOKE_STACK_PREFIX}{started.strftime('%Y%m%d%H%M%S')}"
    job_id = f"job_smoke_{int(started.timestamp() * 1000)}"
    key = {"pk": job_key(stack, job_id), "sk": META_SK}
    result = SmokeResult(ok=True)

    dead_before = _visible(sqs, queue_url)
    try:
        response = events.put_events(
            Entries=[build_entry(bus, build_detail(stack, job_id, started, target))]
        )
        failed = int(response.get("FailedEntryCount", 0))
        if not result.check(
            "event accepted by the bus", failed == 0, f"{bus}: {failed} failed entr(y/ies)"
        ):
            return result

        item = None
        deadline = monotonic() + timeout
        while item is None:
            item = table.get_item(Key=key, ConsistentRead=True).get("Item")
            if item is not None or monotonic() >= deadline:
                break
            sleep(poll)
        if not result.check(
            "stored by AgentJobIngestFunction",
            item is not None,
            f"{key['pk']}" if item else f"{key['pk']} not stored within {timeout:.0f}s",
        ):
            return result

        stored = json.loads(item.get("job") or "{}")
        result.check(
            "stored item matches the event",
            item.get("model") == SMOKE_MODEL
            and item.get("outcome") == "succeeded"
            and stored.get("updraft_outcome") == "succeeded"
            and (stored.get("steps") or [{}])[0].get("usage", {}).get("cost_usd") == 0,
            "model, outcome, updraft_outcome and step usage",
        )
        dead_after = _visible(sqs, queue_url)
        result.check(
            "nothing dead-lettered",
            dead_after <= dead_before,
            f"dead-letter queue {dead_before} -> {dead_after}",
        )
        return result
    finally:
        # Always, so a failed run leaves nothing synthetic behind either.
        table.delete_item(Key=key)
