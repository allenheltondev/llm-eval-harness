"""Per-user spend: the windows, the counters on both backends, and reserve/settle."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlmodel import SQLModel, create_engine

from nimbus import spend
from nimbus.config import Settings
from nimbus.spend import (
    DynamoSpendStore,
    Principal,
    SpendLimitError,
    SqliteSpendStore,
    principal_from_claims,
    reserve,
    settle,
    to_micros,
    window_key,
)
from nimbus.store import db, models

USER = "user-1"
NOW = datetime(2026, 9, 28, 23, 30, tzinfo=UTC)


@pytest.fixture
def sqlite_store(tmp_path):
    db.init_db(str(tmp_path / "spend.db"))
    return SqliteSpendStore()


class FakeResourceTable:
    """The slice of a boto3 DynamoDB *resource* Table the counter uses.

    Faithful where it matters: an update creates a missing item (real DynamoDB
    does; the shared fake does not), ``ADD`` is an increment, ``if_not_exists``
    only sets the TTL once, numbers come back as ``Decimal``, and a float in the
    request is rejected as boto3's serializer rejects it.
    """

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs["ExpressionAttributeValues"]
        assert not any(isinstance(value, float) for value in values.values()), "float in request"
        assert kwargs["UpdateExpression"].startswith("ADD micros :amount")
        assert kwargs["ReturnValues"] == "UPDATED_NEW"
        key = (kwargs["Key"]["pk"], kwargs["Key"]["sk"])
        item = self.items.setdefault(key, dict(kwargs["Key"]))
        item["micros"] = Decimal(item.get("micros", 0)) + Decimal(values[":amount"])
        item.setdefault("expiresAt", Decimal(values[":expires"]))
        return {"Attributes": {"micros": item["micros"]}}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        item = self.items.get((kwargs["Key"]["pk"], kwargs["Key"]["sk"]))
        return {"Item": dict(item)} if item else {}


@pytest.fixture
def dynamo_store():
    return DynamoSpendStore(FakeResourceTable())


@pytest.fixture(params=["sqlite", "dynamodb"])
def store(request, sqlite_store, dynamo_store):
    """Every behaviour below holds on both backends."""
    return sqlite_store if request.param == "sqlite" else dynamo_store


def settings(**fields: Any) -> Settings:
    return Settings(_env_file=None, **fields)


# --------------------------------------------------------------------------- #
# Windows and identity
# --------------------------------------------------------------------------- #


def test_a_window_is_the_utc_day_or_month():
    assert window_key("day", NOW) == "2026-09-28"
    assert window_key("month", NOW) == "2026-09"


def test_the_window_follows_utc_not_the_callers_offset():
    late_in_new_york = datetime(2026, 9, 28, 22, 0, tzinfo=timezone(timedelta(hours=-5)))

    assert window_key("day", late_in_new_york) == "2026-09-29"  # already tomorrow in UTC


def test_micros_are_whole_and_exact():
    assert to_micros(1.5) == 1_500_000
    assert to_micros(0.0000012) == 1
    assert to_micros(0) == 0


def test_a_principal_comes_from_the_verified_subject():
    claims = {"sub": "abc-123", "email": "dev@example.com", "cognito:groups": ["nimbus"]}

    principal = principal_from_claims(claims)

    assert principal == Principal(user="abc-123", email="dev@example.com")
    assert principal.window is None and principal.reserved_micros == 0


@pytest.mark.parametrize(
    "claims", [None, {}, {"sub": ""}, {"sub": 7}, {"email": "a@b.c"}, "not-a-dict", object()]
)
def test_claims_without_a_subject_are_nobody(claims):
    assert principal_from_claims(claims) is None


def test_an_access_token_without_an_email_still_identifies_the_user():
    assert principal_from_claims({"sub": "abc"}).email is None


# --------------------------------------------------------------------------- #
# The counters
# --------------------------------------------------------------------------- #


def test_a_counter_starts_at_zero_and_adds(store):
    assert store.total(USER, "2026-09-28") == 0
    assert store.add(USER, "2026-09-28", 250) == 250
    assert store.add(USER, "2026-09-28", 100) == 350
    assert store.total(USER, "2026-09-28") == 350


def test_a_negative_add_gives_back(store):
    store.add(USER, "w", 500)

    assert store.add(USER, "w", -200) == 300


def test_counters_are_per_user_and_per_window(store):
    store.add("alice", "2026-09-28", 100)
    store.add("bob", "2026-09-28", 7)
    store.add("alice", "2026-09-29", 3)

    assert store.total("alice", "2026-09-28") == 100
    assert store.total("bob", "2026-09-28") == 7
    assert store.total("alice", "2026-09-29") == 3


def test_concurrent_adds_are_all_counted(sqlite_store):
    """The reason reservation adds first: racing writers must not lose an update."""
    workers = [threading.Thread(target=lambda: sqlite_store.add(USER, "w", 1)) for _ in range(40)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert sqlite_store.total(USER, "w") == 40


def test_a_dynamo_counter_sets_its_expiry_once():
    table = FakeResourceTable()
    store = DynamoSpendStore(table)

    store.add(USER, "w", 1)
    first = table.items[("SPEND#user-1", "WINDOW#w")]["expiresAt"]
    store.add(USER, "w", 1)

    assert table.items[("SPEND#user-1", "WINDOW#w")]["expiresAt"] == first
    assert first > 0


def test_a_dynamo_counter_never_looks_like_a_history_item():
    table = FakeResourceTable()

    DynamoSpendStore(table).add(USER, "w", 1)

    item = table.items[("SPEND#user-1", "WINDOW#w")]
    assert not any(attribute.startswith("GSI1") for attribute in item)  # not in any listing


def test_an_old_history_file_gains_the_counter_table_without_losing_anything(tmp_path):
    """Compatibility: a history file made before per-user spend existed."""
    path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(
        engine,
        tables=[models.Run.__table__, models.Evaluation.__table__],  # type: ignore[attr-defined]
    )
    engine.dispose()

    db.init_db(str(path))
    store = SqliteSpendStore()

    assert store.add(USER, "w", 5) == 5


# --------------------------------------------------------------------------- #
# Reserve and settle
# --------------------------------------------------------------------------- #


def test_a_reservation_counts_against_the_window_and_is_carried_on_the_principal(store):
    principal = reserve(store, settings(), Principal(user=USER), 0.25, NOW)

    assert principal.window == "2026-09-28"
    assert principal.reserved_micros == 250_000
    assert store.total(USER, "2026-09-28") == 250_000


def test_with_no_ceiling_nothing_is_refused_but_spend_is_still_counted(store):
    for _ in range(3):
        reserve(store, settings(), Principal(user=USER), 1_000.0, NOW)

    assert store.total(USER, "2026-09-28") == 3_000 * 1_000_000


def test_a_request_that_fits_under_the_ceiling_is_admitted(store):
    reserve(store, settings(spend_limit_usd=1.0), Principal(user=USER), 0.6, NOW)

    admitted = reserve(store, settings(spend_limit_usd=1.0), Principal(user=USER), 0.4, NOW)

    assert admitted.reserved_micros == 400_000  # exactly reaching the ceiling is allowed


def test_a_request_that_would_pass_the_ceiling_is_refused_and_costs_nothing(store):
    reserve(store, settings(spend_limit_usd=1.0), Principal(user=USER), 0.9, NOW)

    with pytest.raises(SpendLimitError) as caught:
        reserve(store, settings(spend_limit_usd=1.0), Principal(user=USER), 0.2, NOW)

    assert caught.value.status_code == 402
    assert caught.value.code == "spend_limit_reached"
    assert caught.value.detail == {
        "limit_usd": 1.0,
        "spent_usd": 0.9,
        "estimate_usd": 0.2,
        "window": "day",
        "window_key": "2026-09-28",
    }
    assert store.total(USER, "2026-09-28") == 900_000  # the refused estimate was taken back


def test_a_user_already_over_the_ceiling_is_refused_even_for_free_work(store):
    store.add(USER, "2026-09-28", 2_000_000)

    with pytest.raises(SpendLimitError):
        reserve(store, settings(spend_limit_usd=1.0), Principal(user=USER), 0.0, NOW)


def test_one_users_spend_never_blocks_another(store):
    reserve(store, settings(spend_limit_usd=1.0), Principal(user="alice"), 1.0, NOW)

    reserve(store, settings(spend_limit_usd=1.0), Principal(user="bob"), 1.0, NOW)


def test_the_ceiling_resets_with_the_window(store):
    reserve(store, settings(spend_limit_usd=1.0), Principal(user=USER), 1.0, NOW)

    tomorrow = NOW + timedelta(days=1)
    reserve(store, settings(spend_limit_usd=1.0), Principal(user=USER), 1.0, tomorrow)


def test_a_monthly_window_spans_the_month(store):
    monthly = settings(spend_limit_usd=1.0, spend_window="month")
    reserve(store, monthly, Principal(user=USER), 1.0, NOW)

    with pytest.raises(SpendLimitError) as caught:
        reserve(store, monthly, Principal(user=USER), 0.5, NOW + timedelta(days=1))

    assert caught.value.detail["window"] == "month"


def test_racing_requests_cannot_both_take_the_last_of_the_budget(sqlite_store):
    """Only one of ten threads asking for the whole ceiling may be admitted."""
    admitted: list[Principal] = []
    refused: list[SpendLimitError] = []

    def ask() -> None:
        try:
            admitted.append(
                reserve(sqlite_store, settings(spend_limit_usd=1.0), Principal(user=USER), 1.0, NOW)
            )
        except SpendLimitError as error:
            refused.append(error)

    threads = [threading.Thread(target=ask) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert (len(admitted), len(refused)) == (1, 9)
    assert sqlite_store.total(USER, "2026-09-28") == 1_000_000


def test_settling_replaces_the_estimate_with_the_actual_cost(store):
    principal = reserve(store, settings(), Principal(user=USER), 1.0, NOW)

    settle(store, principal, 0.3)

    assert store.total(USER, "2026-09-28") == 300_000


def test_settling_a_run_that_cost_more_than_estimated_charges_the_difference(store):
    principal = reserve(store, settings(), Principal(user=USER), 0.1, NOW)

    settle(store, principal, 0.5)

    assert store.total(USER, "2026-09-28") == 500_000


def test_settling_charges_the_window_that_was_reserved_not_the_one_it_ended_in(store):
    principal = reserve(store, settings(), Principal(user=USER), 0.5, NOW)  # 23:30 on the 28th

    settle(store, principal, 0.5)  # ...settled after midnight, by a worker that reads no clock

    assert store.total(USER, "2026-09-28") == 500_000
    assert store.total(USER, "2026-09-29") == 0


def test_a_cancelled_run_that_cost_nothing_gives_the_whole_reservation_back(store):
    principal = reserve(store, settings(), Principal(user=USER), 2.0, NOW)

    settle(store, principal, 0.0)

    assert store.total(USER, "2026-09-28") == 0


@pytest.mark.parametrize("principal", [None, Principal(user=USER)])
def test_settling_without_a_reservation_is_a_no_op(store, principal):
    settle(store, principal, 5.0)

    assert store.total(USER, "2026-09-28") == 0


def test_a_negative_estimate_or_cost_is_treated_as_zero(store):
    principal = reserve(store, settings(), Principal(user=USER), -3.0, NOW)
    assert principal.reserved_micros == 0

    settle(store, principal, -1.0)

    assert store.total(USER, "2026-09-28") == 0


# --------------------------------------------------------------------------- #
# Which store
# --------------------------------------------------------------------------- #


def test_a_stack_table_selects_dynamodb_even_when_history_is_local(monkeypatch):
    """The cloud worker keeps history in SQLite but must settle into the shared counter."""
    from nimbus.evals import ddb_reader

    table = FakeResourceTable()
    monkeypatch.setattr(ddb_reader, "build_table", lambda name, region: table)
    spend.reset_cache()

    store = spend.get_spend_store(settings(eval_table="stack-table", history_backend="sqlite"))

    assert isinstance(store, DynamoSpendStore)
    store.add(USER, "w", 3)
    assert table.items[("SPEND#user-1", "WINDOW#w")]["micros"] == 3


def test_without_a_stack_table_the_local_file_is_used():
    spend.reset_cache()

    assert isinstance(spend.get_spend_store(settings(eval_table=None)), SqliteSpendStore)
