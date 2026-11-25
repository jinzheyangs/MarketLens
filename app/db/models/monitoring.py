"""监测订阅、变更记录、通知渠道与投递记录。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.monitoring.enums import (
    ChangeType,
    DeliveryStatus,
    MonitorKind,
    MonitorRunStatus,
    MonitorStatus,
    NotificationChannelKind,
    Severity,
)

_MONITOR_STATUSES = ", ".join(f"'{status}'" for status in MonitorStatus)
_MONITOR_KINDS = ", ".join(f"'{kind}'" for kind in MonitorKind)
_RUN_STATUSES = ", ".join(f"'{status}'" for status in MonitorRunStatus)
_CHANGE_TYPES = ", ".join(f"'{value}'" for value in ChangeType)
_SEVERITIES = ", ".join(f"'{value}'" for value in Severity)
_CHANNEL_KINDS = ", ".join(f"'{value}'" for value in NotificationChannelKind)
_DELIVERY_STATUSES = ", ".join(f"'{status}'" for status in DeliveryStatus)


class MonitorSubscription(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "monitor_subscriptions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_monitor_subscriptions_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "name",
            name="uq_monitor_subscriptions_tenant_workspace_name",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_monitor_subscriptions_tenant_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_monitor_subscriptions_tenant_project",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"kind IN ({_MONITOR_KINDS})", name="kind_valid"),
        CheckConstraint(f"status IN ({_MONITOR_STATUSES})", name="status_valid"),
        CheckConstraint("cadence_minutes >= 1", name="cadence_positive"),
        Index(
            "ix_monitor_subscriptions_tenant_status_next",
            "tenant_id",
            "status",
            "next_run_at",
        ),
        Index(
            "ix_monitor_subscriptions_tenant_workspace",
            "tenant_id",
            "workspace_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(nullable=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    query: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=MonitorStatus.ACTIVE.value,
        server_default=text(f"'{MonitorStatus.ACTIVE}'"),
    )
    cadence_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(nullable=True)


class NotificationChannel(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "notification_channels"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_notification_channels_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "name",
            name="uq_notification_channels_tenant_workspace_name",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_notification_channels_tenant_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"kind IN ({_CHANNEL_KINDS})", name="kind_valid"),
        Index(
            "ix_notification_channels_tenant_workspace",
            "tenant_id",
            "workspace_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    encrypted_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_by: Mapped[UUID | None] = mapped_column(nullable=True)


class MonitorAlertRule(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "monitor_alert_rules"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "subscription_id",
            "channel_id",
            name="uq_monitor_alert_rules_tenant_subscription_channel",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "subscription_id"],
            ["monitor_subscriptions.tenant_id", "monitor_subscriptions.id"],
            name="fk_monitor_alert_rules_tenant_subscription",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "channel_id"],
            ["notification_channels.tenant_id", "notification_channels.id"],
            name="fk_monitor_alert_rules_tenant_channel",
            ondelete="CASCADE",
        ),
        CheckConstraint(f"min_severity IN ({_SEVERITIES})", name="severity_valid"),
        Index(
            "ix_monitor_alert_rules_tenant_subscription",
            "tenant_id",
            "subscription_id",
        ),
    )

    subscription_id: Mapped[UUID] = mapped_column(nullable=False)
    channel_id: Mapped[UUID] = mapped_column(nullable=False)
    min_severity: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=Severity.MEDIUM.value,
        server_default=text(f"'{Severity.MEDIUM}'"),
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class MonitorRun(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "monitor_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_monitor_runs_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "subscription_id"],
            ["monitor_subscriptions.tenant_id", "monitor_subscriptions.id"],
            name="fk_monitor_runs_tenant_subscription",
            ondelete="CASCADE",
        ),
        CheckConstraint(f"status IN ({_RUN_STATUSES})", name="status_valid"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("change_count >= 0", name="change_count_non_negative"),
        Index("ix_monitor_runs_tenant_status_next", "tenant_id", "status", "next_attempt_at"),
        Index("ix_monitor_runs_tenant_subscription", "tenant_id", "subscription_id"),
    )

    subscription_id: Mapped[UUID] = mapped_column(nullable=False)
    triggered_by: Mapped[str] = mapped_column(
        String(16), nullable=False, default="schedule", server_default=text("'schedule'")
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=MonitorRunStatus.PENDING.value,
        server_default=text(f"'{MonitorRunStatus.PENDING}'"),
    )
    task_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    change_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MonitorSeenSource(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "monitor_seen_sources"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "subscription_id",
            "source_id",
            name="uq_monitor_seen_sources_tenant_subscription_source",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "subscription_id"],
            ["monitor_subscriptions.tenant_id", "monitor_subscriptions.id"],
            name="fk_monitor_seen_sources_tenant_subscription",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_id"],
            ["web_sources.tenant_id", "web_sources.id"],
            name="fk_monitor_seen_sources_tenant_source",
            ondelete="CASCADE",
        ),
        Index(
            "ix_monitor_seen_sources_tenant_subscription",
            "tenant_id",
            "subscription_id",
        ),
    )

    subscription_id: Mapped[UUID] = mapped_column(nullable=False)
    source_id: Mapped[UUID] = mapped_column(nullable=False)
    last_snapshot_id: Mapped[UUID] = mapped_column(nullable=False)
    last_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MonitorChange(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "monitor_changes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_monitor_changes_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "source_id",
            "change_type",
            name="uq_monitor_changes_tenant_run_source_type",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "subscription_id"],
            ["monitor_subscriptions.tenant_id", "monitor_subscriptions.id"],
            name="fk_monitor_changes_tenant_subscription",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["monitor_runs.tenant_id", "monitor_runs.id"],
            name="fk_monitor_changes_tenant_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_id"],
            ["web_sources.tenant_id", "web_sources.id"],
            name="fk_monitor_changes_tenant_source",
            ondelete="CASCADE",
        ),
        CheckConstraint(f"change_type IN ({_CHANGE_TYPES})", name="change_type_valid"),
        CheckConstraint(f"severity IN ({_SEVERITIES})", name="severity_valid"),
        Index(
            "ix_monitor_changes_tenant_subscription_detected",
            "tenant_id",
            "subscription_id",
            "detected_at",
        ),
    )

    subscription_id: Mapped[UUID] = mapped_column(nullable=False)
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    source_id: Mapped[UUID] = mapped_column(nullable=False)
    previous_snapshot_id: Mapped[UUID | None] = mapped_column(nullable=True)
    current_snapshot_id: Mapped[UUID] = mapped_column(nullable=False)
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(String(1_000), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NotificationDelivery(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_notification_deliveries_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "delivery_key",
            name="uq_notification_deliveries_tenant_delivery_key",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "channel_id"],
            ["notification_channels.tenant_id", "notification_channels.id"],
            name="fk_notification_deliveries_tenant_channel",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "subscription_id"],
            ["monitor_subscriptions.tenant_id", "monitor_subscriptions.id"],
            name="fk_notification_deliveries_tenant_subscription",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "change_id"],
            ["monitor_changes.tenant_id", "monitor_changes.id"],
            name="fk_notification_deliveries_tenant_change",
            ondelete="CASCADE",
        ),
        CheckConstraint(f"status IN ({_DELIVERY_STATUSES})", name="status_valid"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        Index(
            "ix_notification_deliveries_tenant_status_next",
            "tenant_id",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_notification_deliveries_tenant_channel",
            "tenant_id",
            "channel_id",
        ),
    )

    channel_id: Mapped[UUID] = mapped_column(nullable=False)
    subscription_id: Mapped[UUID] = mapped_column(nullable=False)
    change_id: Mapped[UUID] = mapped_column(nullable=False)
    delivery_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=DeliveryStatus.PENDING.value,
        server_default=text(f"'{DeliveryStatus.PENDING}'"),
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
