"""共享的 FastAPI 依赖。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """提供一个事务会话：成功提交，任何异常（含租户守卫违规）都回滚。

    业务接口应统一使用该依赖获取会话，不要自行创建临时会话。
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session, session.begin():
        yield session
