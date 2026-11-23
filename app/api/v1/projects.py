"""Project endpoints and project-scoped memberships."""

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
from app.db.models import Organization, Project, Workspace
from app.schemas.tenancy import (
    MembershipGrant,
    MembershipRead,
    ProjectRead,
    ProjectUpdate,
)
from app.services.tenancy import (
    ensure_active_user,
    get_project_bundle,
    grant_project_membership,
    list_project_members,
    membership_read,
    project_context,
    revoke_project_membership,
    update_project,
)

router = APIRouter(prefix="/projects", tags=["projects"])


async def _require_project(
    session: AsyncSession,
    project_id: UUID,
) -> tuple[Project, Workspace, Organization]:
    bundle = await get_project_bundle(session, project_id)
    if bundle is None:
        raise NotFoundError
    return bundle


@router.get("/{project_id}", response_model=ProjectRead, summary="Get a project")
async def get_single_project(
    project_id: UUID,
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ProjectRead:
    project, workspace, organization = await _require_project(session, project_id)
    await authz.require(Permission.PROJECT_READ, project_context(project, workspace, organization))
    return ProjectRead.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectRead, summary="Update a project")
async def update_single_project(
    project_id: UUID,
    payload: ProjectUpdate,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ProjectRead:
    project, workspace, organization = await _require_project(session, project_id)
    context = project_context(project, workspace, organization)
    if payload.status is not None and payload.status.value != project.status:
        await authz.require(Permission.PROJECT_MANAGE_STATUS, context)
    if (
        payload.classification is not None
        and payload.classification.value != project.classification
    ):
        await authz.require(Permission.PROJECT_MANAGE_CLASSIFICATION, context)
    await authz.require(Permission.PROJECT_UPDATE, context)
    project = await update_project(session, project=project, payload=payload, principal=principal)
    return ProjectRead.model_validate(project)


@router.get(
    "/{project_id}/members",
    response_model=list[MembershipRead],
    summary="List project members",
)
async def list_project_memberships(
    project_id: UUID,
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[MembershipRead]:
    project, workspace, organization = await _require_project(session, project_id)
    await authz.require(
        Permission.PROJECT_MANAGE_MEMBERS,
        project_context(project, workspace, organization),
    )
    return await list_project_members(session, project_id=project.id, limit=limit, offset=offset)


@router.post(
    "/{project_id}/members",
    response_model=MembershipRead,
    summary="Grant a project role",
)
async def grant_project_role(
    project_id: UUID,
    payload: MembershipGrant,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> MembershipRead:
    project, workspace, organization = await _require_project(session, project_id)
    await authz.require(
        Permission.PROJECT_MANAGE_MEMBERS,
        project_context(project, workspace, organization),
    )
    target_user = await ensure_active_user(session, payload.user_id)
    membership = await grant_project_membership(
        session,
        project=project,
        target_user=target_user,
        role=payload.role,
        principal=principal,
    )
    return membership_read(membership, target_user)


@router.delete(
    "/{project_id}/members/{user_id}",
    status_code=204,
    summary="Revoke a project role",
)
async def revoke_project_role(
    project_id: UUID,
    user_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> Response:
    project, workspace, organization = await _require_project(session, project_id)
    await authz.require(
        Permission.PROJECT_MANAGE_MEMBERS,
        project_context(project, workspace, organization),
    )
    await revoke_project_membership(session, project=project, user_id=user_id, principal=principal)
    return Response(status_code=204)
