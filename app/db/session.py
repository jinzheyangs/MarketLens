"""Async engine and session factory construction."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Create a pooled async engine.

    Connection establishment and statement timeouts are bounded. The password is
    supplied only through ``DATABASE_URL`` and is never logged.
    """
    return create_async_engine(
        settings.database_url_str,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_pre_ping=True,
        connect_args={
            "timeout": settings.health_check_timeout_seconds,
            "server_settings": {
                "application_name": settings.db_application_name,
                "statement_timeout": str(settings.db_statement_timeout_ms),
                "idle_in_transaction_session_timeout": str(settings.db_statement_timeout_ms),
            },
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
