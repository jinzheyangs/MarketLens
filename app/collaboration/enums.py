"""项目协作（评论、审批）相关枚举。"""

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
