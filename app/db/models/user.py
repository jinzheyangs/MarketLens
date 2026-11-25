"""租户范围的用户目录，数据来源于受信任的身份提供商。

用户由租户管理员或引导 CLI 显式开通；OIDC 登录绝不会自动创建账号。
系统不存储任何密码。
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.enums import TenantRole, UserStatus
from app.db.base import Base
from app.db.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    """系统用户：归属某一租户，关联 OIDC 的 issuer 与 subject。"""

    __tablename__ = "users"
    __table_args__ = (
        # 同一租户内主键唯一；同一身份提供商主体与邮箱在租户内唯一
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

    issuer: Mapped[str] = mapped_column(String(512), nullable=False)  # 身份提供商标识
    subject: Mapped[str] = mapped_column(String(255), nullable=False)  # IdP 内用户主体 ID
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 租户级角色：租户管理员 / 普通成员
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
