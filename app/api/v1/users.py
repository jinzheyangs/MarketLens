"""Tenant user directory management (tenant administrators only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.auth.authorization import AuthorizationService
from app.auth.dependencies import get_authorization, get_principal, get_settings_dep
from app.auth.enums import Permission
from app.auth.principal import Principal
from app.core.config import Settings
from app.schemas.users import UserProvisionRequest, UserRead
from app.services.users import list_users, provision_user

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserRead], summary="List tenant users")
async def list_tenant_users(
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[UserRead]:
    await authz.require_tenant_admin(Permission.USER_READ)
    users = await list_users(session, limit=limit, offset=offset)
    return [UserRead.model_validate(user) for user in users]


@router.post(
    "",
    response_model=UserRead,
    status_code=201,
    summary="Provision a user for the configured OIDC issuer",
)
async def provision_tenant_user(
    payload: UserProvisionRequest,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    settings: Settings = Depends(get_settings_dep),
    session: AsyncSession = Depends(get_session),
) -> UserRead:
    await authz.require_tenant_admin(Permission.USER_MANAGE)
    user = await provision_user(
        session,
        payload=payload,
        issuer=settings.oidc_issuer,
        principal=principal,
    )
    return UserRead.model_validate(user)
