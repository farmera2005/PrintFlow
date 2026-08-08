from __future__ import annotations

import os
import sys

# Configuration must be in place before anything imports app.config.
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://printflow@127.0.0.1:55432/printflow_test",
    ),
)
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-printflow-suite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app.db as app_db  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture(scope="function", autouse=True)
async def _fresh_engine():
    """Each test runs on its own event loop, so it needs its own engine and pool."""
    app_db._engine = None
    app_db._sessionmaker = None
    yield
    engine = app_db._engine
    app_db._engine = None
    app_db._sessionmaker = None
    if engine is not None:
        await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db(_fresh_engine):
    engine = app_db.get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with app_db.get_sessionmaker()() as session:
        yield session


@pytest_asyncio.fixture
async def client(db):
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest_asyncio.fixture
async def signed_in(client):
    """Create the admin account and keep the session cookie on the client."""
    response = await client.post(
        "/api/setup/admin", json={"username": "admin", "password": "hunter2hunter2"}
    )
    assert response.status_code == 200, response.text
    return client


async def truncate(session) -> None:
    await session.execute(
        text(
            "TRUNCATE orders, order_lines, print_jobs, products, bom_lines, "
            "print_mappings CASCADE"
        )
    )
    await session.commit()
