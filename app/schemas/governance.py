"""租户配额接口 Schema。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

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


class CostBucketRead(BaseModel):
    workspace_id: UUID
    project_id: UUID | None
    model_provider: str
    model_name: str
    runs: int
    input_tokens: int
    output_tokens: int
    cost_micros: int
    tool_calls: int


class CostSummaryRead(BaseModel):
    since: datetime
    until: datetime
    runs: int
    input_tokens: int
    output_tokens: int
    cost_micros: int
    tool_calls: int
    buckets: list[CostBucketRead]


class QueueSnapshotRead(BaseModel):
    generated_at: datetime
    active_research_runs: int
    pending_collection_tasks: int
    pending_reviews: int
    pending_exports: int
    pending_monitor_runs: int
    pending_notification_deliveries: int
    pending_approvals: int
    outbox_pending: int
    outbox_failed: int
    consumer_failures_open: int
    consumer_failures_dead_letter: int


class DomainPolicyRead(BaseModel):
    allowed_domains: list[str]
    blocked_domains: list[str]


class DomainPolicyUpdate(BaseModel):
    allowed_domains: list[str] | None = Field(default=None, max_length=500)
    blocked_domains: list[str] | None = Field(default=None, max_length=500)
