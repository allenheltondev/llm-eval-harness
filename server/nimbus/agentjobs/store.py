"""Where an agent job *is* a DynamoDB item, and how it is read back.

One item per finished job, in the shared table::

    pk      AGENTJOB#{stack}#{jobId}
    sk      META
    GSI1PK  AGENTJOB
    GSI1SK  finished-at, UTC ISO 8601 (the listing's and the comparison's order)

The key is the job's identity, so storing is a plain ``PutItem``: EventBridge
and Lambda both deliver at least once, and a redelivered event overwrites its
own item with the same content. No condition is needed for that to be safe,
because a job finishes once -- a retry of it is a different job.

The job itself is one JSON-string column (``job``), matching how the rest of
the table stores structured values (:mod:`nimbus.store.ddb_items`). The few
attributes a reader filters on are copied out beside it, present-and-null when
unknown, never absent.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Key

from nimbus.agentjobs.schemas import SYNTHETIC_TTL_SECONDS, JobFinished, is_synthetic
from nimbus.store import ddb_items
from nimbus.store.ddb_items import GSI1_INDEX_NAME, GSI1_PK, GSI1_SK, META_SK

AGENTJOB_PARTITION = "AGENTJOB"
#: Page size while walking a time range for a comparison.
SCAN_PAGE_SIZE = 200
#: The most jobs one comparison reads. At Updraft's expected volume (about 100
#: jobs a month) this is years of history; the cap is a guard, not a budget.
MAX_COMPARED_JOBS = 5000

#: Step kinds that write the code, as opposed to reviewing it.
AUTHOR_STEP_KINDS = ("implement", "revision")


def job_key(stack: str, job_id: str) -> str:
    """The ``pk`` of a job, and (without the prefix) its id in the API."""
    return f"{AGENTJOB_PARTITION}#{stack}#{job_id}"


def job_ref(stack: str, job_id: str) -> str:
    """The id a job is served under: ``{stack}#{jobId}``."""
    return f"{stack}#{job_id}"


def _aware(value: datetime) -> datetime:
    """A datetime as UTC-aware, treating a naive one (a bare ``?since=``) as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def utc_iso(value: str | None, fallback: datetime) -> str:
    """An ISO timestamp normalized to UTC, so ``GSI1SK`` sorts as time does.

    Updraft sends ``...Z``; Python writes ``+00:00``. Comparing the two as strings
    would misorder them, so every key is rewritten in one form. A missing or
    unparsable value takes ``fallback`` (the time the event was received).
    """
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).isoformat()
    return fallback.astimezone(UTC).isoformat()


def primary_step(job: Mapping[str, Any]) -> dict[str, Any] | None:
    """The step a job is credited to when it is compared by model.

    For an implement or revision job, the first step that wrote code: that is
    the model whose work the outcome, the verification and the merge describe.
    A review job is credited to its first review step. Reviewer steps inside
    an implement job's loop are deliberately not the job's model -- they judged
    the work, they did not do it.

    ``None`` when the job reports no step of the kind it needs. Falling back to
    another step would credit the wrong model (an implement job whose only
    reported step is its reviewer), so such a job is grouped as unknown instead.
    """
    steps = [s for s in job.get("steps") or [] if isinstance(s, Mapping)]
    wanted = ("review",) if job.get("kind") == "review" else AUTHOR_STEP_KINDS
    for step in steps:
        if step.get("kind") in wanted:
            return dict(step)
    return None


def build_item(
    event: JobFinished,
    *,
    received_at: datetime | None = None,
    retention_days: int = ddb_items.KEEP_FOREVER,
) -> dict[str, Any]:
    """The table item for one ``Job Finished`` event."""
    received = received_at or datetime.now(UTC)
    job = event.stored()
    finished = utc_iso(event.finished_at, received)
    step = primary_step(job) or {}
    item: dict[str, Any] = {
        "pk": job_key(event.stack, event.job_id),
        "sk": META_SK,
        GSI1_PK: AGENTJOB_PARTITION,
        GSI1_SK: finished,
        "id": job_ref(event.stack, event.job_id),
        "ts": finished,
        "received_at": received.astimezone(UTC).isoformat(),
        "stack": event.stack,
        "tenant_id": event.tenant_id,
        "job_id": event.job_id,
        "kind": ddb_items.text(event.kind),
        "repository": ddb_items.text(event.repository),
        "outcome": ddb_items.text(event.outcome),
        "model": ddb_items.text(step.get("model")),
        "agent": ddb_items.text(step.get("agent")),
        "job": ddb_items.dump_json(job),
    }
    if is_synthetic(event.stack):
        # A smoke test's item always expires, even when it lands after the test
        # deleted it (a late or repeated delivery), whatever the retention.
        item[ddb_items.TTL_ATTRIBUTE] = int(received.timestamp()) + SYNTHETIC_TTL_SECONDS
        return item
    return ddb_items.apply_retention(item, retention_days)


def record_of(item: Mapping[str, Any]) -> dict[str, Any]:
    """A stored item as the API serves it: the job, plus where it is stored."""
    job = ddb_items.parse_json(item.get("job"), default={}) or {}
    # The stored columns win over the event's own copies: ``finished_at`` is
    # served normalized (UTC, the form the listing sorts and filters on), not
    # in whatever form the event spelled it.
    return {
        **job,
        "id": item.get("id"),
        "finished_at": item.get("ts"),
        "received_at": item.get("received_at"),
    }


class AgentJobStore:
    """Agent jobs on the shared table.

    Args:
        table: A ``boto3`` DynamoDB *resource* ``Table``.
        retention_days: ``HistoryRetentionDays``; ``0`` keeps jobs forever.
    """

    def __init__(self, table: Any, *, retention_days: int = ddb_items.KEEP_FOREVER) -> None:
        self._table = table
        self._retention_days = retention_days

    def put(self, event: JobFinished, *, received_at: datetime | None = None) -> dict[str, Any]:
        """Store (or idempotently re-store) one finished job. Returns the item."""
        item = build_item(event, received_at=received_at, retention_days=self._retention_days)
        self._table.put_item(Item=item)
        return item

    def get(self, ref: str) -> dict[str, Any] | None:
        """One job by its ``{stack}#{jobId}`` id, or ``None``."""
        response = self._table.get_item(Key={"pk": f"{AGENTJOB_PARTITION}#{ref}", "sk": META_SK})
        item = response.get("Item")
        return record_of(item) if item else None

    def page(
        self, *, limit: int, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One newest-first page of jobs, and the cursor of the next."""
        query: dict[str, Any] = {
            "IndexName": GSI1_INDEX_NAME,
            "KeyConditionExpression": Key(GSI1_PK).eq(AGENTJOB_PARTITION),
            "ScanIndexForward": False,
            "Limit": max(1, min(limit, 100)),
        }
        if cursor:
            query["ExclusiveStartKey"] = ddb_items.decode_cursor(cursor, AGENTJOB_PARTITION)
        response = self._table.query(**query)
        last = response.get("LastEvaluatedKey")
        next_cursor = ddb_items.encode_cursor(str(last[GSI1_SK]), str(last["pk"])) if last else None
        return [record_of(item) for item in response.get("Items", [])], next_cursor

    def between(
        self, since: datetime, until: datetime, *, max_jobs: int = MAX_COMPARED_JOBS
    ) -> tuple[list[dict[str, Any]], bool]:
        """Every job finished in ``[since, until]``, oldest first.

        Returns the jobs and whether the read stopped at ``max_jobs`` (in which
        case the newest jobs in the range are the ones left out).
        """
        jobs: list[dict[str, Any]] = []
        for item in self._walk(since, until):
            if len(jobs) >= max_jobs:
                return jobs, True
            jobs.append(record_of(item))
        return jobs, False

    def _walk(self, since: datetime, until: datetime) -> Iterator[dict[str, Any]]:
        condition = Key(GSI1_PK).eq(AGENTJOB_PARTITION) & Key(GSI1_SK).between(
            _aware(since).astimezone(UTC).isoformat(), _aware(until).astimezone(UTC).isoformat()
        )
        start: dict[str, Any] | None = None
        while True:
            query: dict[str, Any] = {
                "IndexName": GSI1_INDEX_NAME,
                "KeyConditionExpression": condition,
                "ScanIndexForward": True,
                "Limit": SCAN_PAGE_SIZE,
            }
            if start is not None:
                query["ExclusiveStartKey"] = start
            response = self._table.query(**query)
            yield from response.get("Items", [])
            start = response.get("LastEvaluatedKey")
            if not start:
                return
