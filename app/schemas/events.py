"""事件 outbox 与消费者死信接口 Schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.events.enums import ConsumerFailureStatus, OutboxStatus


class DomainEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_id: UUID
    event_type: str
    event_version: int
    aggregate_type: str
    aggregate_id: UUID | None
    actor_user_id: UUID | None
    occurred_at: datetime
    status: OutboxStatus
    attempts: int
    max_attempts: int
    available_at: datetime
    last_error: str | None
    published_at: datetime | None
    created_at: datetime


class EventConsumerFailureRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    consumer: str
    event_id: UUID
    event_type: str
    status: ConsumerFailureStatus
    attempts: int
    max_attempts: int
    last_error: str | None
    first_failed_at: datetime
    last_failed_at: datetime
    resolved_at: datetime | None
    payload: dict[str, Any]
