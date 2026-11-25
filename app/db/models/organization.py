"""组织：租户范围内最顶层的业务容器。"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.enums import OrganizationStatus
from app.db.base import Base
from app.db.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_organizations_tenant_id_id"),
        UniqueConstraint("tenant_id", "slug", name="uq_organizations_tenant_slug"),
        CheckConstraint(
            f"status IN ('{OrganizationStatus.ACTIVE}', '{OrganizationStatus.SUSPENDED}')",
            name="status_valid",
        ),
    )

    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=OrganizationStatus.ACTIVE.value,
        server_default=text(f"'{OrganizationStatus.ACTIVE}'"),
    )
