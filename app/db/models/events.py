"""Transactional outbox, consumer idempotency, and dead-letter records."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.events.enums import ConsumerFailureStatus, OutboxStatus

_OUTBOX_STATUSES = ", ".join(f"'{status}'" for status in OutboxStatus)
_FAILURE_STATUSES = ", ".join(f"'{status}'" for status in ConsumerFailureStatus)


class DomainEvent(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "domain_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_domain_events_tenant_id_id"),
        UniqueConstraint("tenant_id", "event_id", name="uq_domain_events_tenant_event"),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_domain_events_tenant_idempotency",
        ),
        CheckConstraint(f"status IN ({_OUTBOX_STATUSES})", name="status_valid"),
        CheckConstraint("event_version >= 1", name="event_version_positive"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        Index(
            "ix_domain_events_tenant_status_available",
            "tenant_id",
            "status",
            "available_at",
        ),
        Index("ix_domain_events_tenant_type", "tenant_id", "event_type"),
    )

    event_id: Mapped[UUID] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(50), nullable=False)
    aggregate_id: Mapped[UUID | None] = mapped_column(nullable=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    partition_key: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=OutboxStatus.PENDING.value,
        server_default=text(f"'{OutboxStatus.PENDING}'"),
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProcessedEvent(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "processed_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "consumer",
            "event_id",
            name="uq_processed_events_tenant_consumer_event",
        ),
        Index(
            "ix_processed_events_tenant_consumer",
            "tenant_id",
            "consumer",
            "processed_at",
        ),
    )

    consumer: Mapped[str] = mapped_column(String(100), nullable=False)
    event_id: Mapped[UUID] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EventConsumerFailure(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "event_consumer_failures"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "consumer",
            "event_id",
            name="uq_event_consumer_failures_tenant_consumer_event",
        ),
        CheckConstraint(f"status IN ({_FAILURE_STATUSES})", name="status_valid"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        Index(
            "ix_event_consumer_failures_tenant_status",
            "tenant_id",
            "status",
            "last_failed_at",
        ),
    )

    consumer: Mapped[str] = mapped_column(String(100), nullable=False)
    event_id: Mapped[UUID] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ConsumerFailureStatus.OPEN.value,
        server_default=text(f"'{ConsumerFailureStatus.OPEN}'"),
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    first_failed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_failed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
