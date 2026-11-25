"""FastAPI 鉴权与授权依赖注入。"""

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

# Bearer 认证方案：auto_error=False，由下游统一抛 401 信封
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description="OIDC-issued JWT access token",
)


async def get_principal(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """从请求状态中的令牌声明解析当前主体；校验租户上下文一致性。"""
    claims = getattr(request.state, "claims", None)
    if not isinstance(claims, TokenClaims):
        raise AuthenticationError
    if current_tenant_id() != claims.tenant_id:
        # 中间件不变量被破坏：快速失败，不按任何范围查询
        raise TenantContextError
    principal = await resolve_principal(session, claims)
    request.state.principal = principal
    return principal


def get_settings_dep(request: Request) -> Settings:
    """取用应用挂载的全局配置。"""
    return cast(Settings, request.app.state.settings)


def get_session_factory(
    request: Request,
) -> async_sessionmaker[AsyncSession]:
    """取用应用挂载的数据库会话工厂。"""
    return cast(
        async_sessionmaker[AsyncSession],
        request.app.state.session_factory,
    )


def get_audit_writer(
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> AuditWriter:
    """构造审计写入器。"""
    return AuditWriter(session_factory)


def get_authorization(
    principal: Principal = Depends(get_principal),
    audit_writer: AuditWriter = Depends(get_audit_writer),
) -> AuthorizationService:
    """组装授权服务（携带主体与审计写入器）。"""
    return AuthorizationService(principal=principal, denial_audit=audit_writer)
