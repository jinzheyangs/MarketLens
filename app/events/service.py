"""Tenant-admin event outbox and dead-letter operations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.enums import AuditAction, ResourceType
from app.auth.principal import Principal
from app.core.errors import ConflictError, NotFoundError
from app.db.models import DomainEvent, EventConsumerFailure
from app.db.tenant_scope import tenant_scoped_select
from app.events.enums import OutboxStatus
from app.services.audit import record_event


async def list_outbox_events(
    session: AsyncSession,
    *,
    status: OutboxStatus | None,
    event_type: str | None,
    limit: int,
    offset: int,
) -> list[DomainEvent]:
    statement = tenant_scoped_select(DomainEvent)
    if status is not None:
        statement = statement.where(DomainEvent.status == status.value)
    if event_type:
        statement = statement.where(DomainEvent.event_type == event_type[:100])
    statement = (
        statement.order_by(DomainEvent.created_at.desc(), DomainEvent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.scalars(statement)).all())


async def retry_failed_event(
    session: AsyncSession,
    *,
    event_id: UUID,
    principal: Principal,
) -> DomainEvent:
    event = cast(
        DomainEvent | None,
        await session.scalar(
            tenant_scoped_select(DomainEvent).where(DomainEvent.id == event_id).with_for_update()
        ),
    )
    if event is None:
        raise NotFoundError("The domain event was not found.")
    if event.status != OutboxStatus.FAILED.value:
        raise ConflictError("Only a failed domain event can be replayed.")
    event.status = OutboxStatus.PENDING.value
    event.attempts = 0
    event.available_at = datetime.now(UTC)
    event.lease_expires_at = None
    event.worker_id = None
    event.last_error = None
    event.published_at = None
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.EVENT_REPLAYED,
        resource_type=ResourceType.DOMAIN_EVENT,
        resource_id=event.id,
        details={"event_type": event.event_type, "event_id": str(event.event_id)},
    )
    await session.flush()
    await session.refresh(event)
    return event


async def list_consumer_failures(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
) -> list[EventConsumerFailure]:
    statement = (
        tenant_scoped_select(EventConsumerFailure)
        .order_by(
            EventConsumerFailure.last_failed_at.desc(),
            EventConsumerFailure.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return list((await session.scalars(statement)).all())


__all__ = [
    "list_consumer_failures",
    "list_outbox_events",
    "retry_failed_event",
]
