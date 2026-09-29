"""``GET /evaluations/compare``: a comparison of stored suite evaluations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from nimbus.config import Settings
from nimbus.errors import register_exception_handlers
from nimbus.routers import runs
from nimbus.store import db
from nimbus.store.repo import get_history_repo
from tests.test_evals_compare import ALL_PASS, HALF, result

CASES = [
    {"id": "a", "input": "question a"},
    {"id": "b", "input": "question b"},
]


@pytest.fixture
def initialized_db(tmp_path):
    return db.init_db(str(tmp_path / "compare-api.db"))


@pytest.fixture
async def client(initialized_db) -> AsyncIterator[httpx.AsyncClient]:
    application = FastAPI()
    register_exception_handlers(application)
    application.include_router(runs.router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def store_evaluation(
    model_id: str,
    outcome: dict[str, Any] | None,
    *,
    kind: str = "suite",
    cases: list[dict[str, str]] | None = None,
    provider: str | None = None,
    status: str = "completed",
) -> str:
    repo = get_history_repo(Settings(_env_file=None))
    run_config = {"model_id": model_id, **({"provider": provider} if provider else {})}
    record = repo.create_evaluation(
        kind=kind,
        run_ids=[],
        config={
            "kind": kind,
            "suite": {"name": "support", "run_config": run_config, "cases": cases or CASES},
        },
        status="pending",
    )
    repo.update_evaluation(record.id, status=status, result=outcome)
    return record.id


async def compare(client: httpx.AsyncClient, *ids: str) -> httpx.Response:
    return await client.get("/api/v1/evaluations/compare", params=[("ids", i) for i in ids])


async def test_two_stored_evaluations_are_compared(client):
    weak = store_evaluation("weak", result(HALF))
    strong = store_evaluation("strong", result(ALL_PASS))

    response = await compare(client, weak, strong)

    assert response.status_code == 200
    body = response.json()
    assert body["winner"] == "bedrock:strong"
    assert body["ranking"] == ["bedrock:strong", "bedrock:weak"]
    assert body["suite"] == {"name": "support", "cases": 2}
    assert [arm["evaluation_id"] for arm in body["arms"]] == [weak, strong]
    assert body["split_cases"] == ["b"]


async def test_the_model_comes_from_the_stored_suite_and_its_provider(client):
    first = store_evaluation("m", result(ALL_PASS), provider="openai")
    second = store_evaluation("m", result(HALF))

    body = (await compare(client, first, second)).json()

    assert sorted(arm["label"] for arm in body["arms"]) == ["bedrock:m", "openai:m"]


async def test_an_arm_that_did_not_complete_is_listed_but_not_ranked(client):
    done = store_evaluation("done", result(ALL_PASS))
    cut = store_evaluation("cut", result(HALF), status="cancelled")

    body = (await compare(client, done, cut)).json()

    assert body["ranking"] == ["bedrock:done"]
    assert {arm["model_id"]: arm["status"] for arm in body["arms"]} == {
        "done": "completed",
        "cut": "cancelled",
    }


async def test_an_evaluation_with_no_result_is_still_listed(client):
    done = store_evaluation("done", result(ALL_PASS))
    empty = store_evaluation("empty", None, status="error")

    response = await compare(client, done, empty)

    assert response.status_code == 200
    assert response.json()["ranking"] == ["bedrock:done"]


async def test_something_that_is_not_a_suite_cannot_be_compared(client):
    suite = store_evaluation("m1", result(ALL_PASS))
    determinism = store_evaluation("m2", result(ALL_PASS), kind="determinism")

    response = await compare(client, suite, determinism)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "compare_not_suite"
    assert error["detail"]["evaluation_ids"] == [determinism]


async def test_different_suites_cannot_be_compared(client):
    first = store_evaluation("m1", result(ALL_PASS))
    changed = [{"id": "a", "input": "question a"}, {"id": "b", "input": "a DIFFERENT question"}]
    second = store_evaluation("m2", result(ALL_PASS), cases=changed)

    response = await compare(client, first, second)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "compare_different_suites"
    assert error["detail"]["cases"] == ["b"]


async def test_a_suite_with_an_extra_case_is_a_different_suite(client):
    first = store_evaluation("m1", result(ALL_PASS))
    second = store_evaluation("m2", result(ALL_PASS), cases=[*CASES, {"id": "c", "input": "q"}])

    response = await compare(client, first, second)

    assert response.json()["error"]["detail"]["cases"] == ["c"]


async def test_the_same_model_twice_cannot_be_compared(client):
    first = store_evaluation("same", result(ALL_PASS))
    second = store_evaluation("same", result(HALF))

    response = await compare(client, first, second)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "compare_duplicate_model"


async def test_an_unknown_evaluation_is_a_404(client):
    known = store_evaluation("m1", result(ALL_PASS))

    response = await compare(client, known, "no-such-evaluation")

    assert response.status_code == 404


async def test_at_least_two_evaluations_are_needed(client):
    known = store_evaluation("m1", result(ALL_PASS))

    assert (await compare(client, known)).status_code == 422
    assert (await compare(client)).status_code == 422


async def test_no_more_than_twelve_can_be_compared(client):
    ids = [store_evaluation(f"m{n}", result(ALL_PASS)) for n in range(13)]

    assert (await compare(client, *ids)).status_code == 422
    assert (await compare(client, *ids[:12])).status_code == 200


async def test_the_compare_route_does_not_shadow_a_real_evaluation_lookup(client):
    known = store_evaluation("m1", result(ALL_PASS))

    assert (await client.get(f"/api/v1/evaluations/{known}")).status_code == 200


def tagged_evaluation(name: str, model: str, outcome, *, baseline: bool = False) -> str:
    """A stored, arm-tagged suite evaluation (the tag lives in its config)."""
    repo = get_history_repo(Settings(_env_file=None))
    record = repo.create_evaluation(
        kind="suite",
        run_ids=[],
        config={
            "kind": "suite",
            "arm": {"name": name, **({"baseline": True} if baseline else {})},
            "suite": {
                "name": "support",
                "run_config": {"model_id": model},
                "cases": CASES,
            },
        },
        status="pending",
    )
    repo.update_evaluation(record.id, status="completed", result=outcome)
    return record.id


async def test_a_baseline_turns_on_the_fallback_analysis(client):
    primary = tagged_evaluation("primary", "m1", result(ALL_PASS), baseline=True)
    fallback = tagged_evaluation("fallback", "m2", result(HALF))

    response = await compare(client, primary, fallback)

    body = response.json()
    assert body["baseline"] == "primary"  # the tagged arm, with no query parameter needed
    vs = next(arm for arm in body["arms"] if arm["label"] == "fallback")["vs_baseline"]
    assert vs["regressions"] == ["b"]
    assert vs["status"] in ("not_ready", "inconclusive")
    assert body["bar"]["source"] == "default"


async def test_a_baseline_can_be_named_in_the_query(client):
    one = tagged_evaluation("one", "m1", result(ALL_PASS))
    two = tagged_evaluation("two", "m2", result(HALF))

    response = await client.get(
        "/api/v1/evaluations/compare", params=[("ids", one), ("ids", two), ("baseline", two)]
    )

    assert response.json()["baseline"] == "two"


async def test_an_unknown_baseline_is_a_named_400(client):
    one = tagged_evaluation("one", "m1", result(ALL_PASS))
    two = tagged_evaluation("two", "m2", result(HALF))

    response = await client.get(
        "/api/v1/evaluations/compare", params=[("ids", one), ("ids", two), ("baseline", "nope")]
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "compare_unknown_baseline"


async def test_two_arms_with_one_name_are_refused_with_the_arm_code(client):
    one = tagged_evaluation("same", "m1", result(ALL_PASS))
    two = tagged_evaluation("same", "m2", result(HALF))

    response = await compare(client, one, two)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "compare_duplicate_arm"
