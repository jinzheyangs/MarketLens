"""根租户注册表。

组织、工作区、项目、成员关系与角色在后续阶段引入。本表用于锚定后续
外键引用的 ``tenant_id``。
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'suspended')",
            name="status_valid",
        ),
        {
            "comment": (
                "Root tenant registry. Organizations, workspaces, projects, and "
                "memberships arrive in stage 2."
            )
        },
    )

    slug: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        comment="Stable human-readable key. Never used as an authorization decision.",
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        server_default=text("'active'"),
    )
