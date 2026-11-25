"""证据审查、报告、版本与导出任务枚举。"""

from __future__ import annotations

from enum import StrEnum

REPORT_RULES_VERSION = "stage6-v1"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_FOLLOWUP = "awaiting_followup"
    COMPLETED = "completed"
    FAILED = "failed"


class ClaimStatus(StrEnum):
    CANDIDATE = "candidate"
    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    INSUFFICIENT = "insufficient"
    CONFLICTED = "conflicted"
    INVALID = "invalid"


class CitationRelation(StrEnum):
    SUPPORTS = "supports"
    INSUFFICIENT = "insufficient"
    UNAVAILABLE = "unavailable"


class ReportStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReportVersionStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class ExportFormat(StrEnum):
    MARKDOWN = "markdown"
    DOCX = "docx"


class ExportStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


__all__ = [
    "REPORT_RULES_VERSION",
    "CitationRelation",
    "ClaimStatus",
    "ExportFormat",
    "ExportStatus",
    "ReportStatus",
    "ReportVersionStatus",
    "ReviewStatus",
]
