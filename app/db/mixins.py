"""可复用的 ORM 混入字段。

后续所有业务表都应继承 ``TenantScopedMixin``。``tenant_id`` 列非空并建索引；
ORM 写入另由 ``app.db.tenant_guard`` 守卫。完整的 SQL/RLS 强制由身份层补齐。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPrimaryKeyMixin:
    """自带 UUID 主键（默认生成 uuid4）。"""

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )


class TimestampMixin:
    """自带创建时间与最后更新时间，更新时自动刷新 ``updated_at``。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TenantScopedMixin:
    """租户范围混入：每张业务表都带归属租户 ID 并建立外键与索引。"""

    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="Owning tenant. Every read/write must be scoped to it.",
    )
