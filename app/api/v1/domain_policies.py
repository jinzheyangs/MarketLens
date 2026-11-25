"""租户级采集域名策略接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.auth.authorization import AuthorizationService
from app.auth.dependencies import get_authorization, get_principal
from app.auth.enums import AuditAction, Permission, ResourceType
from app.auth.principal import Principal
from app.governance.domain_policies import (
    DomainPolicy,
    get_domain_policy,
    upsert_domain_policy,
)
from app.schemas.governance import DomainPolicyRead, DomainPolicyUpdate
from app.services.audit import record_event

router = APIRouter(prefix="/domain-policy", tags=["domain-policy"])


def _read(policy: DomainPolicy) -> DomainPolicyRead:
    return DomainPolicyRead(
        allowed_domains=list(policy.allowed_domains),
        blocked_domains=list(policy.blocked_domains),
    )


@router.get("", response_model=DomainPolicyRead, summary="Get tenant domain policy")
async def get_policy(
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> DomainPolicyRead:
    await authz.require_tenant_admin(Permission.DOMAIN_POLICY_READ)
    policy = await get_domain_policy(session, tenant_id=principal.tenant_id)
    return _read(policy)


@router.patch("", response_model=DomainPolicyRead, summary="Update tenant domain policy")
async def patch_policy(
    payload: DomainPolicyUpdate,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> DomainPolicyRead:
    await authz.require_tenant_admin(Permission.DOMAIN_POLICY_MANAGE)
    row, policy = await upsert_domain_policy(
        session,
        tenant_id=principal.tenant_id,
        allowed_domains=payload.allowed_domains,
        blocked_domains=payload.blocked_domains,
        updated_by=principal.user_id,
    )
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.DOMAIN_POLICY_UPDATED,
        resource_type=ResourceType.TENANT,
        resource_id=principal.tenant_id,
        details={
            "allowed_domains": len(policy.allowed_domains),
            "blocked_domains": len(policy.blocked_domains),
        },
    )
    await session.refresh(row)
    return _read(policy)
