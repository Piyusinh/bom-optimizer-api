"""Shared fixtures for the API test suite."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-at-least-32-characters")

import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app import database as db
from app.database import get_db
from app.main import app


@pytest_asyncio.fixture
async def client():
    """Yield an HTTP client against a fresh in-memory database."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Point the app globals at the test engine/session for the lifespan's init_db
    # and for all route dependencies.
    db.engine = engine
    db.async_session = session_maker

    async def override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    # httpx's ASGITransport does not run the app lifespan, so run it explicitly:
    # it creates the tables and seeds roles/users on the test engine.
    # The login limiter is a process-wide singleton, so clear it between tests.
    from app import rate_limit
    rate_limit._limiter._attempts.clear()

    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    app.dependency_overrides.clear()
