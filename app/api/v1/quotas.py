"""租户配额查询与管理接口。"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.auth.authorization import AuthorizationService
from app.auth.dependencies import get_authorization, get_principal, get_settings_dep
from app.auth.enums import AuditAction, Permission, ResourceType
from app.auth.principal import Principal
from app.core.config import Settings
from app.governance.quotas import quota_snapshot, update_quota
from app.schemas.governance import QuotaRead, QuotaUpdate
from app.services.audit import record_event

router = APIRouter(prefix="/quotas", tags=["quotas"])


@router.get("", response_model=QuotaRead, summary="Get tenant quotas and current usage")
async def get_quota(
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> QuotaRead:
    await authz.require_tenant_admin(Permission.QUOTA_READ)
    usage = await quota_snapshot(
        session,
        tenant_id=principal.tenant_id,
        settings=settings,
    )
    return QuotaRead(**asdict(usage))


@router.patch("", response_model=QuotaRead, summary="Update tenant quota limits")
async def patch_quota(
    payload: QuotaUpdate,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> QuotaRead:
    await authz.require_tenant_admin(Permission.QUOTA_MANAGE)
    values = {
        field: value
        for field, value in payload.model_dump(exclude_none=True).items()
        if value is not None
    }
    quota = await update_quota(
        session,
        tenant_id=principal.tenant_id,
        settings=settings,
        values=values,
    )
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.QUOTA_UPDATED,
        resource_type=ResourceType.TENANT_QUOTA,
        resource_id=quota.id,
        details={"limits": values},
    )
    usage = await quota_snapshot(
        session,
        tenant_id=principal.tenant_id,
        settings=settings,
    )
    return QuotaRead(**asdict(usage))
