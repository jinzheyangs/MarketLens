"""异步数据库引擎与会话工厂构建。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """创建带连接池的异步引擎。

    连接建立与语句超时均有上限；密码仅通过 ``DATABASE_URL`` 提供，绝不记录到日志。
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
    """构造异步会话工厂：提交后不过期对象、关闭自动 flush。"""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
