"""Project collaboration enums."""

from __future__ import annotations

from enum import StrEnum


class CommentStatus(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELED = "canceled"


class ApprovalResourceType(StrEnum):
    REPORT_VERSION = "report_version"


__all__ = ["ApprovalResourceType", "ApprovalStatus", "CommentStatus"]
