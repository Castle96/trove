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
