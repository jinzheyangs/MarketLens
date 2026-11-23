"""Audit API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.auth.enums import AuditOutcome, ResourceType


class AuditEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    actor_user_id: UUID | None
    action: str
    resource_type: ResourceType
    resource_id: UUID | None
    outcome: AuditOutcome
    request_id: str | None
    details: dict[str, Any]
    created_at: datetime
