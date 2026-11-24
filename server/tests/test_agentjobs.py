"""Agent jobs: Updraft's ``Job Finished`` events, stored and compared by model."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from nimbus.agentjobs import compare as cmp
from nimbus.agentjobs import lambda_app
from nimbus.agentjobs.schemas import JobFinished
from nimbus.agentjobs.store import AgentJobStore, build_item, primary_step, record_of, utc_iso
from nimbus.errors import register_exception_handlers
from nimbus.routers import agent_jobs
from tests.fake_table import FakeTable

RECEIVED = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def detail(**overrides: Any) -> dict[str, Any]:
    """A v1 ``Job Finished`` detail, as Updraft publishes it (camelCase)."""
    base: dict[str, Any] = {
        "schemaVersion": 1,
        "stack": "updraft-prod",
        "tenantId": "t-1",
        "jobId": "job-1",
        "kind": "implement",
        "repository": "acme/app",
        "issue": 12,
        "createdAt": "2026-11-20T10:00:00Z",
        "finishedAt": "2026-11-20T10:13:32Z",
        "durationSeconds": 812,
        "configVersion": 7,
        "status": "finished",
        "outcome": "succeeded",
        "updraftOutcome": "succeeded",
        "verificationPassed": True,
        "pr": {"url": "https://github.com/acme/app/pull/34", "number": 34},
        "loop": {"rounds": 2, "reviews": 2, "verdict": "approve", "merged": True},
        "steps": [
            {
                "kind": "implement",
                "agent": "main",
                "adapter": "claude",
                "model": "model-a",
                "effort": "high",
            },
            {"kind": "review", "agent": "rev", "adapter": "codex", "model": "reviewer-x"},
        ],
        "routing": {"source": "pool", "pool": "default"},
        "telemetry": {"inputTokens": 1000, "outputTokens": 200, "costUsd": 1.5},
    }
    base.update(overrides)
    return base


def envelope(body: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    event = {
        "id": "evt-1",
        "source": "updraft.cloud",
        "detail-type": "Job Finished",
        "detail": body,
    }
    event.update(overrides)
    return event


def stored(**overrides: Any) -> dict[str, Any]:
    """A job as the store serves it."""
    return record_of(
        build_item(JobFinished.model_validate(detail(**overrides)), received_at=RECEIVED)
    )


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #


def test_unknown_fields_are_ignored_so_additive_v1_changes_do_not_break_ingest():
    job = JobFinished.model_validate(
        detail(somethingNew={"x": 1}, steps=[{"kind": "implement", "newField": 2}])
    )
    assert "somethingNew" not in job.stored()


def test_only_the_identity_is_required():
    job = JobFinished.model_validate(
        {"schemaVersion": 1, "stack": "s", "tenantId": "t", "jobId": "j"}
    )
    assert job.stored() == {
        "schema_version": 1,
        "stack": "s",
        "tenant_id": "t",
        "job_id": "j",
        "steps": [],
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"schemaVersion": 2},
        {"jobId": ""},
        {"stack": "has spaces"},
        {"jobId": "../x"},
        {"steps": [{}] * 25},
    ],
)
def test_events_this_version_cannot_read_are_rejected(overrides):
    with pytest.raises(lambda_app.UnreadableEventError):
        lambda_app.parse(envelope(detail(**overrides)))


# --------------------------------------------------------------------------- #
# The item
# --------------------------------------------------------------------------- #


def test_item_is_keyed_on_the_job_and_listed_by_finish_time():
    item = build_item(JobFinished.model_validate(detail()), received_at=RECEIVED)
    assert item["pk"] == "AGENTJOB#updraft-prod#job-1"
    assert item["sk"] == "META"
    assert item["GSI1PK"] == "AGENTJOB"
    assert item["GSI1SK"] == "2026-11-20T10:13:32+00:00"
    assert item["model"] == "model-a"
    assert json.loads(item["job"])["telemetry"]["cost_usd"] == 1.5
    assert "expiresAt" not in item


def test_nullable_columns_are_present_and_null():
    item = build_item(
        JobFinished.model_validate(
            {"schemaVersion": 1, "stack": "s", "tenantId": "t", "jobId": "j"}
        ),
        received_at=RECEIVED,
    )
    for column in ("kind", "repository", "outcome", "model", "agent"):
        assert column in item and item[column] is None
    # No finish time: the time it was received keeps the listing in order.
    assert item["GSI1SK"] == RECEIVED.isoformat()


def test_retention_expires_agent_jobs_like_other_history():
    item = build_item(JobFinished.model_validate(detail()), received_at=RECEIVED, retention_days=30)
    assert isinstance(item["expiresAt"], int)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-20T10:13:32Z", "2026-11-20T10:13:32+00:00"),
        ("2026-11-20T12:13:32+02:00", "2026-11-20T10:13:32+00:00"),
        ("2026-11-20T10:13:32", "2026-11-20T10:13:32+00:00"),
        ("not a time", RECEIVED.isoformat()),
        (None, RECEIVED.isoformat()),
    ],
)
def test_timestamps_are_normalized_so_keys_sort_as_time(value, expected):
    assert utc_iso(value, RECEIVED) == expected


def test_a_job_is_credited_to_the_step_that_did_its_work():
    implement = detail()["steps"]
    assert primary_step({"kind": "implement", "steps": implement})["model"] == "model-a"
    # The reviewer inside an implement loop judged the work; it did not do it.
    assert (
        primary_step({"kind": "implement", "steps": list(reversed(implement))})["model"]
        == "model-a"
    )
    assert primary_step({"kind": "review", "steps": implement})["model"] == "reviewer-x"
    assert primary_step({"kind": "implement", "steps": []}) is None


# --------------------------------------------------------------------------- #
# The store and the handler
# --------------------------------------------------------------------------- #


def test_a_redelivered_event_overwrites_its_own_item():
    table = FakeTable()
    store = AgentJobStore(table)
    event = envelope(detail())
    lambda_app.handle(event, store)
    lambda_app.handle(copy.deepcopy(event), store)
    assert len(table.items) == 1
    assert store.get("updraft-prod#job-1")["outcome"] == "succeeded"
    assert store.get("updraft-prod#missing") is None


def test_detail_may_arrive_as_a_json_string():
    store = AgentJobStore(FakeTable())
    assert lambda_app.handle(envelope(json.dumps(detail())), store) == {
        "stored": "updraft-prod#job-1"
    }


def test_the_wrong_event_is_rejected_not_stored():
    table = FakeTable()
    with pytest.raises(lambda_app.UnreadableEventError):
        lambda_app.handle(envelope(detail(), source="someone.else"), AgentJobStore(table))
    assert table.items == []


def test_a_rejection_names_fields_not_their_values():
    with pytest.raises(lambda_app.UnreadableEventError) as raised:
        lambda_app.parse(envelope(detail(repository="secret-org/" + "x" * 300)))
    assert "repository" in str(raised.value)
    assert "secret-org" not in str(raised.value)


def test_jobs_are_read_back_by_window_and_newest_first():
    store = AgentJobStore(FakeTable())
    for day in (1, 5, 9):
        store.put(
            JobFinished.model_validate(
                detail(jobId=f"job-{day}", finishedAt=f"2026-11-0{day}T00:00:00Z")
            )
        )
    jobs, truncated = store.between(datetime(2026, 11, 2), datetime(2026, 11, 30))
    assert [j["job_id"] for j in jobs] == ["job-5", "job-9"]
    assert truncated is False
    jobs, truncated = store.between(
        datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 30, tzinfo=UTC), max_jobs=2
    )
    assert truncated is True
    page, cursor = store.page(limit=2)
    assert [j["job_id"] for j in page] == ["job-9", "job-5"]
    rest, end = store.page(limit=2, cursor=cursor)
    assert [j["job_id"] for j in rest] == ["job-1"]
    assert end is None


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def test_wilson_interval_matches_known_values():
    low, high = cmp.wilson(8, 10)
    assert low == pytest.approx(0.4902, abs=1e-4)
    assert high == pytest.approx(0.9433, abs=1e-4)
    assert cmp.wilson(0, 0) is None
    assert cmp.wilson(0, 5)[0] == 0.0


def test_newcombe_interval_matches_known_values():
    # Newcombe (1998), Table II example (a): 56/70 vs 48/80 -> (0.0524, 0.3339).
    low, high = cmp.newcombe(56, 70, 48, 80)
    assert low == pytest.approx(0.0524, abs=1e-3)
    assert high == pytest.approx(0.3339, abs=1e-3)


# --------------------------------------------------------------------------- #
# The comparison
# --------------------------------------------------------------------------- #


def jobs_for(model: str, succeeded: int, failed: int, **overrides: Any) -> list[dict[str, Any]]:
    steps = [
        {"kind": "implement", "agent": model, "adapter": "claude", "model": model, "effort": "high"}
    ]
    return [
        stored(
            jobId=f"{model}-{i}",
            steps=steps,
            outcome="succeeded" if i < succeeded else "failed",
            **overrides,
        )
        for i in range(succeeded + failed)
    ]


def arm(comparison: dict[str, Any], name: str) -> dict[str, Any]:
    return next(a for a in comparison["arms"] if a["arm"] == name)


def test_arms_are_ranked_and_a_clear_gap_is_distinguishable():
    result = cmp.compare_jobs(jobs_for("strong", 45, 5) + jobs_for("weak", 20, 30))
    assert result["basis"] == "observational"
    assert result["leader"] == "strong"
    assert [a["arm"] for a in result["arms"]] == ["strong", "weak"]
    weak = arm(result, "weak")
    assert weak["success_rate"] == pytest.approx(0.4)
    assert weak["versus_leader"]["distinguishable"] is True
    assert arm(result, "strong")["versus_leader"] is None


def test_a_small_gap_on_small_samples_is_reported_as_not_yet_evidence():
    result = cmp.compare_jobs(jobs_for("a", 8, 2) + jobs_for("b", 7, 3))
    assert arm(result, "b")["versus_leader"]["distinguishable"] is False
    assert all(a["small_sample"] for a in result["arms"])
    assert any("not yet evidence" in w for w in result["warnings"])
    assert any("Fewer than 20" in w for w in result["warnings"])


def test_tier_routed_infrastructure_and_benchmark_jobs_are_left_out():
    jobs = (
        jobs_for("a", 3, 0)
        + jobs_for("routed", 3, 0, routing={"source": "tier", "tier": "hard"})
        + [stored(jobId="infra", outcome="launch_failed")]
        + [stored(jobId="bench", benchmark={"runId": "r", "caseId": "c"})]
    )
    result = cmp.compare_jobs(jobs)
    assert [a["arm"] for a in result["arms"]] == ["a"]
    assert result["excluded"] == {
        "filtered": 0,
        "not_attributable": 1,
        "unresolved_outcome": 0,
        "routed": 3,
        "benchmark": 1,
    }
    included = cmp.compare_jobs(jobs, include_routed=True, include_benchmarks=True)
    assert {a["arm"] for a in included["arms"]} == {"a", "routed", "model-a"}
    assert any("Tier-routed jobs are included" in w for w in included["warnings"])


def test_filters_narrow_the_jobs_compared():
    jobs = jobs_for("a", 2, 0) + jobs_for("b", 2, 0, repository="acme/other")
    result = cmp.compare_jobs(jobs, repository="acme/other")
    assert [a["arm"] for a in result["arms"]] == ["b"]
    assert result["excluded"]["filtered"] == 2


def test_jobs_without_a_model_are_grouped_as_unknown_and_never_lead():
    jobs = jobs_for("a", 1, 1) + [
        stored(jobId=f"old-{i}", steps=[{"kind": "implement", "agent": "main"}]) for i in range(5)
    ]
    result = cmp.compare_jobs(jobs)
    assert result["arms"][0]["arm"] == "unknown"  # ranked first on rate...
    assert result["leader"] == "a"  # ...but not credited with the lead
    assert any("did not report a model" in w for w in result["warnings"])


def test_group_by_changes_what_an_arm_is():
    jobs = jobs_for("a", 1, 0)
    assert cmp.compare_jobs(jobs, group_by="model_effort")["arms"][0]["arm"] == "a @ high"
    assert cmp.compare_jobs(jobs, group_by="adapter")["arms"][0]["arm"] == "claude"
    assert cmp.compare_jobs(jobs, group_by="agent")["arms"][0]["arm"] == "a"


def test_outcome_rates_and_costs_are_summarized():
    jobs = [
        stored(
            jobId="1",
            outcome="succeeded",
            telemetry={"costUsd": 2.0, "inputTokens": 100, "outputTokens": 10},
        ),
        stored(
            jobId="2",
            outcome="failed",
            verificationPassed=False,
            loop={"rounds": 4, "merged": False},
            telemetry={"costUsd": 1.0},
        ),
        stored(
            jobId="3",
            outcome="succeeded",
            telemetry=None,
            pr=None,
            loop=None,
            verificationPassed=None,
        ),
    ]
    summary = cmp.compare_jobs(jobs)["arms"][0]
    assert summary["jobs"] == 3
    assert summary["success_rate"] == pytest.approx(2 / 3)
    assert summary["verification_reported"] == 2
    assert summary["verification_pass_rate"] == pytest.approx(0.5)
    assert summary["pr_opened_rate"] == pytest.approx(2 / 3)
    assert summary["merged_rate"] == pytest.approx(0.5)
    assert summary["mean_revision_rounds"] == pytest.approx(3.0)
    # The job totals (every step summed); these steps report no usage of their own.
    assert summary["job_cost"] == {
        "reported": 2,
        "total_usd": 3.0,
        "per_job_usd": 1.5,
        "per_success_usd": 3.0,
    }
    assert summary["cost"]["reported"] == 0
    assert summary["mean_input_tokens"] is None


def test_an_empty_window_says_so():
    result = cmp.compare_jobs([])
    assert result["arms"] == [] and result["leader"] is None
    assert result["warnings"] == ["No jobs to compare in this window."]


# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #


@pytest.fixture
def table() -> FakeTable:
    return FakeTable()


@pytest.fixture
async def client(table) -> AsyncIterator[httpx.AsyncClient]:
    application = FastAPI()
    register_exception_handlers(application)
    application.include_router(agent_jobs.router, prefix="/api/v1")
    application.dependency_overrides[agent_jobs.get_agent_job_store] = lambda: AgentJobStore(table)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def seed(table: FakeTable, **overrides: Any) -> None:
    AgentJobStore(table).put(JobFinished.model_validate(detail(**overrides)), received_at=RECEIVED)


async def test_list_get_and_compare(client, table):
    seed(table, jobId="a", finishedAt="2026-11-20T00:00:00Z")
    seed(table, jobId="b", finishedAt="2026-11-21T00:00:00Z", outcome="failed")

    listing = (await client.get("/api/v1/agent-jobs")).json()
    assert [j["job_id"] for j in listing["items"]] == ["b", "a"]

    one = await client.get("/api/v1/agent-jobs/updraft-prod%23a")
    assert one.status_code == 200 and one.json()["finished_at"] == "2026-11-20T00:00:00+00:00"
    assert (await client.get("/api/v1/agent-jobs/updraft-prod%23zzz")).status_code == 404

    response = await client.get(
        "/api/v1/agent-jobs/compare",
        params={"since": "2026-11-01T00:00:00Z", "until": "2026-11-30T00:00:00Z"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["jobs_compared"] == 2
    assert body["arms"][0]["success_rate"] == pytest.approx(0.5)
    assert body["window"]["since"] == "2026-11-01T00:00:00+00:00"
    assert body["truncated"] is False


async def test_compare_rejects_a_backwards_or_oversized_window(client):
    backwards = await client.get(
        "/api/v1/agent-jobs/compare",
        params={"since": "2026-11-30T00:00:00Z", "until": "2026-11-01T00:00:00Z"},
    )
    assert backwards.status_code == 400
    huge = await client.get(
        "/api/v1/agent-jobs/compare",
        params={"since": "2024-01-01T00:00:00Z", "until": "2026-01-01T00:00:00Z"},
    )
    assert huge.status_code == 400
    assert (
        await client.get("/api/v1/agent-jobs/compare", params={"group_by": "nope"})
    ).status_code == 422


async def test_a_server_without_the_table_says_agent_jobs_are_unavailable():
    application = FastAPI()
    register_exception_handlers(application)
    application.include_router(agent_jobs.router, prefix="/api/v1")
    application.dependency_overrides[agent_jobs.get_agent_job_store] = lambda: None
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.get("/api/v1/agent-jobs")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "agent_jobs_unavailable"


# --------------------------------------------------------------------------- #
# Review fixes (PR #65): unresolved outcomes, strict crediting, one kind at a time
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("outcome", "updraft", "verdict"),
    [
        # Both agree the agent's work succeeded.
        ("succeeded", "succeeded", "success"),
        # The runner's own failures, whatever Updraft's summary said (or if it was lost).
        ("failed", None, "failure"),
        ("failed", "failed", "failure"),
        ("timed_out", None, "failure"),
        ("failed", "succeeded", "failure"),
        # Updraft says the agent's work failed under a runner exit of 0.
        ("succeeded", "failed", "failure"),
        # Runner outcomes that say nothing about the agent.
        ("launch_failed", None, "not_attributable"),
        ("terminated", "succeeded", "not_attributable"),
        ("runner_error", None, "not_attributable"),
        ("state_not_saved", "succeeded", "not_attributable"),
        # #66: the runner exited 0, but no agent work was judged.
        ("succeeded", "nothing-to-do", "not_attributable"),
        ("succeeded", "canceled", "not_attributable"),
        ("succeeded", "superseded", "not_attributable"),
        ("succeeded", "deferred", "not_attributable"),
        ("succeeded", "not-started", "not_attributable"),
        ("succeeded", "state-pending", "not_attributable"),
        ("failed", "error", "not_attributable"),
        # The agent succeeded; only publishing to GitHub failed.
        ("succeeded", "publish-failed", "not_attributable"),
        ("failed", "publish-failed", "not_attributable"),
        # Not enough to say either way.
        ("succeeded", None, "unresolved"),
        ("succeeded", "something-new", "unresolved"),
        ("unconfirmed", "succeeded", "unresolved"),
        ("unknown", None, "unresolved"),
        (None, None, "unresolved"),
    ],
)
def test_outcomes_are_classified_from_both_fields(outcome, updraft, verdict):
    assert cmp.outcome_class({"outcome": outcome, "updraft_outcome": updraft}) == verdict


def test_a_runner_success_with_no_agent_work_is_not_a_model_success():
    """#66: ``updraft run-event`` exits 0 for nothing-to-do, so the runner says succeeded."""
    jobs = jobs_for("m", 2, 2) + [
        stored(jobId=f"idle-{i}", steps=[{"kind": "implement", "model": "m"}], updraftOutcome=u)
        for i, u in enumerate(["nothing-to-do", "superseded", "publish-failed"])
    ]
    result = cmp.compare_jobs(jobs)
    only = arm(result, "m")
    assert only["jobs"] == 4
    assert only["success_rate"] == pytest.approx(0.5)
    assert result["excluded"]["not_attributable"] == 3


def test_a_runner_success_without_an_updraft_summary_is_unresolved():
    jobs = jobs_for("m", 1, 1) + [
        stored(jobId="no-summary", steps=[{"kind": "implement", "model": "m"}], updraftOutcome=None)
    ]
    result = cmp.compare_jobs(jobs)
    assert arm(result, "m")["jobs"] == 2
    assert result["excluded"]["unresolved_outcome"] == 1


def test_missing_or_unrecognized_outcomes_are_not_counted_as_failed_attempts():
    steps = [{"kind": "implement", "model": "m"}]
    jobs = (
        jobs_for("m", 2, 0)
        + [stored(jobId="no-outcome", steps=steps, outcome=None)]
        + [stored(jobId="odd", steps=steps, outcome="unconfirmed")]
    )
    identity_only = record_of(
        build_item(
            JobFinished.model_validate(
                {
                    "schemaVersion": 1,
                    "stack": "s",
                    "tenantId": "t",
                    "jobId": "bare",
                    "kind": "implement",
                }
            ),
            received_at=RECEIVED,
        )
    )
    result = cmp.compare_jobs([*jobs, identity_only])
    only = arm(result, "m")
    assert only["jobs"] == 2
    assert only["success_rate"] == 1.0
    assert result["excluded"]["unresolved_outcome"] == 3
    assert any("missing or unrecognized outcome" in w for w in result["warnings"])


def test_an_implement_job_with_only_a_reviewer_step_is_not_credited_to_the_reviewer():
    reviewer_only = [{"kind": "review", "agent": "rev", "model": "reviewer-x"}]
    assert primary_step({"kind": "implement", "steps": reviewer_only}) is None
    result = cmp.compare_jobs([stored(jobId="j", steps=reviewer_only)])
    assert [a["arm"] for a in result["arms"]] == ["unknown"]
    item = build_item(JobFinished.model_validate(detail(steps=reviewer_only)), received_at=RECEIVED)
    assert item["model"] is None


def test_a_review_job_without_a_review_step_is_not_credited_to_the_implementer():
    implementer_only = [{"kind": "implement", "agent": "main", "model": "model-a"}]
    assert primary_step({"kind": "review", "steps": implementer_only}) is None


def test_only_implement_jobs_are_compared_by_default():
    review_steps = [{"kind": "review", "agent": "rev", "model": "m"}]
    jobs = jobs_for("m", 1, 1) + [stored(jobId="r", kind="review", steps=review_steps)]
    result = cmp.compare_jobs(jobs)
    assert result["kind"] == "implement"
    assert arm(result, "m")["jobs"] == 2
    assert result["excluded"]["filtered"] == 1
    reviews = cmp.compare_jobs(jobs, kind="review")
    assert arm(reviews, "m")["jobs"] == 1
    assert not any("mixed" in w for w in reviews["warnings"])


def test_mixing_kinds_is_an_explicit_opt_in_with_a_warning():
    review_steps = [{"kind": "review", "agent": "rev", "model": "m"}]
    jobs = jobs_for("m", 1, 1) + [stored(jobId="r", kind="review", steps=review_steps)]
    mixed = cmp.compare_jobs(jobs, kind=None)
    assert arm(mixed, "m")["jobs"] == 3
    assert any("Job kinds are mixed" in w for w in mixed["warnings"])


def test_an_arm_reports_an_attribute_only_when_every_member_agrees():
    jobs = [
        stored(
            jobId="1",
            steps=[{"kind": "implement", "adapter": "claude", "model": "a", "effort": "high"}],
        ),
        stored(
            jobId="2",
            steps=[{"kind": "implement", "adapter": "claude", "model": "b", "effort": "high"}],
        ),
    ]
    by_adapter = arm(cmp.compare_jobs(jobs, group_by="adapter"), "claude")
    assert by_adapter["adapter"] == "claude"
    assert by_adapter["effort"] == "high"
    assert by_adapter["model"] is None
    assert by_adapter["mixed"] == {"model": ["a", "b"]}
    by_model = arm(cmp.compare_jobs(jobs), "a")
    assert by_model["model"] == "a" and by_model["mixed"] == {}
    assert by_model["unreported"] == {"agent": 1}


def test_one_reported_value_among_silent_members_is_not_the_arms_identity():
    silent = [{"kind": "implement", "adapter": "claude"}]
    jobs = [
        stored(jobId="known", steps=[{"kind": "implement", "adapter": "claude", "model": "a"}]),
        *[stored(jobId=f"silent-{i}", steps=silent) for i in range(9)],
    ]
    claude = arm(cmp.compare_jobs(jobs, group_by="adapter"), "claude")
    assert claude["adapter"] == "claude"
    assert claude["model"] is None
    assert claude["mixed"]["model"] == ["a"]
    assert claude["unreported"]["model"] == 9


async def test_the_api_compares_implement_jobs_unless_asked_for_all(client, table):
    window = {"since": "2026-11-01T00:00:00Z", "until": "2026-11-30T00:00:00Z"}
    seed(table, jobId="i", finishedAt="2026-11-20T00:00:00Z")
    seed(
        table,
        jobId="r",
        kind="review",
        finishedAt="2026-11-21T00:00:00Z",
        steps=[{"kind": "review", "model": "model-a"}],
    )
    default = (await client.get("/api/v1/agent-jobs/compare", params=window)).json()
    assert default["kind"] == "implement" and default["jobs_compared"] == 1
    everything = (
        await client.get("/api/v1/agent-jobs/compare", params={**window, "kind": "all"})
    ).json()
    assert everything["kind"] is None and everything["jobs_compared"] == 2


@pytest.mark.parametrize("kind", ["", "Implement", "bogus"])
async def test_a_blank_or_unknown_kind_is_rejected_not_read_as_mixed(client, kind):
    response = await client.get("/api/v1/agent-jobs/compare", params={"kind": kind})
    assert response.status_code == 422


def test_an_empty_kind_filter_matches_nothing_rather_than_mixing():
    result = cmp.compare_jobs(jobs_for("m", 1, 0), kind="")
    assert result["arms"] == []
    assert not any("mixed" in w for w in result["warnings"])


# --------------------------------------------------------------------------- #
# #67: cost and tokens belong to the credited step, not the whole job
# --------------------------------------------------------------------------- #


def _priced_steps(implementer_cost: float, reviewer_cost: float) -> list[dict[str, Any]]:
    return [
        {
            "kind": "implement",
            "agent": "main",
            "model": "cheap",
            "modelSource": "response",
            "runId": "run_1",
            "usage": {"costUsd": implementer_cost, "inputTokens": 1000, "outputTokens": 100},
        },
        {
            "kind": "review",
            "agent": "rev",
            "model": "pricey-reviewer",
            "modelSource": "configured",
            "usage": {"costUsd": reviewer_cost, "inputTokens": 50000, "outputTokens": 5000},
        },
    ]


def test_an_arm_is_charged_only_its_own_steps_cost():
    jobs = [
        stored(
            jobId=f"j{i}",
            steps=_priced_steps(0.5, 9.5),
            telemetry={"costUsd": 10.0, "inputTokens": 51000, "outputTokens": 5100},
        )
        for i in range(2)
    ]
    cheap = arm(cmp.compare_jobs(jobs), "cheap")
    assert cheap["cost"] == {
        "reported": 2,
        "total_usd": 1.0,
        "per_job_usd": 0.5,
        "per_success_usd": 0.5,
    }
    assert cheap["mean_input_tokens"] == pytest.approx(1000)
    assert cheap["mean_output_tokens"] == pytest.approx(100)
    # The whole job, reviewer included, is still there -- labelled as such.
    assert cheap["job_cost"]["per_job_usd"] == pytest.approx(10.0)


def test_steps_without_usage_are_not_averaged_in_as_zero():
    jobs = [
        stored(jobId="priced", steps=_priced_steps(2.0, 1.0)),
        stored(jobId="silent", steps=[{"kind": "implement", "model": "cheap"}]),
    ]
    cheap = arm(cmp.compare_jobs(jobs), "cheap")
    assert cheap["jobs"] == 2
    assert cheap["cost"]["reported"] == 1
    assert cheap["cost"]["per_job_usd"] == pytest.approx(2.0)


def test_each_arm_counts_how_its_model_was_known():
    jobs = [
        stored(jobId="r", steps=[{"kind": "implement", "model": "m", "modelSource": "response"}]),
        stored(jobId="c", steps=[{"kind": "implement", "model": "m", "modelSource": "configured"}]),
        stored(jobId="u", steps=[{"kind": "implement", "model": "m"}]),
    ]
    assert arm(cmp.compare_jobs(jobs), "m")["model_source"] == {
        "response": 1,
        "configured": 1,
        "unreported": 1,
    }
    nameless = cmp.compare_jobs([stored(jobId="n", steps=[{"kind": "implement", "agent": "a"}])])
    assert arm(nameless, "unknown")["model_source"] == {"no_model": 1}


def test_step_usage_and_provenance_survive_ingestion():
    job = stored(steps=_priced_steps(0.5, 9.5))
    step = job["steps"][0]
    assert step["usage"]["cost_usd"] == 0.5
    assert step["model_source"] == "response"
    assert step["run_id"] == "run_1"
