"""Sync SQLModel engine/session management for the run + evaluation history store.

Kept deliberately sync: this is a local tool, sqlite has no meaningful async
advantage here, and FastAPI happily runs sync ``def`` endpoints/dependencies in
a threadpool. A single module-level engine is lazily created from
``Settings.db_path`` the first time it's needed, or explicitly via
``init_db(path)`` (used by tests to point the store at a tmp_path file).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from nimbus.config import get_settings

_engine: Engine | None = None


def _enable_wal(dbapi_connection: sqlite3.Connection, connection_record: object) -> None:
    """Enable WAL journaling + NORMAL sync on every new DBAPI connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def _build_engine(db_path: str) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _enable_wal)
    return engine


def init_db(db_path: str) -> Engine:
    """Create the parent directory (if needed), build the engine, and create tables.

    Sets the module-level engine used by :func:`get_session` / :func:`get_engine`
    and returns it, so callers (including tests) can (re)point the store at a
    specific sqlite file, e.g. ``init_db(str(tmp_path / "history.db"))``.
    """
    global _engine

    # Import inside the function (not at module scope) to avoid a circular
    # import between store.db and store.models, while still guaranteeing the
    # table classes are registered on SQLModel.metadata before create_all.
    from nimbus.store import models  # noqa: F401

    path = Path(db_path)
    if db_path not in (":memory:", "") and path.parent != Path():
        path.parent.mkdir(parents=True, exist_ok=True)

    engine = _build_engine(db_path)
    SQLModel.metadata.create_all(engine)
    _engine = engine
    return engine


@contextmanager
def scoped_db(db_path: str) -> Iterator[Engine]:
    """Point the store at ``db_path`` for the duration, then put things back.

    On exit the scoped engine is disposed -- its pooled connections closed, so
    the file can be deleted -- and whatever engine was active before is
    restored. The eval worker uses this to give every invocation a database of
    its own inside a warm Lambda environment, where the process (and so the
    module-level engine) outlives any one evaluation.
    """
    global _engine
    previous = _engine
    engine = init_db(db_path)
    try:
        yield engine
    finally:
        # Restore first: if disposing raises, the process must still not be
        # left pointing at this engine and a file about to be deleted.
        _engine = previous
        engine.dispose()


def get_engine() -> Engine:
    """Return the active engine, lazily initializing it from settings if needed."""
    global _engine
    if _engine is None:
        init_db(get_settings().db_path)
    assert _engine is not None
    return _engine


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a SQLModel session bound to the active engine."""
    with Session(get_engine()) as session:
        yield session
