from __future__ import annotations

import os

# Must be set before importing anything that constructs module-level settings.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://market_intel:test-only@127.0.0.1:5432/market_intel_test",
)
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DEBUG", "false")
os.environ.setdefault("OTEL_ENABLED", "false")

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.factory import create_app

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://market_intel:test-only@127.0.0.1:5432/market_intel_test"
)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "debug": False,
        "database_url": DEFAULT_TEST_DATABASE_URL,
        "allowed_hosts": ["testserver", "localhost", "127.0.0.1"],
        "oidc_enabled": False,
        "oidc_issuer": "https://idp.test",
        "oidc_audience": "marketlens-test",
        "oidc_jwks_uri": "https://idp.test/jwks",
        "otel_enabled": False,
        "log_format": "json",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_production_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "production",
        "allowed_hosts": ["api.example.com"],
        "oidc_enabled": True,
        "oidc_issuer": "https://idp.example.com",
        "oidc_audience": "marketlens",
        "oidc_jwks_uri": "https://idp.example.com/jwks",
    }
    values.update(overrides)
    return make_settings(**values)


@asynccontextmanager
async def open_client(application: FastAPI) -> AsyncIterator[AsyncClient]:
    """Run an app's lifespan and yield an in-process HTTP client."""
    async with LifespanManager(application):
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="https://testserver",
            follow_redirects=False,
        ) as client:
            yield client


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with LifespanManager(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="https://testserver",
        follow_redirects=False,
    ) as http_client:
        yield http_client
