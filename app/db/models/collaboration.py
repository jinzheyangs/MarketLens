"""项目评论与审批节点。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.collaboration.enums import ApprovalResourceType, ApprovalStatus, CommentStatus
from app.db.base import Base
from app.db.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

_COMMENT_STATUSES = ", ".join(f"'{status}'" for status in CommentStatus)
_APPROVAL_STATUSES = ", ".join(f"'{status}'" for status in ApprovalStatus)
_APPROVAL_RESOURCES = ", ".join(f"'{value}'" for value in ApprovalResourceType)


class ProjectComment(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "project_comments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_project_comments_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_project_comments_tenant_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_project_comments_tenant_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "parent_id"],
            ["project_comments.tenant_id", "project_comments.id"],
            name="fk_project_comments_tenant_parent",
            ondelete="CASCADE",
        ),
        CheckConstraint(f"status IN ({_COMMENT_STATUSES})", name="status_valid"),
        Index(
            "ix_project_comments_tenant_project_created",
            "tenant_id",
            "project_id",
            "created_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    project_id: Mapped[UUID] = mapped_column(nullable=False)
    author_user_id: Mapped[UUID] = mapped_column(nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=CommentStatus.ACTIVE.value,
        server_default=text(f"'{CommentStatus.ACTIVE}'"),
    )


class ApprovalRequest(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_approval_requests_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_approval_requests_tenant_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_approval_requests_tenant_project",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"status IN ({_APPROVAL_STATUSES})", name="status_valid"),
        CheckConstraint(
            f"resource_type IN ({_APPROVAL_RESOURCES})",
            name="resource_type_valid",
        ),
        Index(
            "ix_approval_requests_tenant_assignee_status",
            "tenant_id",
            "assignee_user_id",
            "status",
        ),
        Index(
            "ix_approval_requests_tenant_project",
            "tenant_id",
            "project_id",
            "created_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    project_id: Mapped[UUID] = mapped_column(nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(nullable=False)
    requested_by: Mapped[UUID] = mapped_column(nullable=False)
    assignee_user_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ApprovalStatus.PENDING.value,
        server_default=text(f"'{ApprovalStatus.PENDING}'"),
    )
    request_note: Mapped[str | None] = mapped_column(String(1_000), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(String(1_000), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[UUID | None] = mapped_column(nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
