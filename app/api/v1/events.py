"""Tenant-admin event outbox and consumer dead-letter APIs."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.auth.authorization import AuthorizationService
from app.auth.dependencies import get_authorization, get_principal
from app.auth.enums import Permission
from app.auth.principal import Principal
from app.events.enums import OutboxStatus
from app.events.service import (
    list_consumer_failures,
    list_outbox_events,
    retry_failed_event,
)
from app.schemas.events import DomainEventRead, EventConsumerFailureRead

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=list[DomainEventRead], summary="List domain outbox events")
async def list_events(
    status: OutboxStatus | None = Query(default=None),
    event_type: str | None = Query(default=None, max_length=100),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[DomainEventRead]:
    await authz.require_tenant_admin(Permission.EVENT_READ)
    events = await list_outbox_events(
        session,
        status=status,
        event_type=event_type,
        limit=limit,
        offset=offset,
    )
    return [DomainEventRead.model_validate(event) for event in events]


@router.post(
    "/{event_id}/retry",
    response_model=DomainEventRead,
    summary="Replay a failed domain event",
)
async def replay_event(
    event_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> DomainEventRead:
    await authz.require_tenant_admin(Permission.EVENT_MANAGE)
    event = await retry_failed_event(session, event_id=event_id, principal=principal)
    return DomainEventRead.model_validate(event)


@router.get(
    "/consumer-failures",
    response_model=list[EventConsumerFailureRead],
    summary="List event consumer dead letters",
)
async def list_dead_letters(
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[EventConsumerFailureRead]:
    await authz.require_tenant_admin(Permission.EVENT_READ)
    failures = await list_consumer_failures(session, limit=limit, offset=offset)
    return [EventConsumerFailureRead.model_validate(failure) for failure in failures]
