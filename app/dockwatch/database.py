"""Async database setup (SQLAlchemy 2.0 + aiosqlite).

The engine is created lazily via :func:`get_engine` so importing this module
has no side effects and tests can override ``DOCKWATCH_DATABASE_URL`` before
first use. ``engine`` and ``SessionFactory`` remain available as deprecated
aliases through module ``__getattr__``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.dockwatch.config import get_settings


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base for all ORM models."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _enable_sqlite_wal(engine: AsyncEngine) -> None:
    """Turn on WAL journaling + a busy timeout for SQLite connections.

    The background monitor sampler writes every couple of seconds while the
    HTTP API also writes (activity log, endpoints). Without WAL, writers block
    readers and concurrent writes can raise "database is locked"; WAL lets
    readers and the writer proceed at the same time. On other dialects this is
    a no-op.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        # The aiosqlite adapter's cursor.execute() runs the PRAGMA synchronously
        # and does not expose rows, so results are not fetched here.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first call."""
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        if url.startswith("sqlite"):
            _engine = create_async_engine(
                url,
                echo=False,
                future=True,
                pool_pre_ping=True,
                connect_args={"timeout": 30},
            )
            _enable_sqlite_wal(_engine)
        else:
            _engine = create_async_engine(
                url,
                echo=False,
                future=True,
                pool_pre_ping=True,
                pool_size=settings.database_pool_size,
                max_overflow=settings.database_max_overflow,
            )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory, creating it on first call."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


def __getattr__(name: str) -> Any:
    """Deprecated aliases: ``engine`` and ``SessionFactory`` (lazy)."""
    if name == "engine":
        return get_engine()
    if name == "SessionFactory":
        return get_session_factory()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def init_db() -> None:
    """Create all tables unless ``create_all_on_startup`` is disabled.

    ``create_all`` is additive-only (it never mutates existing tables), so it
    is safe to run on every startup in local/dev runs and in the container.
    Set ``DOCKWATCH_CREATE_ALL_ON_STARTUP=false`` when the schema is managed
    by an external migration tool instead.
    """
    if not get_settings().create_all_on_startup:
        return
    # Import models so they are registered on the metadata before create_all.
    from app.dockwatch import models  # noqa: F401

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency exposing an async database session."""
    async with get_session_factory()() as session:
        yield session
