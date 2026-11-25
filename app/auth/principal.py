"""请求主体（Principal）：从 PostgreSQL 加载，而非取自 JWT 授权声明。"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    """当前请求的已鉴权主体：身份信息 + 各层级角色与可见范围。"""

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
    # 各层级角色映射：资源 ID -> 角色
    organization_roles: dict[UUID, ScopeRole]
    workspace_roles: dict[UUID, ScopeRole]
    project_roles: dict[UUID, ScopeRole]
    # 主体可见的组织/工作区/项目集合
    organization_ids: frozenset[UUID]
    workspace_ids: frozenset[UUID]
    project_ids: frozenset[UUID]
    # 项目 -> 所属工作区 的映射
    project_workspace_ids: dict[UUID, UUID] = field(default_factory=dict)

    @property
    def is_tenant_admin(self) -> bool:
        """是否为租户管理员。"""
        return self.tenant_role == TenantRole.TENANT_ADMIN


async def resolve_principal(session: AsyncSession, claims: TokenClaims) -> Principal:
    """把已校验令牌映射为处于活跃状态的租户用户及其成员关系。

    租户声明仅作为带签名的查询键；必须在该租户下存在匹配的用户记录，
    且所有层级角色都从数据库加载。JWT 中名为 ``role``/``roles``/
    ``permissions`` 等的声明一律忽略。
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

    return await _build_principal(session, tenant=tenant, user=user)


async def resolve_principal_for_user(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
) -> Principal:
    """为持久化 worker 解析受信任的内部用户。

    仅在 worker 已加载租户范围的运行记录后使用；仍会从 PostgreSQL 重新
    加载活跃租户、用户与成员关系，不相信任何角色/权限声明。
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or tenant.status != TenantStatus.ACTIVE:
        raise ForbiddenError
    user = await session.scalar(
        select(User).where(
            User.tenant_id == tenant_id,
            User.id == user_id,
        )
    )
    if user is None or user.status != UserStatus.ACTIVE:
        raise ForbiddenError
    return await _build_principal(session, tenant=tenant, user=user)


async def _build_principal(
    session: AsyncSession,
    *,
    tenant: Tenant,
    user: User,
) -> Principal:
    """从数据库加载用户在组织/工作区/项目三层的活跃成员关系，组装 Principal。"""
    organization_roles: dict[UUID, ScopeRole] = {}
    workspace_roles: dict[UUID, ScopeRole] = {}
    project_roles: dict[UUID, ScopeRole] = {}
    organization_ids: set[UUID] = set()
    workspace_ids: set[UUID] = set()
    project_ids: set[UUID] = set()
    project_workspace_ids: dict[UUID, UUID] = {}

    # 组织级成员关系
    org_rows = await session.scalars(
        select(OrganizationMembership).where(
            OrganizationMembership.tenant_id == tenant.id,
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.status == MembershipStatus.ACTIVE,
        )
    )
    for membership in org_rows:
        role = ScopeRole(membership.role)
        organization_roles[membership.organization_id] = role
        organization_ids.add(membership.organization_id)

    # 工作区级成员关系（联表带出所属组织）
    workspace_rows = await session.execute(
        select(WorkspaceMembership, Workspace.organization_id)
        .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
        .where(
            WorkspaceMembership.tenant_id == tenant.id,
            Workspace.tenant_id == tenant.id,
            WorkspaceMembership.user_id == user.id,
            WorkspaceMembership.status == MembershipStatus.ACTIVE,
        )
    )
    for membership, organization_id in workspace_rows.all():
        role = ScopeRole(membership.role)
        workspace_roles[membership.workspace_id] = role
        workspace_ids.add(membership.workspace_id)
        organization_ids.add(organization_id)

    # 项目级成员关系（联表带出所属工作区与组织）
    project_rows = await session.execute(
        select(ProjectMembership, Project.workspace_id, Workspace.organization_id)
        .join(Project, Project.id == ProjectMembership.project_id)
        .join(Workspace, Workspace.id == Project.workspace_id)
        .where(
            ProjectMembership.tenant_id == tenant.id,
            Project.tenant_id == tenant.id,
            Workspace.tenant_id == tenant.id,
            ProjectMembership.user_id == user.id,
            ProjectMembership.status == MembershipStatus.ACTIVE,
        )
    )
    for membership, workspace_id, organization_id in project_rows.all():
        role = ScopeRole(membership.role)
        project_roles[membership.project_id] = role
        project_ids.add(membership.project_id)
        project_workspace_ids[membership.project_id] = workspace_id
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
        project_workspace_ids=project_workspace_ids,
    )
