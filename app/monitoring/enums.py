"""Monitoring, change, notification, and quota enums."""

from __future__ import annotations

from enum import StrEnum


class MonitorKind(StrEnum):
    KEYWORD = "keyword"
    COMPETITOR = "competitor"


class MonitorStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DELETED = "deleted"


class MonitorRunStatus(StrEnum):
    PENDING = "pending"
    COLLECTING = "collecting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class ChangeType(StrEnum):
    NEW_SOURCE = "new_source"
    CONTENT_CHANGED = "content_changed"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_SEVERITY_RANK = {
    Severity.LOW.value: 0,
    Severity.MEDIUM.value: 1,
    Severity.HIGH.value: 2,
    Severity.CRITICAL.value: 3,
}


def severity_rank(value: str) -> int:
    return _SEVERITY_RANK.get(value, 0)


class NotificationChannelKind(StrEnum):
    WEBHOOK = "webhook"
    SLACK = "slack"
    LOG = "log"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SENT = "sent"
    FAILED = "failed"


__all__ = [
    "ChangeType",
    "DeliveryStatus",
    "MonitorKind",
    "MonitorRunStatus",
    "MonitorStatus",
    "NotificationChannelKind",
    "Severity",
    "severity_rank",
]
