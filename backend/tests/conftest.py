import asyncio
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from app.config import get_settings

TEST_DB = "splitledger_test"


def _test_db_url() -> str:
    base = get_settings().database_url
    return base.rsplit("/", 1)[0] + f"/{TEST_DB}"


@pytest.fixture(scope="session")
def test_database() -> Iterator[str]:
    """Create splitledger_test on the dev Postgres, migrate to head, drop at end."""
    admin_engine = create_async_engine(
        get_settings().database_url, isolation_level="AUTOCOMMIT"
    )

    async def _create() -> None:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
        await admin_engine.dispose()

    asyncio.run(_create())

    url = _test_db_url()
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    yield url

    drop_engine = create_async_engine(
        get_settings().database_url, isolation_level="AUTOCOMMIT"
    )

    async def _drop() -> None:
        async with drop_engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
        await drop_engine.dispose()

    asyncio.run(_drop())


@pytest.fixture
async def engine(test_database: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(test_database)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """App wired to the test DB. Patches the app.db globals because the
    idempotency middleware acquires sessions outside FastAPI DI."""
    import app.db as db
    from app.main import create_app

    db._engine = engine
    db._session_factory = async_sessionmaker(engine, expire_on_commit=False)
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    db._engine = None
    db._session_factory = None
