"""组织、工作区、项目与成员关系操作。"""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import ResourceContext
from app.auth.enums import (
    AuditAction,
    MembershipStatus,
    ResourceType,
    ScopeRole,
    UserStatus,
)
from app.auth.principal import Principal
from app.core.context import require_tenant_id
from app.core.errors import ConflictError, NotFoundError
from app.db.models import (
    Organization,
    OrganizationMembership,
    Project,
    ProjectMembership,
    User,
    Workspace,
    WorkspaceMembership,
)
from app.db.tenant_scope import tenant_scoped_select
from app.schemas.tenancy import (
    MembershipRead,
    OrganizationCreate,
    OrganizationUpdate,
    ProjectCreate,
    ProjectUpdate,
    WorkspaceCreate,
    WorkspaceUpdate,
)
from app.services.audit import record_event


def organization_context(organization: Organization) -> ResourceContext:
    return ResourceContext(
        resource_type=ResourceType.ORGANIZATION,
        organization_id=organization.id,
        organization_status=organization.status,
    )


def workspace_context(workspace: Workspace, organization: Organization) -> ResourceContext:
    return ResourceContext(
        resource_type=ResourceType.WORKSPACE,
        organization_id=organization.id,
        organization_status=organization.status,
        workspace_id=workspace.id,
        workspace_status=workspace.status,
    )


def project_context(
    project: Project,
    workspace: Workspace,
    organization: Organization,
) -> ResourceContext:
    return ResourceContext(
        resource_type=ResourceType.PROJECT,
        organization_id=organization.id,
        organization_status=organization.status,
        workspace_id=workspace.id,
        workspace_status=workspace.status,
        project_id=project.id,
        project_status=project.status,
        project_classification=project.classification,
    )


async def get_organization(session: AsyncSession, organization_id: UUID) -> Organization | None:
    return cast(
        Organization | None,
        await session.scalar(
            tenant_scoped_select(Organization).where(Organization.id == organization_id)
        ),
    )


async def get_workspace_bundle(
    session: AsyncSession,
    workspace_id: UUID,
) -> tuple[Workspace, Organization] | None:
    tenant_id = require_tenant_id()
    row = (
        await session.execute(
            select(Workspace, Organization)
            .join(Organization, Organization.id == Workspace.organization_id)
            .where(
                Workspace.tenant_id == tenant_id,
                Organization.tenant_id == tenant_id,
                Workspace.id == workspace_id,
            )
        )
    ).first()
    if row is None:
        return None
    return row[0], row[1]


async def get_project_bundle(
    session: AsyncSession,
    project_id: UUID,
) -> tuple[Project, Workspace, Organization] | None:
    tenant_id = require_tenant_id()
    row = (
        await session.execute(
            select(Project, Workspace, Organization)
            .join(Workspace, Workspace.id == Project.workspace_id)
            .join(Organization, Organization.id == Workspace.organization_id)
            .where(
                Project.tenant_id == tenant_id,
                Workspace.tenant_id == tenant_id,
                Organization.tenant_id == tenant_id,
                Project.id == project_id,
            )
        )
    ).first()
    if row is None:
        return None
    return row[0], row[1], row[2]


async def get_user(session: AsyncSession, user_id: UUID) -> User | None:
    return cast(
        User | None,
        await session.scalar(tenant_scoped_select(User).where(User.id == user_id)),
    )


async def create_organization(
    session: AsyncSession,
    *,
    payload: OrganizationCreate,
    principal: Principal,
) -> Organization:
    organization = Organization(
        tenant_id=principal.tenant_id,
        slug=payload.slug,
        name=payload.name,
    )
    session.add(organization)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError("An organization with this slug already exists.") from exc
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.ORGANIZATION_CREATED,
        resource_type=ResourceType.ORGANIZATION,
        resource_id=organization.id,
        details={"slug": organization.slug},
    )
    return organization


async def update_organization(
    session: AsyncSession,
    *,
    organization: Organization,
    payload: OrganizationUpdate,
    principal: Principal,
) -> Organization:
    if payload.name is not None:
        organization.name = payload.name
    if payload.status is not None:
        organization.status = payload.status.value
    await session.flush()
    await session.refresh(organization)
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.ORGANIZATION_UPDATED,
        resource_type=ResourceType.ORGANIZATION,
        resource_id=organization.id,
        details={
            "status": organization.status,
            "name_changed": payload.name is not None,
        },
    )
    return organization


async def create_workspace(
    session: AsyncSession,
    *,
    organization: Organization,
    payload: WorkspaceCreate,
    principal: Principal,
) -> Workspace:
    workspace = Workspace(
        tenant_id=principal.tenant_id,
        organization_id=organization.id,
        slug=payload.slug,
        name=payload.name,
    )
    session.add(workspace)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError("A workspace with this slug already exists.") from exc
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.WORKSPACE_CREATED,
        resource_type=ResourceType.WORKSPACE,
        resource_id=workspace.id,
        details={"slug": workspace.slug, "organization_id": str(organization.id)},
    )
    return workspace


async def update_workspace(
    session: AsyncSession,
    *,
    workspace: Workspace,
    payload: WorkspaceUpdate,
    principal: Principal,
) -> Workspace:
    if payload.name is not None:
        workspace.name = payload.name
    if payload.status is not None:
        workspace.status = payload.status.value
    await session.flush()
    await session.refresh(workspace)
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.WORKSPACE_UPDATED,
        resource_type=ResourceType.WORKSPACE,
        resource_id=workspace.id,
        details={"status": workspace.status, "name_changed": payload.name is not None},
    )
    return workspace


async def create_project(
    session: AsyncSession,
    *,
    workspace: Workspace,
    payload: ProjectCreate,
    principal: Principal,
) -> Project:
    project = Project(
        tenant_id=principal.tenant_id,
        workspace_id=workspace.id,
        slug=payload.slug,
        name=payload.name,
        classification=payload.classification.value,
    )
    session.add(project)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError("A project with this slug already exists.") from exc
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.PROJECT_CREATED,
        resource_type=ResourceType.PROJECT,
        resource_id=project.id,
        details={
            "slug": project.slug,
            "workspace_id": str(workspace.id),
            "classification": project.classification,
        },
    )
    return project


async def update_project(
    session: AsyncSession,
    *,
    project: Project,
    payload: ProjectUpdate,
    principal: Principal,
) -> Project:
    if payload.name is not None:
        project.name = payload.name
    if payload.status is not None:
        project.status = payload.status.value
    if payload.classification is not None:
        project.classification = payload.classification.value
    await session.flush()
    await session.refresh(project)
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.PROJECT_UPDATED,
        resource_type=ResourceType.PROJECT,
        resource_id=project.id,
        details={
            "status": project.status,
            "classification": project.classification,
            "name_changed": payload.name is not None,
        },
    )
    return project


async def list_organizations(
    session: AsyncSession,
    *,
    principal: Principal,
    limit: int,
    offset: int,
) -> list[Organization]:
    statement = tenant_scoped_select(Organization).order_by(
        Organization.created_at.desc(), Organization.id.desc()
    )
    if not principal.is_tenant_admin:
        if not principal.organization_ids:
            return []
        statement = statement.where(Organization.id.in_(principal.organization_ids))
    return list((await session.scalars(statement.limit(limit).offset(offset))).all())


async def list_workspaces(
    session: AsyncSession,
    *,
    organization: Organization,
    principal: Principal,
    limit: int,
    offset: int,
) -> list[Workspace]:
    statement = (
        tenant_scoped_select(Workspace)
        .where(Workspace.organization_id == organization.id)
        .order_by(Workspace.created_at.desc(), Workspace.id.desc())
    )
    has_org_scope = principal.is_tenant_admin or organization.id in principal.organization_roles
    if not has_org_scope:
        if not principal.workspace_ids:
            return []
        statement = statement.where(Workspace.id.in_(principal.workspace_ids))
    return list((await session.scalars(statement.limit(limit).offset(offset))).all())


async def list_projects(
    session: AsyncSession,
    *,
    workspace: Workspace,
    organization: Organization,
    principal: Principal,
    limit: int,
    offset: int,
) -> list[Project]:
    statement = (
        tenant_scoped_select(Project)
        .where(Project.workspace_id == workspace.id)
        .order_by(Project.created_at.desc(), Project.id.desc())
    )
    has_workspace_scope = (
        principal.is_tenant_admin
        or organization.id in principal.organization_roles
        or workspace.id in principal.workspace_roles
    )
    if not has_workspace_scope:
        accessible = principal.project_ids
        if not accessible:
            return []
        statement = statement.where(Project.id.in_(accessible))
    return list((await session.scalars(statement.limit(limit).offset(offset))).all())


async def get_organization_membership(
    session: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
) -> OrganizationMembership | None:
    return cast(
        OrganizationMembership | None,
        await session.scalar(
            tenant_scoped_select(OrganizationMembership).where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id == user_id,
            )
        ),
    )


async def get_workspace_membership(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    user_id: UUID,
) -> WorkspaceMembership | None:
    return cast(
        WorkspaceMembership | None,
        await session.scalar(
            tenant_scoped_select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == user_id,
            )
        ),
    )


async def get_project_membership(
    session: AsyncSession,
    *,
    project_id: UUID,
    user_id: UUID,
) -> ProjectMembership | None:
    return cast(
        ProjectMembership | None,
        await session.scalar(
            tenant_scoped_select(ProjectMembership).where(
                ProjectMembership.project_id == project_id,
                ProjectMembership.user_id == user_id,
            )
        ),
    )


async def grant_organization_membership(
    session: AsyncSession,
    *,
    organization: Organization,
    target_user: User,
    role: ScopeRole,
    principal: Principal,
) -> OrganizationMembership:
    membership = await get_organization_membership(
        session, organization_id=organization.id, user_id=target_user.id
    )
    if membership is None:
        membership = OrganizationMembership(
            tenant_id=principal.tenant_id,
            organization_id=organization.id,
            user_id=target_user.id,
            role=role.value,
            status=MembershipStatus.ACTIVE.value,
        )
        session.add(membership)
    else:
        if (
            membership.role == ScopeRole.ADMIN
            and membership.status == MembershipStatus.ACTIVE
            and role != ScopeRole.ADMIN
        ):
            await _ensure_other_organization_admin(
                session,
                organization_id=organization.id,
                excluding_membership_id=membership.id,
            )
        membership.role = role.value
        membership.status = MembershipStatus.ACTIVE.value
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError("The membership could not be saved.") from exc
    await session.refresh(membership)
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.MEMBERSHIP_GRANTED,
        resource_type=ResourceType.ORGANIZATION,
        resource_id=organization.id,
        details={
            "scope": "organization",
            "user_id": str(target_user.id),
            "role": role.value,
        },
    )
    return membership


async def grant_workspace_membership(
    session: AsyncSession,
    *,
    workspace: Workspace,
    target_user: User,
    role: ScopeRole,
    principal: Principal,
) -> WorkspaceMembership:
    membership = await get_workspace_membership(
        session, workspace_id=workspace.id, user_id=target_user.id
    )
    if membership is None:
        membership = WorkspaceMembership(
            tenant_id=principal.tenant_id,
            workspace_id=workspace.id,
            user_id=target_user.id,
            role=role.value,
            status=MembershipStatus.ACTIVE.value,
        )
        session.add(membership)
    else:
        membership.role = role.value
        membership.status = MembershipStatus.ACTIVE.value
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError("The membership could not be saved.") from exc
    await session.refresh(membership)
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.MEMBERSHIP_GRANTED,
        resource_type=ResourceType.WORKSPACE,
        resource_id=workspace.id,
        details={"scope": "workspace", "user_id": str(target_user.id), "role": role.value},
    )
    return membership


async def grant_project_membership(
    session: AsyncSession,
    *,
    project: Project,
    target_user: User,
    role: ScopeRole,
    principal: Principal,
) -> ProjectMembership:
    membership = await get_project_membership(
        session, project_id=project.id, user_id=target_user.id
    )
    if membership is None:
        membership = ProjectMembership(
            tenant_id=principal.tenant_id,
            project_id=project.id,
            user_id=target_user.id,
            role=role.value,
            status=MembershipStatus.ACTIVE.value,
        )
        session.add(membership)
    else:
        membership.role = role.value
        membership.status = MembershipStatus.ACTIVE.value
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError("The membership could not be saved.") from exc
    await session.refresh(membership)
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.MEMBERSHIP_GRANTED,
        resource_type=ResourceType.PROJECT,
        resource_id=project.id,
        details={"scope": "project", "user_id": str(target_user.id), "role": role.value},
    )
    return membership


async def revoke_organization_membership(
    session: AsyncSession,
    *,
    organization: Organization,
    user_id: UUID,
    principal: Principal,
) -> None:
    membership = await get_organization_membership(
        session, organization_id=organization.id, user_id=user_id
    )
    if membership is None or membership.status != MembershipStatus.ACTIVE:
        raise NotFoundError("The membership was not found.")
    if membership.role == ScopeRole.ADMIN:
        await _ensure_other_organization_admin(
            session,
            organization_id=organization.id,
            excluding_membership_id=membership.id,
        )
    membership.status = MembershipStatus.REVOKED.value
    await session.flush()
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.MEMBERSHIP_REVOKED,
        resource_type=ResourceType.ORGANIZATION,
        resource_id=organization.id,
        details={"scope": "organization", "user_id": str(user_id)},
    )


async def revoke_workspace_membership(
    session: AsyncSession,
    *,
    workspace: Workspace,
    user_id: UUID,
    principal: Principal,
) -> None:
    membership = await get_workspace_membership(session, workspace_id=workspace.id, user_id=user_id)
    if membership is None or membership.status != MembershipStatus.ACTIVE:
        raise NotFoundError("The membership was not found.")
    membership.status = MembershipStatus.REVOKED.value
    await session.flush()
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.MEMBERSHIP_REVOKED,
        resource_type=ResourceType.WORKSPACE,
        resource_id=workspace.id,
        details={"scope": "workspace", "user_id": str(user_id)},
    )


async def revoke_project_membership(
    session: AsyncSession,
    *,
    project: Project,
    user_id: UUID,
    principal: Principal,
) -> None:
    membership = await get_project_membership(session, project_id=project.id, user_id=user_id)
    if membership is None or membership.status != MembershipStatus.ACTIVE:
        raise NotFoundError("The membership was not found.")
    membership.status = MembershipStatus.REVOKED.value
    await session.flush()
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.MEMBERSHIP_REVOKED,
        resource_type=ResourceType.PROJECT,
        resource_id=project.id,
        details={"scope": "project", "user_id": str(user_id)},
    )


async def ensure_active_user(session: AsyncSession, user_id: UUID) -> User:
    user = await get_user(session, user_id)
    if user is None:
        raise NotFoundError("The user was not found in this tenant.")
    if user.status != UserStatus.ACTIVE:
        raise ConflictError("The user is not active.")
    return user


async def list_organization_members(
    session: AsyncSession,
    *,
    organization_id: UUID,
    limit: int,
    offset: int,
) -> list[MembershipRead]:
    tenant_id = require_tenant_id()
    rows = (
        await session.execute(
            select(OrganizationMembership, User)
            .join(User, User.id == OrganizationMembership.user_id)
            .where(
                OrganizationMembership.tenant_id == tenant_id,
                User.tenant_id == tenant_id,
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.status == MembershipStatus.ACTIVE,
            )
            .order_by(User.display_name, User.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [membership_read(membership, user) for membership, user in rows]


async def list_workspace_members(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    limit: int,
    offset: int,
) -> list[MembershipRead]:
    tenant_id = require_tenant_id()
    rows = (
        await session.execute(
            select(WorkspaceMembership, User)
            .join(User, User.id == WorkspaceMembership.user_id)
            .where(
                WorkspaceMembership.tenant_id == tenant_id,
                User.tenant_id == tenant_id,
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.status == MembershipStatus.ACTIVE,
            )
            .order_by(User.display_name, User.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [membership_read(membership, user) for membership, user in rows]


async def list_project_members(
    session: AsyncSession,
    *,
    project_id: UUID,
    limit: int,
    offset: int,
) -> list[MembershipRead]:
    tenant_id = require_tenant_id()
    rows = (
        await session.execute(
            select(ProjectMembership, User)
            .join(User, User.id == ProjectMembership.user_id)
            .where(
                ProjectMembership.tenant_id == tenant_id,
                User.tenant_id == tenant_id,
                ProjectMembership.project_id == project_id,
                ProjectMembership.status == MembershipStatus.ACTIVE,
            )
            .order_by(User.display_name, User.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [membership_read(membership, user) for membership, user in rows]


def membership_read(
    membership: OrganizationMembership | WorkspaceMembership | ProjectMembership,
    user: User,
) -> MembershipRead:
    return MembershipRead(
        id=membership.id,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=ScopeRole(membership.role),
        status=MembershipStatus(membership.status),
        created_at=membership.created_at,
        updated_at=membership.updated_at,
    )


async def _ensure_other_organization_admin(
    session: AsyncSession,
    *,
    organization_id: UUID,
    excluding_membership_id: UUID,
) -> None:
    count = await session.scalar(
        select(func.count())
        .select_from(OrganizationMembership)
        .where(
            OrganizationMembership.tenant_id == require_tenant_id(),
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.role == ScopeRole.ADMIN,
            OrganizationMembership.status == MembershipStatus.ACTIVE,
            OrganizationMembership.id != excluding_membership_id,
        )
    )
    if not count:
        raise ConflictError("The organization must retain at least one admin.")
