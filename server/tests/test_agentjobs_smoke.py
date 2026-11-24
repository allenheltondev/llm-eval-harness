"""``nimbus.agentjobs.smoke``: the synthetic Job Finished round trip (#68), without AWS."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from nimbus.agentjobs import lambda_app, smoke
from nimbus.agentjobs.compare import compare_jobs
from nimbus.agentjobs.schemas import SYNTHETIC_TTL_SECONDS, JobFinished
from nimbus.agentjobs.store import AgentJobStore, build_item, record_of
from tests.fake_table import FakeTable

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)


class FakeQueue:
    def __init__(self, visible: list[int]) -> None:
        self.visible = list(visible)

    def get_queue_attributes(self, QueueUrl: str, AttributeNames: list[str]) -> dict[str, Any]:  # noqa: N803
        value = self.visible.pop(0) if len(self.visible) > 1 else self.visible[0]
        return {"Attributes": {"ApproximateNumberOfMessagesVisible": str(value)}}


TARGET = "llm-eval-harness"


def as_event(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "evt",
        "source": entry["Source"],
        "detail-type": entry["DetailType"],
        "detail": json.loads(entry["Detail"]),
    }


class FakeBus:
    """The default bus: delivers each entry to every subscribed stack's real handler."""

    def __init__(
        self,
        table: FakeTable,
        *,
        deliver: bool = True,
        failed: int = 0,
        others: dict[str, FakeTable] | None = None,
    ) -> None:
        self.subscribers = {TARGET: table, **(others or {})}
        self.deliver = deliver
        self.failed = failed
        self.entries: list[dict[str, Any]] = []

    def put_events(self, Entries: list[dict[str, Any]]) -> dict[str, Any]:  # noqa: N803
        self.entries.extend(Entries)
        if self.deliver and not self.failed:
            for entry in Entries:
                for stack, table in self.subscribers.items():
                    lambda_app.handle(as_event(entry), AgentJobStore(table), stack)
        return {"FailedEntryCount": self.failed}


def run(table: FakeTable, bus: FakeBus, queue: FakeQueue, **kwargs: Any) -> smoke.SmokeResult:
    clock = iter(range(0, 10_000, 5))
    return smoke.run(
        events=bus,
        table=table,
        sqs=queue,
        bus="updraft-staging-events",
        queue_url="https://sqs/dlq",
        target=TARGET,
        now=lambda: NOW,
        sleep=lambda _: None,
        monotonic=lambda: float(next(clock)),
        **kwargs,
    )


def test_the_synthetic_event_is_a_valid_v1_event_that_never_joins_a_real_comparison():
    detail = smoke.build_detail("nimbus-smoke-1", "job_smoke_1", NOW, TARGET)
    job = JobFinished.model_validate(detail)
    assert job.stack.startswith(smoke.SMOKE_STACK_PREFIX)
    assert job.smoke_target == TARGET
    entry = smoke.build_entry("default", detail)
    assert (entry["Source"], entry["DetailType"]) == ("updraft.cloud", "Job Finished")
    stored = record_of(build_item(job, received_at=NOW))
    # Excluded from every comparison, not only the default (implement) one.
    for kind in ("implement", "review", None):
        result = compare_jobs([stored], kind=kind)
        assert result["arms"] == []
        assert result["excluded"]["synthetic"] == 1


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


# --------------------------------------------------------------------------- #
# PR #74 review: the default bus fans out, and deliveries can arrive late
# --------------------------------------------------------------------------- #


def test_only_the_targeted_stack_stores_the_synthetic_event():
    target, staging, other = FakeTable(), FakeTable(), FakeTable()
    bus = FakeBus(target, others={"llm-eval-harness-dev": staging, "someone-else": other})
    result = run(target, bus, FakeQueue([0, 0]))
    assert result.ok, result.checks
    # Every subscriber received it; none but the target kept it, and none failed on it.
    assert staging.puts == [] and other.puts == []


def test_a_synthetic_event_without_a_matching_target_is_skipped_not_dead_lettered():
    table = FakeTable()
    detail = smoke.build_detail("nimbus-smoke-1", "job_smoke_1", NOW, "another-stack")
    entry = smoke.build_entry("default", detail)
    assert lambda_app.handle(as_event(entry), AgentJobStore(table), TARGET) == {
        "skipped": "job_smoke_1"
    }
    # And a stack that does not know its own name keeps nothing synthetic.
    assert lambda_app.handle(as_event(entry), AgentJobStore(table), None)["skipped"]
    assert table.puts == []


def test_a_delivery_after_the_cleanup_expires_and_never_counts():
    """A late or retried delivery lands after the smoke test has deleted its item."""
    table = FakeTable()
    result = run(table, FakeBus(table), FakeQueue([0, 0]))
    assert result.ok and table.items == []

    # The same event delivered again, after the cleanup.
    entry = smoke.build_entry(
        "default", smoke.build_detail("nimbus-smoke-1", "job_smoke_1", NOW, TARGET)
    )
    received = datetime(2026, 11, 24, 16, 0, tzinfo=UTC)
    store = AgentJobStore(table, retention_days=0)  # 0: real history is kept forever
    store.put(JobFinished.model_validate(as_event(entry)["detail"]), received_at=received)
    (item,) = table.items
    # ...but a synthetic item expires a day after it arrived, whenever that was.
    assert item["expiresAt"] == int(received.timestamp()) + SYNTHETIC_TTL_SECONDS
    assert compare_jobs([record_of(item)], kind=None)["excluded"]["synthetic"] == 1


def test_real_jobs_still_follow_the_stack_retention():
    job = JobFinished.model_validate(
        {"schemaVersion": 1, "stack": "updraft-cloud", "tenantId": "1", "jobId": "j"}
    )
    assert "expiresAt" not in build_item(job, received_at=NOW, retention_days=0)
