"""各层级（组织/工作区/项目）的角色分配。

角色按层级显式分配，并在授权时向下继承：组织角色覆盖其下工作区/项目，
工作区角色覆盖其下项目；不存在向上继承。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.enums import MembershipStatus, ScopeRole
from app.db.base import Base
from app.db.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

_ROLE_CHECK = f"role IN ('{ScopeRole.ADMIN}', '{ScopeRole.EDITOR}', '{ScopeRole.VIEWER}')"
_STATUS_CHECK = f"status IN ('{MembershipStatus.ACTIVE}', '{MembershipStatus.REVOKED}')"


class OrganizationMembership(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "organization_memberships"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "organization_id",
            "user_id",
            name="uq_organization_memberships_tenant_org_user",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "organization_id"],
            ["organizations.tenant_id", "organizations.id"],
            name="fk_organization_memberships_tenant_organization",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_organization_memberships_tenant_user",
            ondelete="RESTRICT",
        ),
        CheckConstraint(_ROLE_CHECK, name="role_valid"),
        CheckConstraint(_STATUS_CHECK, name="status_valid"),
    )

    organization_id: Mapped[UUID] = mapped_column(nullable=False)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=MembershipStatus.ACTIVE.value,
        server_default=text(f"'{MembershipStatus.ACTIVE}'"),
    )


class WorkspaceMembership(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "workspace_memberships"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "user_id",
            name="uq_workspace_memberships_tenant_workspace_user",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_workspace_memberships_tenant_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_workspace_memberships_tenant_user",
            ondelete="RESTRICT",
        ),
        CheckConstraint(_ROLE_CHECK, name="role_valid"),
        CheckConstraint(_STATUS_CHECK, name="status_valid"),
    )

    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=MembershipStatus.ACTIVE.value,
        server_default=text(f"'{MembershipStatus.ACTIVE}'"),
    )


class ProjectMembership(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "project_memberships"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "user_id",
            name="uq_project_memberships_tenant_project_user",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_project_memberships_tenant_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_project_memberships_tenant_user",
            ondelete="RESTRICT",
        ),
        CheckConstraint(_ROLE_CHECK, name="role_valid"),
        CheckConstraint(_STATUS_CHECK, name="status_valid"),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=MembershipStatus.ACTIVE.value,
        server_default=text(f"'{MembershipStatus.ACTIVE}'"),
    )
