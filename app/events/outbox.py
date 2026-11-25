"""事务性 outbox 派发器：租约、退避重试与死信状态。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.context import tenant_context
from app.db.models import DomainEvent, Tenant
from app.events.enums import OutboxStatus
from app.events.publisher import EventPublisher, EventPublisherError
from app.events.schemas import DomainEventEnvelope

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class OutboxDispatcher:
    def __init__(
        self,
        *,
        settings: Settings,
        publisher: EventPublisher,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._publisher = publisher
        self._session_factory = session_factory
        self._worker_id = f"outbox-{uuid4().hex[:12]}"

    async def run_forever(self) -> None:
        await self._publisher.start()
        while True:
            try:
                processed = await self.dispatch_once()
            except Exception:
                logger.exception("outbox_dispatch_iteration_failed")
                processed = 0
            if processed == 0:
                await asyncio.sleep(self._settings.event_outbox_poll_interval_seconds)

    async def dispatch_once(
        self,
        *,
        tenant_ids: Sequence[UUID] | None = None,
    ) -> int:
        if not self._settings.events_outbox_enabled:
            return 0
        claimed = await self._claim_events(tenant_ids=tenant_ids)
        for event in claimed:
            envelope = DomainEventEnvelope(
                event_id=event.event_id,
                event_type=event.event_type,
                event_version=event.event_version,
                tenant_id=event.tenant_id,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                actor_user_id=event.actor_user_id,
                occurred_at=event.occurred_at,
                partition_key=event.partition_key,
                payload=event.payload,
                headers=event.headers,
            )
            try:
                await self._publisher.publish(envelope)
            except EventPublisherError as exc:
                await self._mark_failure(event, str(exc))
            except Exception as exc:
                logger.exception(
                    "outbox_publish_unexpected_error",
                    extra={"context": {"event_id": str(event.event_id)}},
                )
                await self._mark_failure(event, type(exc).__name__)
            else:
                await self._mark_published(event)
        return len(claimed)

    async def _claim_events(
        self,
        *,
        tenant_ids: Sequence[UUID] | None,
    ) -> list[DomainEvent]:
        async with self._session_factory() as session:
            tenant_statement = select(Tenant.id).where(Tenant.status == "active")
            if tenant_ids is not None:
                tenant_statement = tenant_statement.where(Tenant.id.in_(tenant_ids))
            active_tenant_ids = list((await session.scalars(tenant_statement)).all())

        claimed: list[DomainEvent] = []
        now = _now()
        for tenant_id in active_tenant_ids:
            remaining = self._settings.event_outbox_batch_size - len(claimed)
            if remaining <= 0:
                break
            with tenant_context(tenant_id):
                async with self._session_factory() as session, session.begin():
                    statement = (
                        select(DomainEvent)
                        .where(
                            DomainEvent.tenant_id == tenant_id,
                            or_(
                                and_(
                                    DomainEvent.status == OutboxStatus.PENDING.value,
                                    DomainEvent.available_at <= now,
                                ),
                                and_(
                                    DomainEvent.status == OutboxStatus.PUBLISHING.value,
                                    or_(
                                        DomainEvent.lease_expires_at.is_(None),
                                        DomainEvent.lease_expires_at <= now,
                                    ),
                                ),
                            ),
                        )
                        .order_by(DomainEvent.available_at, DomainEvent.created_at)
                        .limit(remaining)
                        .with_for_update(skip_locked=True)
                    )
                    for event in (await session.scalars(statement)).all():
                        max_attempts = min(
                            event.max_attempts, self._settings.event_outbox_max_attempts
                        )
                        if event.attempts >= max_attempts:
                            event.status = OutboxStatus.FAILED.value
                            event.last_error = "max_attempts_exceeded"
                            event.lease_expires_at = None
                            event.worker_id = None
                            continue
                        event.status = OutboxStatus.PUBLISHING.value
                        event.attempts += 1
                        event.worker_id = self._worker_id
                        event.lease_expires_at = now + timedelta(
                            seconds=self._settings.event_outbox_lease_seconds
                        )
                        claimed.append(event)
        return claimed

    async def _mark_published(self, event: DomainEvent) -> None:
        with tenant_context(event.tenant_id):
            async with self._session_factory() as session, session.begin():
                current = await session.scalar(
                    select(DomainEvent)
                    .where(
                        DomainEvent.id == event.id,
                        DomainEvent.worker_id == self._worker_id,
                        DomainEvent.status == OutboxStatus.PUBLISHING.value,
                    )
                    .with_for_update()
                )
                if current is None:
                    return
                current.status = OutboxStatus.PUBLISHED.value
                current.published_at = _now()
                current.lease_expires_at = None
                current.worker_id = None
                current.last_error = None
                await session.flush()

    async def _mark_failure(self, event: DomainEvent, error: str) -> None:
        with tenant_context(event.tenant_id):
            async with self._session_factory() as session, session.begin():
                current = await session.scalar(
                    select(DomainEvent)
                    .where(
                        DomainEvent.id == event.id,
                        DomainEvent.worker_id == self._worker_id,
                        DomainEvent.status == OutboxStatus.PUBLISHING.value,
                    )
                    .with_for_update()
                )
                if current is None:
                    return
                current.lease_expires_at = None
                current.worker_id = None
                current.last_error = error[:500]
                max_attempts = min(current.max_attempts, self._settings.event_outbox_max_attempts)
                if current.attempts < max_attempts:
                    current.status = OutboxStatus.PENDING.value
                    delay = min(
                        self._settings.event_outbox_retry_base_seconds
                        * (2 ** max(0, current.attempts - 1)),
                        self._settings.event_outbox_retry_max_seconds,
                    )
                    current.available_at = _now() + timedelta(seconds=delay)
                else:
                    current.status = OutboxStatus.FAILED.value
                logger.error(
                    "outbox_publish_failed",
                    extra={
                        "context": {
                            "event_id": str(current.event_id),
                            "event_type": current.event_type,
                            "attempts": current.attempts,
                            "error": error[:120],
                        }
                    },
                )
                await session.flush()


__all__ = ["OutboxDispatcher"]
