"""只追加（append-only）的租户范围审计事件。

审计记录刻意不通过外键引用用户，以便在后续用户生命周期变更后历史仍
可保留。记录中绝不包含凭证、令牌、请求体或上传内容。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.enums import AuditOutcome
from app.db.base import Base
from app.db.mixins import TenantScopedMixin, UUIDPrimaryKeyMixin


class AuditEvent(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """一条不可变审计日志：谁、在何时、对什么资源、做了什么、结果如何。"""

    __tablename__ = "audit_events"
    __table_args__ = (
        # 按租户 + 时间检索审计记录的复合索引
        Index("ix_audit_events_tenant_created_at", "tenant_id", "created_at"),
        CheckConstraint(
            f"outcome IN ('{AuditOutcome.SUCCESS}', '{AuditOutcome.DENIED}')",
            name="outcome_valid",
        ),
    )

    actor_user_id: Mapped[UUID | None] = mapped_column(nullable=True)  # 操作人（可空，如系统动作）
    action: Mapped[str] = mapped_column(String(100), nullable=False)  # 审计动作码
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)  # 资源类型
    resource_id: Mapped[UUID | None] = mapped_column(nullable=True)  # 资源 ID
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)  # 成功 / 拒绝
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 关联请求 ID
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
