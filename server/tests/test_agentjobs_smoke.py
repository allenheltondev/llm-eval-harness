"""``nimbus.agentjobs.smoke``: the synthetic Job Finished round trip (#68), without AWS."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from nimbus.agentjobs import lambda_app, smoke
from nimbus.agentjobs.compare import compare_jobs
from nimbus.agentjobs.schemas import JobFinished
from nimbus.agentjobs.store import AgentJobStore, build_item, record_of
from tests.fake_table import FakeTable

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)


class FakeQueue:
    def __init__(self, visible: list[int]) -> None:
        self.visible = list(visible)

    def get_queue_attributes(self, QueueUrl: str, AttributeNames: list[str]) -> dict[str, Any]:  # noqa: N803
        value = self.visible.pop(0) if len(self.visible) > 1 else self.visible[0]
        return {"Attributes": {"ApproximateNumberOfMessagesVisible": str(value)}}


class FakeBus:
    """Delivers each entry to the real handler, as the rule would -- or drops it."""

    def __init__(self, table: FakeTable, *, deliver: bool = True, failed: int = 0) -> None:
        self.table = table
        self.deliver = deliver
        self.failed = failed
        self.entries: list[dict[str, Any]] = []

    def put_events(self, Entries: list[dict[str, Any]]) -> dict[str, Any]:  # noqa: N803
        self.entries.extend(Entries)
        if self.deliver and not self.failed:
            for entry in Entries:
                event = {
                    "id": "evt",
                    "source": entry["Source"],
                    "detail-type": entry["DetailType"],
                    "detail": json.loads(entry["Detail"]),
                }
                lambda_app.handle(event, AgentJobStore(self.table))
        return {"FailedEntryCount": self.failed}


def run(table: FakeTable, bus: FakeBus, queue: FakeQueue, **kwargs: Any) -> smoke.SmokeResult:
    clock = iter(range(0, 10_000, 5))
    return smoke.run(
        events=bus,
        table=table,
        sqs=queue,
        bus="updraft-staging-events",
        queue_url="https://sqs/dlq",
        now=lambda: NOW,
        sleep=lambda _: None,
        monotonic=lambda: float(next(clock)),
        **kwargs,
    )


def test_the_synthetic_event_is_a_valid_v1_event_that_never_joins_a_real_comparison():
    detail = smoke.build_detail("smoke-1", "job_smoke_1", NOW)
    job = JobFinished.model_validate(detail)
    assert job.stack.startswith(smoke.SMOKE_STACK_PREFIX)
    entry = smoke.build_entry("bus", detail)
    assert (entry["Source"], entry["DetailType"]) == ("updraft.cloud", "Job Finished")
    # A review job: the default (implement) comparison filters it out.
    stored = record_of(build_item(job, received_at=NOW))
    assert compare_jobs([stored])["arms"] == []


def test_a_healthy_stack_passes_and_is_left_clean():
    table = FakeTable()
    bus = FakeBus(table)
    result = run(table, bus, FakeQueue([0, 0]))
    assert result.ok, result.checks
    assert [name for name, _, _ in result.checks] == [
        "event accepted by the bus",
        "stored by AgentJobIngestFunction",
        "stored item matches the event",
        "nothing dead-lettered",
    ]
    assert bus.entries[0]["EventBusName"] == "updraft-staging-events"
    assert table.items == []
    assert len(table.deletes) == 1


def test_an_event_that_is_never_stored_fails_and_still_cleans_up():
    table = FakeTable()
    result = run(table, FakeBus(table, deliver=False), FakeQueue([0]), timeout=20)
    assert not result.ok
    assert result.checks[-1][0] == "stored by AgentJobIngestFunction"
    assert "not stored within 20s" in result.checks[-1][2]
    assert len(table.deletes) == 1


def test_a_rejected_put_fails_at_the_bus():
    table = FakeTable()
    result = run(table, FakeBus(table, failed=1), FakeQueue([0]))
    assert not result.ok
    assert result.checks == [
        ("event accepted by the bus", False, "updraft-staging-events: 1 failed entr(y/ies)")
    ]


def test_a_new_dead_letter_fails_the_run():
    table = FakeTable()
    result = run(table, FakeBus(table), FakeQueue([3, 4]))
    assert not result.ok
    assert result.checks[-1] == ("nothing dead-lettered", False, "dead-letter queue 3 -> 4")
