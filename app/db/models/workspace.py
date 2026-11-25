"""工作区：归属于某一组织，且始终处于同一租户之内。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.enums import WorkspaceStatus
from app.db.base import Base
from app.db.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Workspace(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_workspaces_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "organization_id",
            "slug",
            name="uq_workspaces_tenant_org_slug",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "organization_id"],
            ["organizations.tenant_id", "organizations.id"],
            name="fk_workspaces_tenant_organization",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"status IN ('{WorkspaceStatus.ACTIVE}', '{WorkspaceStatus.ARCHIVED}')",
            name="status_valid",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=WorkspaceStatus.ACTIVE.value,
        server_default=text(f"'{WorkspaceStatus.ACTIVE}'"),
    )
