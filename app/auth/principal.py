"""Request principal loaded from PostgreSQL, not from JWT authorization claims."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.enums import (
    MembershipStatus,
    ScopeRole,
    TenantRole,
    TenantStatus,
    UserStatus,
)
from app.auth.oidc import TokenClaims
from app.core.errors import AuthenticationError, ForbiddenError
from app.db.models import (
    OrganizationMembership,
    Project,
    ProjectMembership,
    Tenant,
    User,
    Workspace,
    WorkspaceMembership,
)


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID
    tenant_id: UUID
    tenant_slug: str
    tenant_name: str
    tenant_status: TenantStatus
    issuer: str
    subject: str
    email: str
    display_name: str
    tenant_role: TenantRole
    organization_roles: dict[UUID, ScopeRole]
    workspace_roles: dict[UUID, ScopeRole]
    project_roles: dict[UUID, ScopeRole]
    organization_ids: frozenset[UUID]
    workspace_ids: frozenset[UUID]
    project_ids: frozenset[UUID]

    @property
    def is_tenant_admin(self) -> bool:
        return self.tenant_role == TenantRole.TENANT_ADMIN


async def resolve_principal(session: AsyncSession, claims: TokenClaims) -> Principal:
    """Map a verified token to an active tenant user and its memberships.

    The tenant claim is only a signed lookup key. A matching user row must exist
    in that tenant, and every scope role is loaded from the database. JWT claims
    named ``role``, ``roles``, ``permissions``, etc. are ignored.
    """
    tenant = await session.get(Tenant, claims.tenant_id)
    if tenant is None:
        raise AuthenticationError(
            "The authentication token is not valid for this tenant.",
            invalid_token=True,
        )
    if tenant.status != TenantStatus.ACTIVE:
        raise ForbiddenError

    user = await session.scalar(
        select(User).where(
            User.tenant_id == claims.tenant_id,
            User.issuer == claims.issuer,
            User.subject == claims.subject,
        )
    )
    if user is None:
        raise AuthenticationError(
            "The authentication token is not valid for this tenant.",
            invalid_token=True,
        )
    if user.status != UserStatus.ACTIVE:
        raise ForbiddenError

    organization_roles: dict[UUID, ScopeRole] = {}
    workspace_roles: dict[UUID, ScopeRole] = {}
    project_roles: dict[UUID, ScopeRole] = {}
    organization_ids: set[UUID] = set()
    workspace_ids: set[UUID] = set()
    project_ids: set[UUID] = set()

    org_rows = await session.scalars(
        select(OrganizationMembership).where(
            OrganizationMembership.tenant_id == claims.tenant_id,
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.status == MembershipStatus.ACTIVE,
        )
    )
    for membership in org_rows:
        role = ScopeRole(membership.role)
        organization_roles[membership.organization_id] = role
        organization_ids.add(membership.organization_id)

    workspace_rows = await session.execute(
        select(WorkspaceMembership, Workspace.organization_id)
        .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
        .where(
            WorkspaceMembership.tenant_id == claims.tenant_id,
            Workspace.tenant_id == claims.tenant_id,
            WorkspaceMembership.user_id == user.id,
            WorkspaceMembership.status == MembershipStatus.ACTIVE,
        )
    )
    for membership, organization_id in workspace_rows.all():
        role = ScopeRole(membership.role)
        workspace_roles[membership.workspace_id] = role
        workspace_ids.add(membership.workspace_id)
        organization_ids.add(organization_id)

    project_rows = await session.execute(
        select(ProjectMembership, Project.workspace_id, Workspace.organization_id)
        .join(Project, Project.id == ProjectMembership.project_id)
        .join(Workspace, Workspace.id == Project.workspace_id)
        .where(
            ProjectMembership.tenant_id == claims.tenant_id,
            Project.tenant_id == claims.tenant_id,
            Workspace.tenant_id == claims.tenant_id,
            ProjectMembership.user_id == user.id,
            ProjectMembership.status == MembershipStatus.ACTIVE,
        )
    )
    for membership, workspace_id, organization_id in project_rows.all():
        role = ScopeRole(membership.role)
        project_roles[membership.project_id] = role
        project_ids.add(membership.project_id)
        workspace_ids.add(workspace_id)
        organization_ids.add(organization_id)

    return Principal(
        user_id=user.id,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        tenant_name=tenant.name,
        tenant_status=TenantStatus(tenant.status),
        issuer=user.issuer,
        subject=user.subject,
        email=user.email,
        display_name=user.display_name,
        tenant_role=TenantRole(user.tenant_role),
        organization_roles=organization_roles,
        workspace_roles=workspace_roles,
        project_roles=project_roles,
        organization_ids=frozenset(organization_ids),
        workspace_ids=frozenset(workspace_ids),
        project_ids=frozenset(project_ids),
    )
