"""Workspace endpoints and workspace-scoped memberships."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.auth.authorization import AuthorizationService
from app.auth.dependencies import get_authorization, get_principal
from app.auth.enums import Permission
from app.auth.principal import Principal
from app.core.errors import NotFoundError
from app.db.models import Organization, Workspace
from app.schemas.tenancy import (
    MembershipGrant,
    MembershipRead,
    ProjectCreate,
    ProjectRead,
    WorkspaceRead,
    WorkspaceUpdate,
)
from app.services.tenancy import (
    create_project,
    ensure_active_user,
    get_workspace_bundle,
    grant_workspace_membership,
    list_projects,
    list_workspace_members,
    membership_read,
    revoke_workspace_membership,
    update_workspace,
    workspace_context,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


async def _require_workspace(
    session: AsyncSession,
    workspace_id: UUID,
) -> tuple[Workspace, Organization]:
    bundle = await get_workspace_bundle(session, workspace_id)
    if bundle is None:
        raise NotFoundError
    return bundle


@router.get("/{workspace_id}", response_model=WorkspaceRead, summary="Get a workspace")
async def get_single_workspace(
    workspace_id: UUID,
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> WorkspaceRead:
    workspace, organization = await _require_workspace(session, workspace_id)
    await authz.require(Permission.WORKSPACE_READ, workspace_context(workspace, organization))
    return WorkspaceRead.model_validate(workspace)


@router.patch("/{workspace_id}", response_model=WorkspaceRead, summary="Update a workspace")
async def update_single_workspace(
    workspace_id: UUID,
    payload: WorkspaceUpdate,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> WorkspaceRead:
    workspace, organization = await _require_workspace(session, workspace_id)
    context = workspace_context(workspace, organization)
    if payload.status is not None and payload.status.value != workspace.status:
        await authz.require(Permission.WORKSPACE_MANAGE_STATUS, context)
    await authz.require(Permission.WORKSPACE_UPDATE, context)
    workspace = await update_workspace(
        session, workspace=workspace, payload=payload, principal=principal
    )
    return WorkspaceRead.model_validate(workspace)


@router.get(
    "/{workspace_id}/projects",
    response_model=list[ProjectRead],
    summary="List projects in a workspace",
)
async def list_workspace_projects(
    workspace_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ProjectRead]:
    workspace, organization = await _require_workspace(session, workspace_id)
    await authz.require(Permission.WORKSPACE_READ, workspace_context(workspace, organization))
    projects = await list_projects(
        session,
        workspace=workspace,
        organization=organization,
        principal=principal,
        limit=limit,
        offset=offset,
    )
    return [ProjectRead.model_validate(item) for item in projects]


@router.post(
    "/{workspace_id}/projects",
    response_model=ProjectRead,
    status_code=201,
    summary="Create a project in a workspace",
)
async def create_workspace_project(
    workspace_id: UUID,
    payload: ProjectCreate,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ProjectRead:
    workspace, organization = await _require_workspace(session, workspace_id)
    await authz.require(Permission.PROJECT_CREATE, workspace_context(workspace, organization))
    project = await create_project(
        session, workspace=workspace, payload=payload, principal=principal
    )
    return ProjectRead.model_validate(project)


@router.get(
    "/{workspace_id}/members",
    response_model=list[MembershipRead],
    summary="List workspace members",
)
async def list_workspace_memberships(
    workspace_id: UUID,
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[MembershipRead]:
    workspace, organization = await _require_workspace(session, workspace_id)
    await authz.require(
        Permission.WORKSPACE_MANAGE_MEMBERS,
        workspace_context(workspace, organization),
    )
    return await list_workspace_members(
        session, workspace_id=workspace.id, limit=limit, offset=offset
    )


@router.post(
    "/{workspace_id}/members",
    response_model=MembershipRead,
    summary="Grant a workspace role",
)
async def grant_workspace_role(
    workspace_id: UUID,
    payload: MembershipGrant,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> MembershipRead:
    workspace, organization = await _require_workspace(session, workspace_id)
    await authz.require(
        Permission.WORKSPACE_MANAGE_MEMBERS,
        workspace_context(workspace, organization),
    )
    target_user = await ensure_active_user(session, payload.user_id)
    membership = await grant_workspace_membership(
        session,
        workspace=workspace,
        target_user=target_user,
        role=payload.role,
        principal=principal,
    )
    return membership_read(membership, target_user)


@router.delete(
    "/{workspace_id}/members/{user_id}",
    status_code=204,
    summary="Revoke a workspace role",
)
async def revoke_workspace_role(
    workspace_id: UUID,
    user_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> Response:
    workspace, organization = await _require_workspace(session, workspace_id)
    await authz.require(
        Permission.WORKSPACE_MANAGE_MEMBERS,
        workspace_context(workspace, organization),
    )
    await revoke_workspace_membership(
        session, workspace=workspace, user_id=user_id, principal=principal
    )
    return Response(status_code=204)
