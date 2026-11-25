"""文档入库生命周期枚举。"""

from __future__ import annotations

from enum import StrEnum


class DocumentStatus(StrEnum):
    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"
    DELETE_FAILED = "delete_failed"


class DocumentVersionStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    DELETING = "deleting"
    DELETED = "deleted"
    DELETE_FAILED = "delete_failed"


class DocumentSourceType(StrEnum):
    UPLOAD = "upload"
