"""Event outbox and consumer state enums."""

from __future__ import annotations

from enum import StrEnum

EVENT_SCHEMA_VERSION = 1


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


class ConsumerFailureStatus(StrEnum):
    OPEN = "open"
    DEAD_LETTER = "dead_letter"
    RESOLVED = "resolved"


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "ConsumerFailureStatus",
    "OutboxStatus",
]
