"""审计事件持久化。"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.enums import AuditAction, AuditOutcome, Permission, ResourceType
from app.auth.principal import Principal
from app.core.context import current_request_id
from app.core.logging import get_logger
from app.db.models import AuditEvent, DomainEvent
from app.db.tenant_scope import tenant_scoped_select
from app.events.enums import EVENT_SCHEMA_VERSION, OutboxStatus

logger = get_logger(__name__)


def record_event(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID | None,
    action: AuditAction,
    resource_type: ResourceType,
    resource_id: UUID | None,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    details: dict[str, Any] | None = None,
) -> None:
    audit_id = uuid4()
    request_id = current_request_id()
    session.add(
        AuditEvent(
            id=audit_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action.value,
            resource_type=resource_type.value,
            resource_id=resource_id,
            outcome=outcome.value,
            request_id=request_id,
            details=details or {},
        )
    )
    session.add(
        DomainEvent(
            tenant_id=tenant_id,
            event_id=audit_id,
            event_type=action.value,
            event_version=EVENT_SCHEMA_VERSION,
            aggregate_type=resource_type.value,
            aggregate_id=resource_id,
            actor_user_id=actor_user_id,
            payload={
                "audit_event_id": str(audit_id),
                "outcome": outcome.value,
                "resource_type": resource_type.value,
                "resource_id": str(resource_id) if resource_id else None,
                "details": details or {},
            },
            headers={"request_id": request_id} if request_id else {},
            partition_key=f"{tenant_id}:{resource_type.value}:{resource_id or 'none'}"[:128],
            idempotency_key=f"audit:{audit_id}",
            status=OutboxStatus.PENDING.value,
            attempts=0,
            max_attempts=8,
        )
    )


class AuditWriter:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_denial(
        self,
        *,
        principal: Principal,
        permission: Permission,
        resource_type: ResourceType,
        resource_id: UUID,
        reason: str,
    ) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                record_event(
                    session,
                    tenant_id=principal.tenant_id,
                    actor_user_id=principal.user_id,
                    action=AuditAction.AUTHORIZATION_DENIED,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    outcome=AuditOutcome.DENIED,
                    details={
                        "permission": permission.value,
                        "reason": reason,
                    },
                )
        except Exception:
            # The denied response must not turn into a 500 because auditing is
            # unavailable; the failure is visible in logs/alerting.
            logger.exception(
                "audit_denial_write_failed",
                extra={
                    "context": {
                        "tenant_id": str(principal.tenant_id),
                        "user_id": str(principal.user_id),
                        "permission": permission.value,
                    }
                },
            )


async def list_events(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
) -> list[AuditEvent]:
    statement = (
        tenant_scoped_select(AuditEvent)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.scalars(statement)).all())


__all__ = ["AuditWriter", "list_events", "record_event"]
