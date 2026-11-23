"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a transactional session.

    Commits on success and rolls back on any exception (including tenant guard
    violations). Business endpoints added in later stages must use this
    dependency instead of creating ad-hoc sessions.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session, session.begin():
        yield session
