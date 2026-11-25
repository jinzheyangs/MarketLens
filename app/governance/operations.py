"""租户范围的运维队列与积压快照。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ApprovalRequest,
    CollectionTask,
    DomainEvent,
    EventConsumerFailure,
    MonitorRun,
    NotificationDelivery,
    ReportExportJob,
    ResearchReview,
    ResearchRun,
)


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
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


async def _count(
    session: AsyncSession,
    model: type[
        ResearchRun
        | CollectionTask
        | ResearchReview
        | ReportExportJob
        | MonitorRun
        | NotificationDelivery
        | ApprovalRequest
        | DomainEvent
        | EventConsumerFailure
    ],
    *,
    tenant_id: UUID,
    statuses: tuple[str, ...],
) -> int:
    value = await session.scalar(
        select(func.count())
        .select_from(model)
        .where(
            model.tenant_id == tenant_id,
            model.status.in_(statuses),
        )
    )
    return int(value or 0)


async def queue_snapshot(
    session: AsyncSession,
    *,
    tenant_id: UUID,
) -> QueueSnapshot:
    return QueueSnapshot(
        generated_at=datetime.now(UTC),
        active_research_runs=await _count(
            session,
            ResearchRun,
            tenant_id=tenant_id,
            statuses=("pending", "running", "paused"),
        ),
        pending_collection_tasks=await _count(
            session,
            CollectionTask,
            tenant_id=tenant_id,
            statuses=("pending", "running", "retry"),
        ),
        pending_reviews=await _count(
            session,
            ResearchReview,
            tenant_id=tenant_id,
            statuses=("pending", "running", "awaiting_followup"),
        ),
        pending_exports=await _count(
            session,
            ReportExportJob,
            tenant_id=tenant_id,
            statuses=("pending", "running"),
        ),
        pending_monitor_runs=await _count(
            session,
            MonitorRun,
            tenant_id=tenant_id,
            statuses=("pending", "collecting"),
        ),
        pending_notification_deliveries=await _count(
            session,
            NotificationDelivery,
            tenant_id=tenant_id,
            statuses=("pending", "running"),
        ),
        pending_approvals=await _count(
            session,
            ApprovalRequest,
            tenant_id=tenant_id,
            statuses=("pending",),
        ),
        outbox_pending=await _count(
            session,
            DomainEvent,
            tenant_id=tenant_id,
            statuses=("pending", "publishing"),
        ),
        outbox_failed=await _count(
            session,
            DomainEvent,
            tenant_id=tenant_id,
            statuses=("failed",),
        ),
        consumer_failures_open=await _count(
            session,
            EventConsumerFailure,
            tenant_id=tenant_id,
            statuses=("open",),
        ),
        consumer_failures_dead_letter=await _count(
            session,
            EventConsumerFailure,
            tenant_id=tenant_id,
            statuses=("dead_letter",),
        ),
    )


__all__ = ["QueueSnapshot", "queue_snapshot"]
