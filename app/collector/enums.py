"""采集任务与来源生命周期枚举。"""

from __future__ import annotations

from enum import StrEnum


class CollectionTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY = "retry"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class CollectionTaskType(StrEnum):
    FETCH_URL = "fetch_url"


class SourceStatus(StrEnum):
    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"
    DELETE_FAILED = "delete_failed"


class SourceSnapshotStatus(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"
