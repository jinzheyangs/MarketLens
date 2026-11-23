"""Root tenant registry.

Organizations, workspaces, projects, memberships, and roles are intentionally
deferred to stage 2. This table anchors future ``tenant_id`` foreign keys.
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
