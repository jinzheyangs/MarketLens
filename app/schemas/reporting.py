"""Evidence review, report, version, approval, and export API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.agent.enums import AgentRole, FindingSourceKind
from app.reporting.enums import (
    CitationRelation,
    ClaimStatus,
    ExportFormat,
    ExportStatus,
    ReportStatus,
    ReportVersionStatus,
    ReviewStatus,
)

RejectionReason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class ResearchReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    root_run_id: UUID
    workspace_id: UUID
    project_id: UUID | None
    status: ReviewStatus
    attempts: int
    max_attempts: int
    review_round: int
    followup_round: int
    followup_run_id: UUID | None
    followup_questions: list[str]
    rules_version: str
    claim_count: int
    supported_count: int
    partial_count: int
    insufficient_count: int
    conflicted_count: int
    invalid_count: int
    source_count: int
    evidence_count: int
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ReviewSourceView(BaseModel):
    id: UUID
    source_kind: FindingSourceKind
    title: str
    url: str | None
    domain: str | None
    observed_at: datetime
    untrusted: bool = True


class ReviewEvidenceView(BaseModel):
    id: UUID
    quote: str
    observed_at: datetime
    injection_suspected: bool
    page_number: int | None
    paragraph_number: int | None
    untrusted: bool = True


class ReviewCitationRead(BaseModel):
    relation: CitationRelation
    support_score: float
    review_reason: str | None
    evidence: ReviewEvidenceView
    source: ReviewSourceView


class ReviewClaimRead(BaseModel):
    id: UUID
    claim: str
    agent_role: AgentRole
    status: ClaimStatus
    scope: str
    limitations: str
    original_confidence: float
    citation_count: int
    supported_citation_count: int
    independent_source_count: int
    stale_evidence_count: int
    review_summary: str | None
    conflict_reason: str | None
    reviewed_at: datetime | None
    citations: list[ReviewCitationRead]
    untrusted_evidence: bool = True


class ReportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    root_run_id: UUID
    workspace_id: UUID
    project_id: UUID | None
    status: ReportStatus
    current_version_id: UUID | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime


class ReportVersionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    report_id: UUID
    version_number: int
    status: ReportVersionStatus
    title: str
    checksum_sha256: str
    created_by: UUID | None
    approved_by: UUID | None
    approved_at: datetime | None
    rejection_reason: str | None
    created_at: datetime


class ReportVersionRead(ReportVersionSummary):
    review_id: UUID | None
    content_markdown: str
    structured: dict[str, Any]
    source_list: list[dict[str, Any]]
    citation_index: list[dict[str, Any]]
    untrusted_evidence: bool = True


class RejectReportVersionRequest(BaseModel):
    reason: RejectionReason


class ReportExportCreate(BaseModel):
    format: ExportFormat


class ReportExportJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    report_id: UUID
    version_id: UUID
    format: ExportFormat
    status: ExportStatus
    attempts: int
    max_attempts: int
    content_type: str | None
    size_bytes: int
    error_code: str | None
    error_message: str | None
    requested_by: UUID | None
    created_at: datetime
    completed_at: datetime | None
