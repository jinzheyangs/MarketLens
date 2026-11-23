"""Projects belong to one workspace and remain inside the same tenant."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.enums import ProjectClassification, ProjectStatus
from app.db.base import Base
from app.db.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Project(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_projects_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "slug",
            name="uq_projects_tenant_workspace_slug",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_projects_tenant_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"status IN ('{ProjectStatus.ACTIVE}', '{ProjectStatus.ARCHIVED}')",
            name="status_valid",
        ),
        CheckConstraint(
            "classification IN "
            f"('{ProjectClassification.STANDARD}', '{ProjectClassification.CONFIDENTIAL}')",
            name="classification_valid",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ProjectStatus.ACTIVE.value,
        server_default=text(f"'{ProjectStatus.ACTIVE}'"),
    )
    classification: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ProjectClassification.STANDARD.value,
        server_default=text(f"'{ProjectClassification.STANDARD}'"),
    )
