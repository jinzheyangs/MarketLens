"""Documents, immutable versions, and citation-bearing chunks.

Document access is derived from workspace/project scope and re-checked against
PostgreSQL for every retrieved candidate. Chunk IDs are deterministic so
re-indexing is idempotent in both PostgreSQL and Qdrant.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
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
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.rag.enums import (
    DocumentSourceType,
    DocumentStatus,
    DocumentVersionStatus,
)

_DOCUMENT_STATUSES = ", ".join(f"'{status}'" for status in DocumentStatus)
_VERSION_STATUSES = ", ".join(f"'{status}'" for status in DocumentVersionStatus)


class Document(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_documents_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_documents_tenant_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_documents_tenant_project",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"status IN ({_DOCUMENT_STATUSES})", name="status_valid"),
        CheckConstraint(
            f"source_type IN ('{DocumentSourceType.UPLOAD}')",
            name="source_type_valid",
        ),
        Index("ix_documents_tenant_workspace", "tenant_id", "workspace_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DocumentSourceType.UPLOAD.value,
        server_default=text(f"'{DocumentSourceType.UPLOAD}'"),
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DocumentStatus.ACTIVE.value,
        server_default=text(f"'{DocumentStatus.ACTIVE}'"),
    )
    active_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(nullable=True)


class DocumentVersion(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_document_versions_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "document_id",
            "version_number",
            name="uq_document_versions_tenant_document_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            name="fk_document_versions_tenant_document",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint("size_bytes >= 0", name="size_bytes_non_negative"),
        CheckConstraint(f"status IN ({_VERSION_STATUSES})", name="status_valid"),
        Index(
            "ix_document_versions_tenant_document",
            "tenant_id",
            "document_id",
        ),
    )

    document_id: Mapped[UUID] = mapped_column(nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DocumentVersionStatus.UPLOADED.value,
        server_default=text(f"'{DocumentVersionStatus.UPLOADED}'"),
    )
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentChunk(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_document_chunks_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "version_id",
            "chunk_index",
            name="uq_document_chunks_tenant_version_chunk",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            name="fk_document_chunks_tenant_document",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            name="fk_document_chunks_tenant_version",
            ondelete="CASCADE",
        ),
        CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
        CheckConstraint("start_offset >= 0", name="start_offset_non_negative"),
        CheckConstraint("end_offset >= start_offset", name="offset_order_valid"),
        CheckConstraint("char_count > 0", name="char_count_positive"),
        Index(
            "ix_document_chunks_tenant_document",
            "tenant_id",
            "document_id",
        ),
        Index(
            "ix_document_chunks_search_vector",
            "search_vector",
            postgresql_using="gin",
        ),
    )

    document_id: Mapped[UUID] = mapped_column(nullable=False)
    version_id: Mapped[UUID] = mapped_column(nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', content)", persisted=True),
        nullable=True,
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paragraph_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    embedding_version: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
