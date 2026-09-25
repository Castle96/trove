from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.database import get_session_factory, init_db, init_engine
from app.main import create_app
from app.seed import seed_if_empty


@pytest_asyncio.fixture
async def client(tmp_path) -> AsyncGenerator[AsyncClient, None]:
    """Each test gets its own temp DB seeded with the 5 demo certificates.

    ``TROVE_DATA_DIR`` is pointed at the temp dir so generated encryption
    master keys and key material never leak into the real ``data/`` folder.
    """
    os.environ["TROVE_DATA_DIR"] = str(tmp_path)
    get_settings.cache_clear()
    init_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db()
    async with get_session_factory()() as db:
        await seed_if_empty(db)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def dw_db(tmp_path_factory):
    """Point the Dockwatch SQLite DB at a throwaway file and create its tables.

    ``httpx.ASGITransport`` never runs the FastAPI lifespan, so the dockwatch
    tables (endpoints, container_links, monitors, ...) are created explicitly
    here. Opt-in: only tests that request this fixture touch the dockwatch
    database, and each requesting test gets a fresh file (no cross-test
    pollution of ``./dockwatch.db``).
    """
    path = tmp_path_factory.mktemp("dockwatch") / "dockwatch.db"
    os.environ["DOCKWATCH_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"

    import app.dockwatch.database as dw_db_mod

    # Drop any lazily-built engine so the fresh URL takes effect.
    if dw_db_mod._engine is not None:
        await dw_db_mod._engine.dispose()
        dw_db_mod._engine = None
        dw_db_mod._session_factory = None

    from app.dockwatch.config import get_settings as dw_settings

    dw_settings.cache_clear()

    engine = dw_db_mod.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(dw_db_mod.Base.metadata.drop_all)
        await conn.run_sync(dw_db_mod.Base.metadata.create_all)
    yield
    await engine.dispose()
    dw_db_mod._engine = None
    dw_db_mod._session_factory = None
