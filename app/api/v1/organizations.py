"""组织接口与组织级成员关系。"""

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
from app.db.models import Organization
from app.schemas.tenancy import (
    MembershipGrant,
    MembershipRead,
    OrganizationCreate,
    OrganizationRead,
    OrganizationUpdate,
    WorkspaceCreate,
    WorkspaceRead,
)
from app.services.tenancy import (
    create_organization,
    create_workspace,
    ensure_active_user,
    get_organization,
    grant_organization_membership,
    list_organization_members,
    list_organizations,
    list_workspaces,
    membership_read,
    organization_context,
    revoke_organization_membership,
    update_organization,
)

router = APIRouter(prefix="/organizations", tags=["organizations"])


async def _require_organization(
    session: AsyncSession,
    organization_id: UUID,
) -> Organization:
    organization = await get_organization(session, organization_id)
    if organization is None:
        raise NotFoundError
    return organization


@router.get("", response_model=list[OrganizationRead], summary="List accessible organizations")
async def list_accessible_organizations(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[OrganizationRead]:
    organizations = await list_organizations(
        session, principal=principal, limit=limit, offset=offset
    )
    return [OrganizationRead.model_validate(item) for item in organizations]


@router.post(
    "",
    response_model=OrganizationRead,
    status_code=201,
    summary="Create an organization",
)
async def create_new_organization(
    payload: OrganizationCreate,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> OrganizationRead:
    await authz.require_tenant_admin(Permission.ORGANIZATION_CREATE)
    organization = await create_organization(session, payload=payload, principal=principal)
    return OrganizationRead.model_validate(organization)


@router.get("/{organization_id}", response_model=OrganizationRead, summary="Get an organization")
async def get_single_organization(
    organization_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> OrganizationRead:
    organization = await _require_organization(session, organization_id)
    await authz.require(Permission.ORGANIZATION_READ, organization_context(organization))
    return OrganizationRead.model_validate(organization)


@router.patch(
    "/{organization_id}",
    response_model=OrganizationRead,
    summary="Update an organization",
)
async def update_single_organization(
    organization_id: UUID,
    payload: OrganizationUpdate,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> OrganizationRead:
    organization = await _require_organization(session, organization_id)
    await authz.require(Permission.ORGANIZATION_UPDATE, organization_context(organization))
    organization = await update_organization(
        session, organization=organization, payload=payload, principal=principal
    )
    return OrganizationRead.model_validate(organization)


@router.get(
    "/{organization_id}/workspaces",
    response_model=list[WorkspaceRead],
    summary="List workspaces in an organization",
)
async def list_organization_workspaces(
    organization_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[WorkspaceRead]:
    organization = await _require_organization(session, organization_id)
    await authz.require(Permission.ORGANIZATION_READ, organization_context(organization))
    workspaces = await list_workspaces(
        session,
        organization=organization,
        principal=principal,
        limit=limit,
        offset=offset,
    )
    return [WorkspaceRead.model_validate(item) for item in workspaces]


@router.post(
    "/{organization_id}/workspaces",
    response_model=WorkspaceRead,
    status_code=201,
    summary="Create a workspace in an organization",
)
async def create_organization_workspace(
    organization_id: UUID,
    payload: WorkspaceCreate,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> WorkspaceRead:
    organization = await _require_organization(session, organization_id)
    await authz.require(Permission.WORKSPACE_CREATE, organization_context(organization))
    workspace = await create_workspace(
        session, organization=organization, payload=payload, principal=principal
    )
    return WorkspaceRead.model_validate(workspace)


@router.get(
    "/{organization_id}/members",
    response_model=list[MembershipRead],
    summary="List organization members",
)
async def list_organization_memberships(
    organization_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[MembershipRead]:
    organization = await _require_organization(session, organization_id)
    await authz.require(Permission.ORGANIZATION_MANAGE_MEMBERS, organization_context(organization))
    return await list_organization_members(
        session, organization_id=organization.id, limit=limit, offset=offset
    )


@router.post(
    "/{organization_id}/members",
    response_model=MembershipRead,
    summary="Grant an organization role",
)
async def grant_organization_role(
    organization_id: UUID,
    payload: MembershipGrant,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> MembershipRead:
    organization = await _require_organization(session, organization_id)
    await authz.require(Permission.ORGANIZATION_MANAGE_MEMBERS, organization_context(organization))
    target_user = await ensure_active_user(session, payload.user_id)
    membership = await grant_organization_membership(
        session,
        organization=organization,
        target_user=target_user,
        role=payload.role,
        principal=principal,
    )
    return membership_read(membership, target_user)


@router.delete(
    "/{organization_id}/members/{user_id}",
    status_code=204,
    summary="Revoke an organization role",
)
async def revoke_organization_role(
    organization_id: UUID,
    user_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> Response:
    organization = await _require_organization(session, organization_id)
    await authz.require(Permission.ORGANIZATION_MANAGE_MEMBERS, organization_context(organization))
    await revoke_organization_membership(
        session, organization=organization, user_id=user_id, principal=principal
    )
    return Response(status_code=204)
