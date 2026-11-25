"""项目评论与审批节点接口。"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.auth.authorization import AuthorizationService, ResourceContext
from app.auth.dependencies import get_authorization, get_principal
from app.auth.enums import Permission
from app.auth.principal import Principal
from app.collaboration.enums import ApprovalStatus
from app.collaboration.service import (
    cancel_approval,
    create_approval_request,
    create_comment,
    decide_approval,
    delete_comment,
    get_approval,
    get_comment,
    list_approvals,
    list_comments,
    update_comment,
)
from app.core.errors import ForbiddenError, NotFoundError
from app.db.models import Project
from app.schemas.collaboration import (
    ApprovalCreate,
    ApprovalDecision,
    ApprovalRead,
    CommentCreate,
    CommentRead,
    CommentUpdate,
)
from app.services.tenancy import get_project_bundle, project_context

router = APIRouter(tags=["collaboration"])


async def _require_project(
    session: AsyncSession,
    project_id: UUID,
) -> tuple[Project, ResourceContext]:
    bundle = await get_project_bundle(session, project_id)
    if bundle is None:
        raise NotFoundError("The project was not found.")
    project, _, _ = bundle
    return project, project_context(*bundle)


@router.get(
    "/projects/{project_id}/comments",
    response_model=list[CommentRead],
    summary="List project comments",
)
async def get_project_comments(
    project_id: UUID,
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[CommentRead]:
    project, context = await _require_project(session, project_id)
    await authz.require(Permission.COMMENT_CREATE, context)
    comments = await list_comments(
        session,
        project_id=project.id,
        limit=limit,
        offset=offset,
    )
    return [CommentRead.model_validate(comment) for comment in comments]


@router.post(
    "/projects/{project_id}/comments",
    response_model=CommentRead,
    status_code=201,
    summary="Create a project comment",
)
async def post_project_comment(
    project_id: UUID,
    payload: CommentCreate,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> CommentRead:
    project, context = await _require_project(session, project_id)
    await authz.require(Permission.COMMENT_CREATE, context)
    comment = await create_comment(
        session,
        project=project,
        principal=principal,
        body=payload.body,
        parent_id=payload.parent_id,
    )
    return CommentRead.model_validate(comment)


@router.patch(
    "/projects/{project_id}/comments/{comment_id}",
    response_model=CommentRead,
    summary="Edit a project comment",
)
async def patch_project_comment(
    project_id: UUID,
    comment_id: UUID,
    payload: CommentUpdate,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> CommentRead:
    project, context = await _require_project(session, project_id)
    comment = await get_comment(session, project_id=project.id, comment_id=comment_id)
    if comment is None:
        raise NotFoundError("The comment was not found.")
    if comment.author_user_id != principal.user_id:
        await authz.require(Permission.COMMENT_MANAGE, context)
    comment = await update_comment(
        session,
        comment=comment,
        principal=principal,
        body=payload.body,
    )
    return CommentRead.model_validate(comment)


@router.delete(
    "/projects/{project_id}/comments/{comment_id}",
    status_code=204,
    summary="Delete a project comment",
)
async def remove_project_comment(
    project_id: UUID,
    comment_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> None:
    project, context = await _require_project(session, project_id)
    comment = await get_comment(session, project_id=project.id, comment_id=comment_id)
    if comment is None:
        raise NotFoundError("The comment was not found.")
    if comment.author_user_id != principal.user_id:
        await authz.require(Permission.COMMENT_MANAGE, context)
    await delete_comment(session, comment=comment, principal=principal)
    return None


@router.get(
    "/projects/{project_id}/approval-requests",
    response_model=list[ApprovalRead],
    summary="List project approval nodes",
)
async def get_project_approvals(
    project_id: UUID,
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status: ApprovalStatus | None = Query(default=None),
    assignee_user_id: UUID | None = Query(default=None),
) -> list[ApprovalRead]:
    project, context = await _require_project(session, project_id)
    await authz.require(Permission.APPROVAL_READ, context)
    approvals = await list_approvals(
        session,
        project_id=project.id,
        limit=limit,
        offset=offset,
        status=status,
        assignee_user_id=assignee_user_id,
    )
    return [ApprovalRead.model_validate(approval) for approval in approvals]


@router.post(
    "/projects/{project_id}/approval-requests",
    response_model=ApprovalRead,
    status_code=201,
    summary="Create a project approval node",
)
async def post_project_approval(
    project_id: UUID,
    payload: ApprovalCreate,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ApprovalRead:
    project, context = await _require_project(session, project_id)
    await authz.require(Permission.APPROVAL_CREATE, context)
    approval = await create_approval_request(
        session,
        project=project,
        principal=principal,
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        assignee_user_id=payload.assignee_user_id,
        request_note=payload.request_note,
        due_at=payload.due_at,
    )
    return ApprovalRead.model_validate(approval)


async def _decide(
    *,
    session: AsyncSession,
    project: Project,
    context: ResourceContext,
    approval_id: UUID,
    principal: Principal,
    authz: AuthorizationService,
    approved: bool,
    note: str | None,
) -> ApprovalRead:
    approval = await get_approval(
        session,
        project_id=project.id,
        approval_id=approval_id,
    )
    if approval is None:
        raise NotFoundError("The approval request was not found.")
    await authz.require(Permission.APPROVAL_DECIDE, context)
    if approval.assignee_user_id != principal.user_id and not principal.is_tenant_admin:
        admin_decision = authz.evaluate(Permission.PROJECT_MANAGE_MEMBERS, context)
        if not admin_decision.allowed:
            raise ForbiddenError
    approval = await decide_approval(
        session,
        approval=approval,
        principal=principal,
        approved=approved,
        note=note,
    )
    return ApprovalRead.model_validate(approval)


@router.post(
    "/projects/{project_id}/approval-requests/{approval_id}/approve",
    response_model=ApprovalRead,
    summary="Approve an approval node",
)
async def approve_project_approval(
    project_id: UUID,
    approval_id: UUID,
    payload: ApprovalDecision,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ApprovalRead:
    project, context = await _require_project(session, project_id)
    return await _decide(
        session=session,
        project=project,
        context=context,
        approval_id=approval_id,
        principal=principal,
        authz=authz,
        approved=True,
        note=payload.note,
    )


@router.post(
    "/projects/{project_id}/approval-requests/{approval_id}/cancel",
    response_model=ApprovalRead,
    summary="Cancel an approval node",
)
async def cancel_project_approval(
    project_id: UUID,
    approval_id: UUID,
    payload: ApprovalDecision,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ApprovalRead:
    project, context = await _require_project(session, project_id)
    approval = await get_approval(
        session,
        project_id=project.id,
        approval_id=approval_id,
    )
    if approval is None:
        raise NotFoundError("The approval request was not found.")
    await authz.require(Permission.APPROVAL_CREATE, context)
    if approval.requested_by != principal.user_id and not principal.is_tenant_admin:
        admin_decision = authz.evaluate(Permission.PROJECT_MANAGE_MEMBERS, context)
        if not admin_decision.allowed:
            raise ForbiddenError
    approval = await cancel_approval(
        session,
        approval=approval,
        principal=principal,
        note=payload.note,
    )
    return ApprovalRead.model_validate(approval)


@router.post(
    "/projects/{project_id}/approval-requests/{approval_id}/reject",
    response_model=ApprovalRead,
    summary="Reject an approval node",
)
async def reject_project_approval(
    project_id: UUID,
    approval_id: UUID,
    payload: ApprovalDecision,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ApprovalRead:
    project, context = await _require_project(session, project_id)
    return await _decide(
        session=session,
        project=project,
        context=context,
        approval_id=approval_id,
        principal=principal,
        authz=authz,
        approved=False,
        note=payload.note,
    )
