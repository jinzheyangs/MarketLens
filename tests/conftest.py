from __future__ import annotations

import os

# Must be set before importing anything that constructs module-level settings.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://marketscope:test-only@127.0.0.1:5432/marketscope_test",
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
    "postgresql+asyncpg://marketscope:test-only@127.0.0.1:5432/marketscope_test"
)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "debug": False,
        "database_url": DEFAULT_TEST_DATABASE_URL,
        "allowed_hosts": ["testserver", "localhost", "127.0.0.1"],
        "oidc_enabled": False,
        "oidc_issuer": "https://idp.test",
        "oidc_audience": "marketscope-test",
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
        "oidc_audience": "marketscope",
        "oidc_jwks_uri": "https://idp.example.com/jwks",
        "temporal_enabled": True,
        "temporal_host": "temporal.example.com:7233",
        "temporal_tls": True,
        "event_publisher": "kafka",
        "kafka_bootstrap_servers": ["kafka.example.com:9093"],
        "kafka_security_protocol": "SASL_SSL",
        "kafka_sasl_mechanism": "SCRAM-SHA-512",
        "kafka_sasl_username": "marketscope",
        "kafka_sasl_password": "test-only-kafka-password",
        "event_consumer_enabled": True,
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
