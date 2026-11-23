"""Tenant-scoped user directory populated from a trusted identity provider.

Users are provisioned explicitly by a tenant administrator or the bootstrap CLI;
OIDC login never auto-provisions accounts. No passwords are stored.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.enums import TenantRole, UserStatus
from app.db.base import Base
from app.db.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_users_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "issuer",
            "subject",
            name="uq_users_tenant_issuer_subject",
        ),
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
        CheckConstraint(
            f"tenant_role IN ('{TenantRole.TENANT_ADMIN}', '{TenantRole.MEMBER}')",
            name="tenant_role_valid",
        ),
        CheckConstraint(
            f"status IN ('{UserStatus.ACTIVE}', '{UserStatus.DISABLED}')",
            name="status_valid",
        ),
    )

    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    tenant_role: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=TenantRole.MEMBER.value,
        server_default=text(f"'{TenantRole.MEMBER}'"),
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=UserStatus.ACTIVE.value,
        server_default=text(f"'{UserStatus.ACTIVE}'"),
    )
