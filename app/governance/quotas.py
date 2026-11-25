"""租户配额：基于行锁/咨询锁强制校验。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import QuotaExceededError
from app.db.models import (
    CollectionTask,
    MonitorRun,
    MonitorSubscription,
    NotificationDelivery,
    ResearchRun,
    TenantQuota,
)
from app.db.tenant_scope import tenant_scoped_select


class QuotaResource(StrEnum):
    RESEARCH_RUNS = "research_runs"
    COLLECTION_TASKS = "collection_tasks"
    MONITOR_SUBSCRIPTIONS = "monitor_subscriptions"
    MONITOR_RUNS = "monitor_runs"
    NOTIFICATION_DELIVERIES = "notification_deliveries"


@dataclass(frozen=True, slots=True)
class QuotaUsage:
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


def _defaults(settings: Settings, tenant_id: UUID) -> TenantQuota:
    return TenantQuota(
        id=uuid4(),
        tenant_id=tenant_id,
        max_active_research_runs=settings.quota_default_max_active_research_runs,
        max_pending_collection_tasks=settings.quota_default_max_pending_collection_tasks,
        max_monitor_subscriptions=settings.quota_default_max_monitor_subscriptions,
        max_concurrent_monitor_runs=settings.quota_default_max_concurrent_monitor_runs,
        max_notification_deliveries_per_day=(
            settings.quota_default_max_notification_deliveries_per_day
        ),
    )


async def get_or_create_quota(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    settings: Settings,
) -> TenantQuota:
    quota = await session.scalar(
        tenant_scoped_select(TenantQuota)
        .where(TenantQuota.tenant_id == tenant_id)
        .with_for_update()
    )
    if quota is not None:
        return quota
    quota = _defaults(settings, tenant_id)
    try:
        async with session.begin_nested():
            session.add(quota)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            tenant_scoped_select(TenantQuota).where(TenantQuota.tenant_id == tenant_id)
        )
        if existing is None:
            raise
        return existing
    return quota


async def _usage_counts(
    session: AsyncSession,
    *,
    tenant_id: UUID,
) -> tuple[int, int, int, int, int]:
    active_research = int(
        await session.scalar(
            select(func.count())
            .select_from(ResearchRun)
            .where(
                ResearchRun.tenant_id == tenant_id,
                ResearchRun.status.in_(["pending", "running", "paused"]),
            )
        )
        or 0
    )
    pending_collection = int(
        await session.scalar(
            select(func.count())
            .select_from(CollectionTask)
            .where(
                CollectionTask.tenant_id == tenant_id,
                CollectionTask.status.in_(["pending", "running", "retry"]),
            )
        )
        or 0
    )
    monitor_subscriptions = int(
        await session.scalar(
            select(func.count())
            .select_from(MonitorSubscription)
            .where(
                MonitorSubscription.tenant_id == tenant_id,
                MonitorSubscription.status != "deleted",
            )
        )
        or 0
    )
    concurrent_monitor = int(
        await session.scalar(
            select(func.count())
            .select_from(MonitorRun)
            .where(
                MonitorRun.tenant_id == tenant_id,
                MonitorRun.status.in_(["pending", "collecting"]),
            )
        )
        or 0
    )
    notification_deliveries = int(
        await session.scalar(
            select(func.count())
            .select_from(NotificationDelivery)
            .where(
                NotificationDelivery.tenant_id == tenant_id,
                NotificationDelivery.created_at >= datetime.now(UTC) - timedelta(hours=24),
            )
        )
        or 0
    )
    return (
        active_research,
        pending_collection,
        monitor_subscriptions,
        concurrent_monitor,
        notification_deliveries,
    )


def _limit_for(quota: TenantQuota, resource: QuotaResource) -> int:
    if resource is QuotaResource.RESEARCH_RUNS:
        return quota.max_active_research_runs
    if resource is QuotaResource.COLLECTION_TASKS:
        return quota.max_pending_collection_tasks
    if resource is QuotaResource.MONITOR_SUBSCRIPTIONS:
        return quota.max_monitor_subscriptions
    if resource is QuotaResource.MONITOR_RUNS:
        return quota.max_concurrent_monitor_runs
    return quota.max_notification_deliveries_per_day


def _usage_for(
    usage: tuple[int, int, int, int, int],
    resource: QuotaResource,
) -> int:
    return {
        QuotaResource.RESEARCH_RUNS: usage[0],
        QuotaResource.COLLECTION_TASKS: usage[1],
        QuotaResource.MONITOR_SUBSCRIPTIONS: usage[2],
        QuotaResource.MONITOR_RUNS: usage[3],
        QuotaResource.NOTIFICATION_DELIVERIES: usage[4],
    }[resource]


async def ensure_quota(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    resource: QuotaResource,
    settings: Settings,
    increment: int = 1,
) -> None:
    """Serialize quota checks per tenant/resource and fail closed on excess."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"quota:{tenant_id}:{resource.value}"},
    )
    quota = await get_or_create_quota(session, tenant_id=tenant_id, settings=settings)
    usage = await _usage_counts(session, tenant_id=tenant_id)
    current = _usage_for(usage, resource)
    limit = _limit_for(quota, resource)
    if current + increment > limit:
        raise QuotaExceededError(f"The tenant {resource.value} quota has been exceeded.")


async def quota_snapshot(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    settings: Settings,
) -> QuotaUsage:
    quota = await get_or_create_quota(session, tenant_id=tenant_id, settings=settings)
    usage = await _usage_counts(session, tenant_id=tenant_id)
    return QuotaUsage(
        max_active_research_runs=quota.max_active_research_runs,
        active_research_runs=usage[0],
        max_pending_collection_tasks=quota.max_pending_collection_tasks,
        pending_collection_tasks=usage[1],
        max_monitor_subscriptions=quota.max_monitor_subscriptions,
        monitor_subscriptions=usage[2],
        max_concurrent_monitor_runs=quota.max_concurrent_monitor_runs,
        concurrent_monitor_runs=usage[3],
        max_notification_deliveries_per_day=(quota.max_notification_deliveries_per_day),
        notification_deliveries_last_24h=usage[4],
    )


async def update_quota(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    settings: Settings,
    values: dict[str, int],
) -> TenantQuota:
    quota = await get_or_create_quota(session, tenant_id=tenant_id, settings=settings)
    for field, value in values.items():
        if value < 1:
            raise ValueError("quota limits must be positive")
        setattr(quota, field, value)
    await session.flush()
    await session.refresh(quota)
    return quota


__all__ = [
    "QuotaResource",
    "QuotaUsage",
    "ensure_quota",
    "get_or_create_quota",
    "quota_snapshot",
    "update_quota",
]
