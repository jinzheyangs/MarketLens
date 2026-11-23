"""Versioned domain-event envelope shared by outbox and consumers."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DomainEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_type: str = Field(min_length=1, max_length=100)
    event_version: int = Field(ge=1)
    tenant_id: UUID
    aggregate_type: str = Field(min_length=1, max_length=50)
    aggregate_id: UUID | None = None
    actor_user_id: UUID | None = None
    occurred_at: datetime
    partition_key: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any]
    headers: dict[str, Any] = Field(default_factory=dict)


__all__ = ["DomainEventEnvelope"]
