"""按租户配置的配额与并发限制。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, Integer, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class TenantQuota(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "tenant_quotas"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_tenant_quotas_tenant_id_id"),
        UniqueConstraint("tenant_id", name="uq_tenant_quotas_tenant"),
        CheckConstraint("max_active_research_runs >= 1", name="research_runs_positive"),
        CheckConstraint("max_pending_collection_tasks >= 1", name="collection_tasks_positive"),
        CheckConstraint("max_monitor_subscriptions >= 1", name="subscriptions_positive"),
        CheckConstraint("max_concurrent_monitor_runs >= 1", name="monitor_runs_positive"),
        CheckConstraint("max_notification_deliveries_per_day >= 1", name="notifications_positive"),
    )

    max_active_research_runs: Mapped[int] = mapped_column(Integer, nullable=False)
    max_pending_collection_tasks: Mapped[int] = mapped_column(Integer, nullable=False)
    max_monitor_subscriptions: Mapped[int] = mapped_column(Integer, nullable=False)
    max_concurrent_monitor_runs: Mapped[int] = mapped_column(Integer, nullable=False)
    max_notification_deliveries_per_day: Mapped[int] = mapped_column(Integer, nullable=False)


class TenantDomainPolicy(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "tenant_domain_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_tenant_domain_policies_tenant_id_id"),
        UniqueConstraint("tenant_id", name="uq_tenant_domain_policies_tenant"),
    )

    allowed_domains: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    blocked_domains: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    updated_by: Mapped[UUID | None] = mapped_column(nullable=True)
