from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


# Additive schema migrations for pre-existing SQLite databases.
# `create_all()` creates any *new* tables but never alters existing tables,
# so columns added later are applied here with guarded ALTER TABLE statements.
_MIGRATIONS: dict[str, list[str]] = {
    "certificates": [
        "ALTER TABLE certificates ADD COLUMN revoked_at DATETIME",
        "ALTER TABLE certificates ADD COLUMN revocation_reason VARCHAR(64) NOT NULL DEFAULT ''",
        "ALTER TABLE certificates ADD COLUMN private_key TEXT",
        "ALTER TABLE certificates ADD COLUMN deleted_at DATETIME",
    ],
}


def init_engine(database_url: str | None = None) -> None:
    """Create the global engine/session-factory. Overridable for tests."""
    global _engine, _session_factory
    url = database_url or get_settings().database_url
    _engine = create_async_engine(url, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


def get_engine():
    """Return the global engine, creating it lazily (for probes/lifespan)."""
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with get_session_factory()() as session:
        yield session


async def _run_migrations(conn) -> None:
    for table, statements in _MIGRATIONS.items():
        rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
        existing = {row[1] for row in rows}
        for stmt in statements:
            column = stmt.split("ADD COLUMN ")[1].split(" ")[0]
            if column not in existing:
                await conn.execute(text(stmt))


async def init_db() -> None:
    """Create tables (imports models to register them), then apply migrations."""
    from . import models  # noqa: F401

    if _engine is None:
        init_engine()
    assert _engine is not None
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _run_migrations(conn)
