"""FastAPI dependencies for authentication and authorization."""

from __future__ import annotations

from typing import cast

from fastapi import Depends, Request
from fastapi.security import HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_session
from app.auth.authorization import AuthorizationService
from app.auth.oidc import TokenClaims
from app.auth.principal import Principal, resolve_principal
from app.core.config import Settings
from app.core.context import current_tenant_id
from app.core.errors import AuthenticationError, TenantContextError
from app.services.audit import AuditWriter

bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description="OIDC-issued JWT access token",
)


async def get_principal(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Principal:
    claims = getattr(request.state, "claims", None)
    if not isinstance(claims, TokenClaims):
        raise AuthenticationError
    if current_tenant_id() != claims.tenant_id:
        # Middleware invariant broken: fail closed rather than querying any scope.
        raise TenantContextError
    principal = await resolve_principal(session, claims)
    request.state.principal = principal
    return principal


def get_settings_dep(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_session_factory(
    request: Request,
) -> async_sessionmaker[AsyncSession]:
    return cast(
        async_sessionmaker[AsyncSession],
        request.app.state.session_factory,
    )


def get_audit_writer(
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> AuditWriter:
    return AuditWriter(session_factory)


def get_authorization(
    principal: Principal = Depends(get_principal),
    audit_writer: AuditWriter = Depends(get_audit_writer),
) -> AuthorizationService:
    return AuthorizationService(principal=principal, denial_audit=audit_writer)
