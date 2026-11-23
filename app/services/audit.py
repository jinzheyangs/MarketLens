"""Audit event persistence.

Successful mutations write an audit row in the same transaction as the change.
Authorization denials are written through a separate short transaction so they
survive the rollback of the denied request.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.enums import AuditAction, AuditOutcome, Permission, ResourceType
from app.auth.principal import Principal
from app.core.context import current_request_id
from app.core.logging import get_logger
from app.db.models import AuditEvent
from app.db.tenant_scope import tenant_scoped_select

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
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action.value,
            resource_type=resource_type.value,
            resource_id=resource_id,
            outcome=outcome.value,
            request_id=current_request_id(),
            details=details or {},
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
