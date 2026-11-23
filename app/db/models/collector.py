"""External sources, snapshots, collection tasks, and domain rate limits."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.collector.enums import (
    CollectionTaskStatus,
    CollectionTaskType,
    SourceSnapshotStatus,
    SourceStatus,
)
from app.db.base import Base
from app.db.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

_TASK_STATUSES = ", ".join(f"'{status}'" for status in CollectionTaskStatus)
_SOURCE_STATUSES = ", ".join(f"'{status}'" for status in SourceStatus)
_SNAPSHOT_STATUSES = ", ".join(f"'{status}'" for status in SourceSnapshotStatus)


class WebSource(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "web_sources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_web_sources_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "url_hash",
            name="uq_web_sources_tenant_workspace_url",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_web_sources_tenant_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"status IN ({_SOURCE_STATUSES})", name="status_valid"),
        Index("ix_web_sources_tenant_workspace", "tenant_id", "workspace_id"),
        Index("ix_web_sources_tenant_domain", "tenant_id", "domain"),
        Index("ix_web_sources_tenant_content", "tenant_id", "content_sha256"),
    )

    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=SourceStatus.ACTIVE.value,
        server_default=text(f"'{SourceStatus.ACTIVE}'"),
    )
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latest_snapshot_id: Mapped[UUID | None] = mapped_column(nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(nullable=True)


class SourceSnapshot(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "source_snapshots"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_source_snapshots_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "source_id",
            "content_sha256",
            name="uq_source_snapshots_tenant_source_content",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_source_snapshots_tenant_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_id"],
            ["web_sources.tenant_id", "web_sources.id"],
            name="fk_source_snapshots_tenant_source",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"status IN ({_SNAPSHOT_STATUSES})", name="status_valid"),
        CheckConstraint("size_bytes >= 0", name="size_bytes_non_negative"),
        Index(
            "ix_source_snapshots_tenant_source",
            "tenant_id",
            "source_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    source_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=SourceSnapshotStatus.ACTIVE.value,
        server_default=text(f"'{SourceSnapshotStatus.ACTIVE}'"),
    )
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    final_url: Mapped[str] = mapped_column(Text, nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_char_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class CollectionTask(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "collection_tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_collection_tasks_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "active_key",
            name="uq_collection_tasks_tenant_active_key",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_collection_tasks_tenant_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_collection_tasks_tenant_project",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"status IN ({_TASK_STATUSES})", name="status_valid"),
        CheckConstraint(
            f"task_type IN ('{CollectionTaskType.FETCH_URL}')",
            name="task_type_valid",
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        Index(
            "ix_collection_tasks_tenant_status_next",
            "tenant_id",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_collection_tasks_tenant_workspace",
            "tenant_id",
            "workspace_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(nullable=True)
    requested_by: Mapped[UUID | None] = mapped_column(nullable=True)
    task_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=CollectionTaskType.FETCH_URL.value,
        server_default=text(f"'{CollectionTaskType.FETCH_URL}'"),
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=CollectionTaskStatus.PENDING.value,
        server_default=text(f"'{CollectionTaskStatus.PENDING}'"),
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_id: Mapped[UUID | None] = mapped_column(nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)


class CollectorDomainLimit(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "collector_domain_limits"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "domain",
            name="uq_collector_domain_limits_tenant_domain",
        ),
    )

    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    next_allowed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
