"""Tenant quota API schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class QuotaRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    max_active_research_runs: int
    active_research_runs: int
    max_pending_collection_tasks: int
    pending_collection_tasks: int
    max_monitor_subscriptions: int
    monitor_subscriptions: int
    max_concurrent_monitor_runs: int
    concurrent_monitor_runs: int
    max_notification_deliveries_per_day: int
    notification_deliveries_last_24h: int


class QuotaUpdate(BaseModel):
    max_active_research_runs: int | None = Field(default=None, ge=1, le=10_000)
    max_pending_collection_tasks: int | None = Field(default=None, ge=1, le=100_000)
    max_monitor_subscriptions: int | None = Field(default=None, ge=1, le=10_000)
    max_concurrent_monitor_runs: int | None = Field(default=None, ge=1, le=100)
    max_notification_deliveries_per_day: int | None = Field(default=None, ge=1, le=1_000_000)
