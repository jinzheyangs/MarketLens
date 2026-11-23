"""Monitoring, channel, change, and notification API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from app.monitoring.enums import (
    ChangeType,
    DeliveryStatus,
    MonitorKind,
    MonitorRunStatus,
    MonitorStatus,
    NotificationChannelKind,
    Severity,
)

NameString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
QueryString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class MonitorAlertInput(BaseModel):
    channel_id: UUID
    min_severity: Severity = Severity.MEDIUM


class MonitorSubscriptionCreate(BaseModel):
    name: NameString
    kind: MonitorKind
    query: QueryString
    project_id: UUID | None = None
    cadence_minutes: int | None = Field(default=None, ge=1, le=525_600)
    alerts: list[MonitorAlertInput] = Field(default_factory=list, max_length=50)


class MonitorSubscriptionUpdate(BaseModel):
    query: QueryString | None = None
    cadence_minutes: int | None = Field(default=None, ge=1, le=525_600)
    alerts: list[MonitorAlertInput] | None = Field(default=None, max_length=50)


class MonitorSubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    project_id: UUID | None
    name: str
    kind: MonitorKind
    query: str
    status: MonitorStatus
    cadence_minutes: int
    next_run_at: datetime
    last_run_at: datetime | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime


class NotificationChannelCreate(BaseModel):
    name: NameString
    kind: NotificationChannelKind
    url: str | None = Field(default=None, max_length=2_048)
    signing_secret: SecretStr | None = None


class NotificationChannelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    name: str
    kind: NotificationChannelKind
    enabled: bool
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime


class MonitorRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    subscription_id: UUID
    triggered_by: str
    status: MonitorRunStatus
    attempts: int
    max_attempts: int
    change_count: int
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MonitorChangeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    subscription_id: UUID
    run_id: UUID
    source_id: UUID
    previous_snapshot_id: UUID | None
    current_snapshot_id: UUID
    change_type: ChangeType
    severity: Severity
    summary: str
    detected_at: datetime


class NotificationDeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    channel_id: UUID
    subscription_id: UUID
    change_id: UUID
    status: DeliveryStatus
    attempts: int
    max_attempts: int
    payload: dict[str, Any]
    response_status: int | None
    last_error: str | None
    sent_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
