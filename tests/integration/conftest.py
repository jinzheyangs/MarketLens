from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.tenant_guard import install_tenant_guard

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        env_file = ROOT / ".env"
        if env_file.exists():
            url = dotenv_values(env_file).get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping PostgreSQL integration tests")
    database = make_url(url).database
    if not database or "test" not in database.lower():
        pytest.fail(
            "Refusing to run destructive integration tests against a database whose "
            "name does not contain 'test'"
        )
    return url


@pytest.fixture(scope="session")
def alembic_config(test_database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", test_database_url.replace("%", "%%"))
    return config


@pytest.fixture(scope="session", autouse=True)
def migrated_database(alembic_config: Config, test_database_url: str) -> None:
    try:
        command.upgrade(alembic_config, "head")
    except Exception as exc:
        safe_url = make_url(test_database_url).render_as_string(hide_password=True)
        raise RuntimeError(
            "Could not initialize the integration test database. Start PostgreSQL "
            f"(docker compose up -d postgres) or unset TEST_DATABASE_URL. Target: {safe_url}"
        ) from exc


@pytest.fixture
async def engine(migrated_database: None, test_database_url: str) -> AsyncIterator[AsyncEngine]:
    install_tenant_guard()
    # NullPool avoids sharing pool connections across per-test event loops.
    test_engine = create_async_engine(test_database_url, poolclass=NullPool)
    try:
        yield test_engine
    finally:
        await test_engine.dispose()
