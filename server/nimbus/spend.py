"""Per-user spend: who spent what in a window, and the ceiling on it.

A deployed stack authenticates every caller (:mod:`nimbus.auth`) but, on its
own, cannot say what any of them cost. This module keeps one counter per
``(user, window)`` -- the window a calendar day or month, UTC -- and enforces an
optional ceiling on it (``NIMBUS_SPEND_LIMIT_USD``). With no ceiling the counter
still runs, so a stack's bill can be attributed even when nobody is capped.

Reserve, then settle
--------------------
Checking a total and *then* starting work lets concurrent requests each see
room and jointly overshoot, so admission adds first and looks second:

1. :func:`reserve` atomically adds a pre-flight *estimate* to the counter. If
   the new total passes the ceiling the estimate is taken straight back out and
   the request is refused (``402 spend_limit_reached``).
2. When the work ends, however it ends, :func:`settle` adds the difference
   between what it really cost and what was reserved.

Both are single atomic adds, so the counter converges on real spend and two
requests racing for the last dollars cannot both win. A worker that dies
between the two leaves its reservation behind until the counter's window
expires; that errs toward refusing, never toward overspending.

Amounts are integer micro-dollars so the adds are exact and atomic on both
backends (SQLite ``UPSERT``, DynamoDB ``ADD``), which have no float-safe
increment. Every figure is an estimate from :mod:`nimbus.pricing`.

Only callers with a verified identity are counted. Locally (no auth) there is
no caller to attribute to and nothing here runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlmodel import Session

from nimbus import deployment
from nimbus.config import Settings
from nimbus.errors import AppError
from nimbus.store import db, ddb_items

MICROS_PER_USD = 1_000_000

Window = Literal["day", "month"]


class SpendLimitError(AppError):
    """The caller's spend for the current window has reached the configured ceiling."""

    status_code = 402
    code = "spend_limit_reached"


def to_micros(usd: float) -> int:
    """USD as whole micro-dollars."""
    return round(usd * MICROS_PER_USD)


def window_key(window: Window, now: datetime | None = None) -> str:
    """The counter's window for ``now`` (UTC): ``2026-09-28`` or ``2026-09``."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return moment.strftime("%Y-%m-%d" if window == "day" else "%Y-%m")


class Principal(BaseModel):
    """Who a request is, and the spend reserved for it.

    Set by the server from the verified token, never taken from a request body:
    the router overwrites whatever a caller sent. ``window`` and
    ``reserved_micros`` are filled by :func:`reserve` and travel with the
    evaluation (to the cloud worker too) so that :func:`settle` adjusts the same
    window the reservation went into, even if the work crosses midnight.
    """

    model_config = ConfigDict(extra="ignore")

    #: The token's stable subject id (Cognito ``sub``).
    user: str = Field(min_length=1)
    #: The token's ``email`` claim, when it carries one (ID tokens do, access tokens do not).
    email: str | None = None
    #: The window key the reservation was added to; ``None`` before :func:`reserve`.
    window: str | None = None
    #: Micro-dollars reserved for this work and not yet settled.
    reserved_micros: int = Field(default=0, ge=0)


def principal_from_claims(claims: dict[str, Any] | None) -> Principal | None:
    """The :class:`Principal` for verified token claims; ``None`` when there is no subject."""
    if not claims:
        return None
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        return None
    email = claims.get("email")
    return Principal(user=subject, email=email if isinstance(email, str) and email else None)


# --------------------------------------------------------------------------- #
# Counters
# --------------------------------------------------------------------------- #


class SpendStore(Protocol):
    """One atomic counter per ``(user, window)``."""

    def add(self, user: str, window: str, micros: int) -> int:
        """Add ``micros`` (negative to give back) and return the new total, atomically."""
        ...

    def total(self, user: str, window: str) -> int:
        """The counter's current value; ``0`` when nothing was ever added."""
        ...


class SqliteSpendStore:
    """Counters in the local history file, in a table of their own.

    A new table, not new columns on the history tables, so an existing history
    file needs no migration: ``create_all`` adds it on the next start.
    """

    def add(self, user: str, window: str, micros: int) -> int:
        with Session(db.get_engine()) as session:
            row = session.execute(
                text(
                    "INSERT INTO spendcounter (user_id, window_key, micros) "
                    "VALUES (:user, :window, :micros) "
                    "ON CONFLICT (user_id, window_key) DO UPDATE SET micros = micros + :micros "
                    "RETURNING micros"
                ),
                {"user": user, "window": window, "micros": micros},
            ).one()
            session.commit()
        return int(row[0])

    def total(self, user: str, window: str) -> int:
        with Session(db.get_engine()) as session:
            row = session.execute(
                text(
                    "SELECT micros FROM spendcounter WHERE user_id = :user AND window_key = :window"
                ),
                {"user": user, "window": window},
            ).first()
        return 0 if row is None else int(row[0])


class DynamoSpendStore:
    """Counters in the stack's DynamoDB table, one item per ``(user, window)``.

    ``ADD`` is DynamoDB's atomic increment. The items carry no GSI1 keys, so
    they never appear in run or evaluation listings, and the table's TTL removes
    them once their window is long past.

    Args:
        table: A ``boto3`` DynamoDB *resource* ``Table``.
    """

    def __init__(self, table: Any) -> None:
        self._table = table

    @staticmethod
    def _key(user: str, window: str) -> dict[str, str]:
        return {"pk": f"SPEND#{user}", "sk": f"WINDOW#{window}"}

    def add(self, user: str, window: str, micros: int) -> int:
        response = self._table.update_item(
            Key=self._key(user, window),
            UpdateExpression=(
                f"ADD micros :amount SET {ddb_items.TTL_ATTRIBUTE} = "
                f"if_not_exists({ddb_items.TTL_ATTRIBUTE}, :expires)"
            ),
            ExpressionAttributeValues={":amount": micros, ":expires": ddb_items.expires_at()},
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["micros"])

    def total(self, user: str, window: str) -> int:
        item = self._table.get_item(Key=self._key(user, window), ConsistentRead=True).get("Item")
        return 0 if item is None else int(item["micros"])


_stores: dict[tuple[str, str], SpendStore] = {}


def get_spend_store(settings: Settings) -> SpendStore:
    """The counter store for these settings: SQLite locally, DynamoDB when deployed."""
    backend = deployment.history_backend(settings)
    if backend == "sqlite":
        key = ("sqlite", "")
        if key not in _stores:
            _stores[key] = SqliteSpendStore()
        return _stores[key]

    # Imported here: boto3 is only needed by a deployed server.
    from nimbus.evals import ddb_reader

    table_name = deployment.history_table(settings)
    key = (table_name, settings.aws_region)
    if key not in _stores:
        _stores[key] = DynamoSpendStore(ddb_reader.build_table(table_name, settings.aws_region))
    return _stores[key]


def reset_cache() -> None:
    """Forget the cached stores (tests switching backends)."""
    _stores.clear()


# --------------------------------------------------------------------------- #
# Admission
# --------------------------------------------------------------------------- #


def reserve(
    store: SpendStore,
    settings: Settings,
    principal: Principal,
    estimate_usd: float,
    now: datetime | None = None,
) -> Principal:
    """Reserve ``estimate_usd`` against the caller's window, or refuse with a 402.

    Returns ``principal`` carrying the window and the amount reserved, which
    :func:`settle` needs. Refusing takes the reservation back out, so a refused
    request costs the caller nothing.
    """
    window = window_key(settings.spend_window, now)
    estimate = to_micros(max(0.0, estimate_usd))
    total = store.add(principal.user, window, estimate)
    limit = settings.spend_limit_usd
    if limit is not None and total > to_micros(limit):
        store.add(principal.user, window, -estimate)
        spent = (total - estimate) / MICROS_PER_USD
        raise SpendLimitError(
            f"Your spend for this {settings.spend_window} (~${spent:,.4f}) plus this request's "
            f"estimate would pass the ${limit:,.2f} limit",
            detail={
                "limit_usd": limit,
                "spent_usd": round(spent, 6),
                "estimate_usd": round(estimate / MICROS_PER_USD, 6),
                "window": settings.spend_window,
                "window_key": window,
            },
        )
    return principal.model_copy(update={"window": window, "reserved_micros": estimate})


def settle(store: SpendStore, principal: Principal | None, actual_usd: float) -> None:
    """Replace a reservation with what the work really cost.

    A no-op for a caller with nothing reserved (no principal, or no window).
    """
    if principal is None or principal.window is None:
        return
    delta = to_micros(max(0.0, actual_usd)) - principal.reserved_micros
    if delta:
        store.add(principal.user, principal.window, delta)
