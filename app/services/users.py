"""Tenant user provisioning and principal-facing views."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.enums import AuditAction, ResourceType
from app.auth.principal import Principal
from app.core.errors import ConflictError
from app.db.models import User
from app.db.tenant_scope import tenant_scoped_select
from app.schemas.tenancy import MembershipView, TenantRead
from app.schemas.users import MeResponse, UserProvisionRequest, UserRead
from app.services.audit import record_event


async def provision_user(
    session: AsyncSession,
    *,
    payload: UserProvisionRequest,
    issuer: str | None,
    principal: Principal,
) -> User:
    if not issuer:
        raise ConflictError("The identity provider issuer is not configured.")
    user = User(
        tenant_id=principal.tenant_id,
        issuer=issuer,
        subject=payload.subject,
        email=str(payload.email),
        display_name=payload.display_name,
        tenant_role=payload.tenant_role.value,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "A user with this subject or email already exists in this tenant."
        ) from exc
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.USER_PROVISIONED,
        resource_type=ResourceType.USER,
        resource_id=user.id,
        details={"tenant_role": user.tenant_role},
    )
    return user


async def list_users(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
) -> list[User]:
    statement = (
        tenant_scoped_select(User)
        .order_by(User.created_at.desc(), User.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.scalars(statement)).all())


async def get_user_by_id(session: AsyncSession, user_id: UUID) -> User | None:
    return cast(
        User | None,
        await session.scalar(tenant_scoped_select(User).where(User.id == user_id)),
    )


def memberships_for_principal(principal: Principal) -> list[MembershipView]:
    memberships = [
        MembershipView(scope_type="organization", scope_id=scope_id, role=role)
        for scope_id, role in principal.organization_roles.items()
    ]
    memberships.extend(
        MembershipView(scope_type="workspace", scope_id=scope_id, role=role)
        for scope_id, role in principal.workspace_roles.items()
    )
    memberships.extend(
        MembershipView(scope_type="project", scope_id=scope_id, role=role)
        for scope_id, role in principal.project_roles.items()
    )
    return memberships


def me_response(principal: Principal, user: User) -> MeResponse:
    return MeResponse(
        user=UserRead.model_validate(user),
        tenant=TenantRead(
            id=principal.tenant_id,
            slug=principal.tenant_slug,
            name=principal.tenant_name,
            status=principal.tenant_status,
        ),
        memberships=memberships_for_principal(principal),
    )


__all__ = [
    "get_user_by_id",
    "list_users",
    "me_response",
    "memberships_for_principal",
    "provision_user",
]
