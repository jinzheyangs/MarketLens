"""项目评论与审批节点服务。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.enums import AuditAction, ResourceType
from app.auth.principal import Principal
from app.collaboration.enums import ApprovalResourceType, ApprovalStatus, CommentStatus
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.db.models import (
    ApprovalRequest,
    Project,
    ProjectComment,
    Report,
    ReportVersion,
    ResearchRun,
    User,
)
from app.db.tenant_scope import tenant_scoped_select
from app.services.audit import record_event


async def create_comment(
    session: AsyncSession,
    *,
    project: Project,
    principal: Principal,
    body: str,
    parent_id: UUID | None,
) -> ProjectComment:
    candidate = body.strip()
    if not candidate or len(candidate) > 5_000:
        raise BadRequestError("comment body must be 1-5000 characters")
    if parent_id is not None:
        parent = await session.scalar(
            tenant_scoped_select(ProjectComment).where(
                ProjectComment.id == parent_id,
                ProjectComment.project_id == project.id,
                ProjectComment.status == CommentStatus.ACTIVE.value,
            )
        )
        if parent is None:
            raise NotFoundError("The parent comment was not found.")
    comment = ProjectComment(
        id=uuid4(),
        tenant_id=principal.tenant_id,
        workspace_id=project.workspace_id,
        project_id=project.id,
        author_user_id=principal.user_id,
        parent_id=parent_id,
        body=candidate,
    )
    session.add(comment)
    await session.flush()
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.COMMENT_CREATED,
        resource_type=ResourceType.PROJECT_COMMENT,
        resource_id=comment.id,
        details={
            "project_id": str(project.id),
            "parent_id": str(parent_id) if parent_id else None,
        },
    )
    await session.refresh(comment)
    return comment


async def list_comments(
    session: AsyncSession,
    *,
    project_id: UUID,
    limit: int,
    offset: int,
) -> list[ProjectComment]:
    return list(
        (
            await session.scalars(
                tenant_scoped_select(ProjectComment)
                .where(
                    ProjectComment.project_id == project_id,
                    ProjectComment.status == CommentStatus.ACTIVE.value,
                )
                .order_by(ProjectComment.created_at, ProjectComment.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )


async def get_comment(
    session: AsyncSession,
    *,
    project_id: UUID,
    comment_id: UUID,
) -> ProjectComment | None:
    return cast(
        ProjectComment | None,
        await session.scalar(
            tenant_scoped_select(ProjectComment).where(
                ProjectComment.project_id == project_id,
                ProjectComment.id == comment_id,
                ProjectComment.status == CommentStatus.ACTIVE.value,
            )
        ),
    )


async def update_comment(
    session: AsyncSession,
    *,
    comment: ProjectComment,
    principal: Principal,
    body: str,
) -> ProjectComment:
    candidate = body.strip()
    if not candidate or len(candidate) > 5_000:
        raise BadRequestError("comment body must be 1-5000 characters")
    comment.body = candidate
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.COMMENT_UPDATED,
        resource_type=ResourceType.PROJECT_COMMENT,
        resource_id=comment.id,
        details={"project_id": str(comment.project_id)},
    )
    await session.flush()
    await session.refresh(comment)
    return comment


async def delete_comment(
    session: AsyncSession,
    *,
    comment: ProjectComment,
    principal: Principal,
) -> None:
    comment.status = CommentStatus.DELETED.value
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.COMMENT_DELETED,
        resource_type=ResourceType.PROJECT_COMMENT,
        resource_id=comment.id,
        details={"project_id": str(comment.project_id)},
    )
    await session.flush()


async def _validate_report_version(
    session: AsyncSession,
    *,
    project: Project,
    resource_id: UUID,
) -> None:
    row = (
        await session.execute(
            select(ReportVersion, Report, ResearchRun)
            .join(Report, Report.id == ReportVersion.report_id)
            .join(ResearchRun, ResearchRun.id == Report.root_run_id)
            .where(
                ReportVersion.tenant_id == project.tenant_id,
                Report.tenant_id == project.tenant_id,
                ResearchRun.tenant_id == project.tenant_id,
                ReportVersion.id == resource_id,
                ResearchRun.project_id == project.id,
            )
        )
    ).first()
    if row is None:
        raise NotFoundError("The report version was not found in this project.")


async def create_approval_request(
    session: AsyncSession,
    *,
    project: Project,
    principal: Principal,
    resource_type: ApprovalResourceType,
    resource_id: UUID,
    assignee_user_id: UUID,
    request_note: str | None,
    due_at: datetime | None,
) -> ApprovalRequest:
    if resource_type is not ApprovalResourceType.REPORT_VERSION:
        raise BadRequestError("unsupported approval resource type")
    await _validate_report_version(session, project=project, resource_id=resource_id)
    assignee = await session.scalar(
        tenant_scoped_select(User).where(
            User.id == assignee_user_id,
            User.status == "active",
        )
    )
    if assignee is None:
        raise NotFoundError("The assignee was not found.")
    existing = await session.scalar(
        tenant_scoped_select(ApprovalRequest).where(
            ApprovalRequest.project_id == project.id,
            ApprovalRequest.resource_type == resource_type.value,
            ApprovalRequest.resource_id == resource_id,
            ApprovalRequest.assignee_user_id == assignee_user_id,
            ApprovalRequest.status == ApprovalStatus.PENDING.value,
        )
    )
    if existing is not None:
        raise ConflictError("A pending approval already exists for this assignee.")
    approval = ApprovalRequest(
        id=uuid4(),
        tenant_id=principal.tenant_id,
        workspace_id=project.workspace_id,
        project_id=project.id,
        resource_type=resource_type.value,
        resource_id=resource_id,
        requested_by=principal.user_id,
        assignee_user_id=assignee_user_id,
        status=ApprovalStatus.PENDING.value,
        request_note=(request_note.strip()[:1_000] if request_note else None),
        due_at=due_at,
    )
    session.add(approval)
    await session.flush()
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.APPROVAL_REQUESTED,
        resource_type=ResourceType.APPROVAL_REQUEST,
        resource_id=approval.id,
        details={
            "project_id": str(project.id),
            "resource_type": approval.resource_type,
            "resource_id": str(resource_id),
            "assignee_user_id": str(assignee_user_id),
        },
    )
    await session.refresh(approval)
    return approval


async def list_approvals(
    session: AsyncSession,
    *,
    project_id: UUID,
    limit: int,
    offset: int,
    status: ApprovalStatus | None = None,
    assignee_user_id: UUID | None = None,
) -> list[ApprovalRequest]:
    statement = tenant_scoped_select(ApprovalRequest).where(
        ApprovalRequest.project_id == project_id
    )
    if status is not None:
        statement = statement.where(ApprovalRequest.status == status.value)
    if assignee_user_id is not None:
        statement = statement.where(ApprovalRequest.assignee_user_id == assignee_user_id)
    statement = statement.order_by(
        ApprovalRequest.created_at.desc(),
        ApprovalRequest.id.desc(),
    )
    return list((await session.scalars(statement.limit(limit).offset(offset))).all())


async def get_approval(
    session: AsyncSession,
    *,
    project_id: UUID,
    approval_id: UUID,
) -> ApprovalRequest | None:
    return cast(
        ApprovalRequest | None,
        await session.scalar(
            tenant_scoped_select(ApprovalRequest).where(
                ApprovalRequest.project_id == project_id,
                ApprovalRequest.id == approval_id,
            )
        ),
    )


async def decide_approval(
    session: AsyncSession,
    *,
    approval: ApprovalRequest,
    principal: Principal,
    approved: bool,
    note: str | None,
) -> ApprovalRequest:
    if approval.status != ApprovalStatus.PENDING.value:
        raise ConflictError("The approval request is no longer pending.")
    approval.status = ApprovalStatus.APPROVED.value if approved else ApprovalStatus.REJECTED.value
    approval.decision_note = note.strip()[:1_000] if note else None
    approval.decided_by = principal.user_id
    approval.decided_at = datetime.now(UTC)
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.APPROVAL_DECIDED,
        resource_type=ResourceType.APPROVAL_REQUEST,
        resource_id=approval.id,
        details={
            "project_id": str(approval.project_id),
            "status": approval.status,
            "resource_type": approval.resource_type,
            "resource_id": str(approval.resource_id),
        },
    )
    await session.flush()
    await session.refresh(approval)
    return approval


async def cancel_approval(
    session: AsyncSession,
    *,
    approval: ApprovalRequest,
    principal: Principal,
    note: str | None,
) -> ApprovalRequest:
    if approval.status != ApprovalStatus.PENDING.value:
        raise ConflictError("The approval request is no longer pending.")
    approval.status = ApprovalStatus.CANCELED.value
    approval.decision_note = note.strip()[:1_000] if note else None
    approval.decided_by = principal.user_id
    approval.decided_at = datetime.now(UTC)
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.APPROVAL_CANCELED,
        resource_type=ResourceType.APPROVAL_REQUEST,
        resource_id=approval.id,
        details={
            "project_id": str(approval.project_id),
            "resource_type": approval.resource_type,
            "resource_id": str(approval.resource_id),
        },
    )
    await session.flush()
    await session.refresh(approval)
    return approval


__all__ = [
    "cancel_approval",
    "create_approval_request",
    "create_comment",
    "decide_approval",
    "delete_comment",
    "get_approval",
    "get_comment",
    "list_approvals",
    "list_comments",
    "update_comment",
]
