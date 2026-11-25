"""证据审查、主张、引用、报告、版本与导出任务。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
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

from app.agent.enums import SPECIALIST_ROLES, FindingSourceKind
from app.db.base import Base
from app.db.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.reporting.enums import (
    CitationRelation,
    ClaimStatus,
    ExportFormat,
    ExportStatus,
    ReportStatus,
    ReportVersionStatus,
    ReviewStatus,
)

_AGENT_ROLES = ", ".join(f"'{role}'" for role in SPECIALIST_ROLES)
_SOURCE_KINDS = ", ".join(f"'{kind}'" for kind in FindingSourceKind)
_REVIEW_STATUSES = ", ".join(f"'{status}'" for status in ReviewStatus)
_CLAIM_STATUSES = ", ".join(f"'{status}'" for status in ClaimStatus)
_RELATIONS = ", ".join(f"'{relation}'" for relation in CitationRelation)
_REPORT_STATUSES = ", ".join(f"'{status}'" for status in ReportStatus)
_VERSION_STATUSES = ", ".join(f"'{status}'" for status in ReportVersionStatus)
_EXPORT_FORMATS = ", ".join(f"'{value}'" for value in ExportFormat)
_EXPORT_STATUSES = ", ".join(f"'{status}'" for status in ExportStatus)


class ResearchSource(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "research_sources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_research_sources_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "root_run_id",
            "source_key",
            name="uq_research_sources_tenant_root_key",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "root_run_id"],
            ["research_runs.tenant_id", "research_runs.id"],
            name="fk_research_sources_tenant_root",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "origin_run_id"],
            ["research_runs.tenant_id", "research_runs.id"],
            name="fk_research_sources_tenant_origin",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"source_kind IN ({_SOURCE_KINDS})", name="source_kind_valid"),
        Index("ix_research_sources_tenant_root", "tenant_id", "root_run_id"),
        Index(
            "ix_research_sources_tenant_independence",
            "tenant_id",
            "root_run_id",
            "independence_key",
        ),
        Index(
            "ix_research_sources_tenant_external",
            "tenant_id",
            "external_source_id",
        ),
    )

    root_run_id: Mapped[UUID] = mapped_column(nullable=False)
    origin_run_id: Mapped[UUID] = mapped_column(nullable=False)
    source_key: Mapped[str] = mapped_column(String(128), nullable=False)
    independence_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    external_source_id: Mapped[UUID] = mapped_column(nullable=False)
    document_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    source_snapshot_id: Mapped[UUID | None] = mapped_column(nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ResearchEvidence(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "research_evidence"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_research_evidence_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "root_run_id",
            "evidence_key",
            name="uq_research_evidence_tenant_root_key",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "root_run_id"],
            ["research_runs.tenant_id", "research_runs.id"],
            name="fk_research_evidence_tenant_root",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_id"],
            ["research_sources.tenant_id", "research_sources.id"],
            name="fk_research_evidence_tenant_source",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "origin_run_id"],
            ["research_runs.tenant_id", "research_runs.id"],
            name="fk_research_evidence_tenant_origin",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_research_evidence_tenant_root",
            "tenant_id",
            "root_run_id",
        ),
        Index(
            "ix_research_evidence_tenant_source",
            "tenant_id",
            "source_id",
        ),
    )

    root_run_id: Mapped[UUID] = mapped_column(nullable=False)
    source_id: Mapped[UUID] = mapped_column(nullable=False)
    origin_run_id: Mapped[UUID] = mapped_column(nullable=False)
    origin_finding_id: Mapped[UUID | None] = mapped_column(nullable=True)
    evidence_key: Mapped[str] = mapped_column(String(128), nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    injection_suspected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    chunk_id: Mapped[UUID | None] = mapped_column(nullable=True)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paragraph_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ResearchClaim(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "research_claims"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_research_claims_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "root_run_id",
            "claim_key",
            name="uq_research_claims_tenant_root_key",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "root_run_id"],
            ["research_runs.tenant_id", "research_runs.id"],
            name="fk_research_claims_tenant_root",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "origin_run_id"],
            ["research_runs.tenant_id", "research_runs.id"],
            name="fk_research_claims_tenant_origin",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"agent_role IN ({_AGENT_ROLES})", name="agent_role_valid"),
        CheckConstraint(f"status IN ({_CLAIM_STATUSES})", name="status_valid"),
        CheckConstraint("citation_count >= 0", name="citation_count_non_negative"),
        CheckConstraint(
            "supported_citation_count >= 0",
            name="supported_citation_count_non_negative",
        ),
        CheckConstraint(
            "independent_source_count >= 0",
            name="independent_source_count_non_negative",
        ),
        CheckConstraint("stale_evidence_count >= 0", name="stale_evidence_count_non_negative"),
        Index(
            "ix_research_claims_tenant_root_status",
            "tenant_id",
            "root_run_id",
            "status",
        ),
    )

    root_run_id: Mapped[UUID] = mapped_column(nullable=False)
    origin_run_id: Mapped[UUID] = mapped_column(nullable=False)
    origin_finding_id: Mapped[UUID | None] = mapped_column(nullable=True)
    claim_key: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_role: Mapped[str] = mapped_column(String(32), nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(String(500), nullable=False)
    limitations: Mapped[str] = mapped_column(String(500), nullable=False)
    original_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ClaimStatus.CANDIDATE.value,
        server_default=text(f"'{ClaimStatus.CANDIDATE}'"),
    )
    citation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    supported_citation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    independent_source_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    stale_evidence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    conflict_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    review_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResearchCitation(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "research_citations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_research_citations_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "claim_id",
            "evidence_id",
            name="uq_research_citations_tenant_claim_evidence",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "claim_id"],
            ["research_claims.tenant_id", "research_claims.id"],
            name="fk_research_citations_tenant_claim",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "evidence_id"],
            ["research_evidence.tenant_id", "research_evidence.id"],
            name="fk_research_citations_tenant_evidence",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "origin_run_id"],
            ["research_runs.tenant_id", "research_runs.id"],
            name="fk_research_citations_tenant_origin",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"relation IN ({_RELATIONS})", name="relation_valid"),
        CheckConstraint("support_score >= 0 AND support_score <= 1", name="support_score_range"),
        Index(
            "ix_research_citations_tenant_claim",
            "tenant_id",
            "claim_id",
        ),
        Index(
            "ix_research_citations_tenant_evidence",
            "tenant_id",
            "evidence_id",
        ),
    )

    claim_id: Mapped[UUID] = mapped_column(nullable=False)
    evidence_id: Mapped[UUID] = mapped_column(nullable=False)
    origin_run_id: Mapped[UUID] = mapped_column(nullable=False)
    origin_finding_id: Mapped[UUID | None] = mapped_column(nullable=True)
    relation: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=CitationRelation.SUPPORTS.value,
        server_default=text(f"'{CitationRelation.SUPPORTS}'"),
    )
    support_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    review_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ResearchReview(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "research_reviews"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_research_reviews_tenant_id_id"),
        UniqueConstraint("tenant_id", "root_run_id", name="uq_research_reviews_tenant_root"),
        ForeignKeyConstraint(
            ["tenant_id", "root_run_id"],
            ["research_runs.tenant_id", "research_runs.id"],
            name="fk_research_reviews_tenant_root",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_research_reviews_tenant_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_research_reviews_tenant_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "followup_run_id"],
            ["research_runs.tenant_id", "research_runs.id"],
            name="fk_research_reviews_tenant_followup",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"status IN ({_REVIEW_STATUSES})", name="status_valid"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("followup_round >= 0", name="followup_round_non_negative"),
        CheckConstraint("review_round >= 1", name="review_round_positive"),
        CheckConstraint("claim_count >= 0", name="claim_count_non_negative"),
        CheckConstraint("supported_count >= 0", name="supported_count_non_negative"),
        CheckConstraint("partial_count >= 0", name="partial_count_non_negative"),
        CheckConstraint("insufficient_count >= 0", name="insufficient_count_non_negative"),
        CheckConstraint("conflicted_count >= 0", name="conflicted_count_non_negative"),
        CheckConstraint("invalid_count >= 0", name="invalid_count_non_negative"),
        CheckConstraint("source_count >= 0", name="source_count_non_negative"),
        CheckConstraint("evidence_count >= 0", name="evidence_count_non_negative"),
        Index("ix_research_reviews_tenant_status_next", "tenant_id", "status", "next_attempt_at"),
    )

    root_run_id: Mapped[UUID] = mapped_column(nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ReviewStatus.PENDING.value,
        server_default=text(f"'{ReviewStatus.PENDING}'"),
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
    followup_round: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    followup_run_id: Mapped[UUID | None] = mapped_column(nullable=True)
    followup_questions: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    review_round: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    rules_version: Mapped[str] = mapped_column(String(32), nullable=False)
    claim_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    supported_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    partial_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    insufficient_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    conflicted_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    invalid_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    source_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    evidence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Report(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_reports_tenant_id_id"),
        UniqueConstraint("tenant_id", "root_run_id", name="uq_reports_tenant_root"),
        ForeignKeyConstraint(
            ["tenant_id", "root_run_id"],
            ["research_runs.tenant_id", "research_runs.id"],
            name="fk_reports_tenant_root",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_reports_tenant_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_reports_tenant_project",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"status IN ({_REPORT_STATUSES})", name="status_valid"),
        Index("ix_reports_tenant_workspace", "tenant_id", "workspace_id"),
    )

    root_run_id: Mapped[UUID] = mapped_column(nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ReportStatus.DRAFT.value,
        server_default=text(f"'{ReportStatus.DRAFT}'"),
    )
    current_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(nullable=True)


class ReportVersion(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "report_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_report_versions_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "report_id",
            "version_number",
            name="uq_report_versions_tenant_report_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "report_id"],
            ["reports.tenant_id", "reports.id"],
            name="fk_report_versions_tenant_report",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "review_id"],
            ["research_reviews.tenant_id", "research_reviews.id"],
            name="fk_report_versions_tenant_review",
            ondelete="SET NULL",
        ),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint(f"status IN ({_VERSION_STATUSES})", name="status_valid"),
        Index(
            "ix_report_versions_tenant_report",
            "tenant_id",
            "report_id",
            "version_number",
        ),
    )

    report_id: Mapped[UUID] = mapped_column(nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ReportVersionStatus.DRAFT.value,
        server_default=text(f"'{ReportVersionStatus.DRAFT}'"),
    )
    review_id: Mapped[UUID | None] = mapped_column(nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    structured: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_list: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    citation_index: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(nullable=True)
    approved_by: Mapped[UUID | None] = mapped_column(nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReportExportJob(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "report_export_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "report_id"],
            ["reports.tenant_id", "reports.id"],
            name="fk_report_export_jobs_tenant_report",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["report_versions.tenant_id", "report_versions.id"],
            name="fk_report_export_jobs_tenant_version",
            ondelete="CASCADE",
        ),
        CheckConstraint(f"format IN ({_EXPORT_FORMATS})", name="format_valid"),
        CheckConstraint(f"status IN ({_EXPORT_STATUSES})", name="status_valid"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("size_bytes >= 0", name="size_bytes_non_negative"),
        Index(
            "ix_report_export_jobs_tenant_status_next",
            "tenant_id",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_report_export_jobs_tenant_version",
            "tenant_id",
            "version_id",
        ),
    )

    report_id: Mapped[UUID] = mapped_column(nullable=False)
    version_id: Mapped[UUID] = mapped_column(nullable=False)
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ExportStatus.PENDING.value,
        server_default=text(f"'{ExportStatus.PENDING}'"),
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
    object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    requested_by: Mapped[UUID | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
