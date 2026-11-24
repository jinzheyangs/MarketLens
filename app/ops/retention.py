"""Bounded retention job for operational event/audit-adjacent tables.

This job intentionally never deletes business artifacts (reports, evidence,
claims, comments, monitor changes, documents). Those require a tenant-specific
data-lifecycle decision. Destructive mode is opt-in with
``RETENTION_DESTRUCTIVE_ENABLED=true`` and ``--execute``.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.auth.enums import AuditAction, ResourceType
from app.core.config import Settings, get_settings
from app.core.context import tenant_context
from app.core.logging import configure_logging, get_logger
from app.db.models import (
    DomainEvent,
    EventConsumerFailure,
    ProcessedEvent,
    Tenant,
)
from app.db.rls import install_rls_context
from app.db.session import create_engine, create_session_factory
from app.db.tenant_guard import install_tenant_guard
from app.services.audit import record_event

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PurgeResult:
    tenant_id: UUID
    table: str
    rows: int


def _cutoff(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)


async def _purge_table(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    model: type[ProcessedEvent] | type[EventConsumerFailure] | type[DomainEvent],
    timestamp_column: Any,
    filters: Sequence[ColumnElement[bool]],
    cutoff: datetime,
    batch_size: int,
    execute: bool,
) -> int:
    total = 0
    predicate = (cast(ColumnElement[bool], timestamp_column < cutoff), *filters)
    while True:
        id_statement = (
            select(model.id).where(model.tenant_id == tenant_id, *predicate).limit(batch_size)
        )
        ids = list((await session.scalars(id_statement)).all())
        if not ids:
            break
        if not execute:
            count = await session.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    model.tenant_id == tenant_id,
                    *predicate,
                )
            )
            total = int(count or 0)
            break
        await session.execute(delete(model).where(model.id.in_(ids)))
        await session.flush()
        total += len(ids)
        if len(ids) < batch_size:
            break
    return total


async def purge_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    tenant_id: UUID,
    now: datetime,
    execute: bool,
) -> list[PurgeResult]:
    operational_cutoff = _cutoff(now, settings.retention_operational_days)
    event_cutoff = _cutoff(now, settings.retention_event_days)
    batch_size = settings.retention_batch_size
    results: list[PurgeResult] = []
    with tenant_context(tenant_id):
        async with session_factory() as session, session.begin():
            counts = {
                "processed_events": await _purge_table(
                    session,
                    tenant_id=tenant_id,
                    model=ProcessedEvent,
                    timestamp_column=ProcessedEvent.processed_at,
                    filters=(),
                    cutoff=operational_cutoff,
                    batch_size=batch_size,
                    execute=execute,
                ),
                "resolved_consumer_failures": await _purge_table(
                    session,
                    tenant_id=tenant_id,
                    model=EventConsumerFailure,
                    timestamp_column=EventConsumerFailure.resolved_at,
                    filters=(EventConsumerFailure.status == "resolved",),
                    cutoff=operational_cutoff,
                    batch_size=batch_size,
                    execute=execute,
                ),
                "published_domain_events": await _purge_table(
                    session,
                    tenant_id=tenant_id,
                    model=DomainEvent,
                    timestamp_column=DomainEvent.published_at,
                    filters=(DomainEvent.status == "published",),
                    cutoff=event_cutoff,
                    batch_size=batch_size,
                    execute=execute,
                ),
            }
            for table, rows in counts.items():
                if rows:
                    results.append(PurgeResult(tenant_id=tenant_id, table=table, rows=rows))
            total = sum(counts.values())
            if execute and total:
                record_event(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=None,
                    action=AuditAction.RETENTION_PURGED,
                    resource_type=ResourceType.TENANT,
                    resource_id=tenant_id,
                    details=counts,
                )
    return results


async def run_retention(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tenant_ids: Sequence[UUID] | None = None,
    execute: bool = False,
    now: datetime | None = None,
) -> list[PurgeResult]:
    effective_now = now or datetime.now(UTC)
    async with session_factory() as session:
        statement = select(Tenant.id).where(Tenant.status == "active")
        if tenant_ids is not None:
            statement = statement.where(Tenant.id.in_(tenant_ids))
        active = list((await session.scalars(statement)).all())
    results: list[PurgeResult] = []
    for tenant_id in active:
        results.extend(
            await purge_tenant(
                session_factory,
                settings=settings,
                tenant_id=tenant_id,
                now=effective_now,
                execute=execute,
            )
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MarketScope operational retention")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="delete eligible rows (default is a dry-run count)",
    )
    parser.add_argument("--tenant", action="append", help="restrict to a tenant UUID")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)
    if args.execute and not settings.retention_destructive_enabled:
        logger.error("retention_destructive_disabled")
        return 2

    tenant_ids = None
    if args.tenant:
        try:
            tenant_ids = [UUID(value) for value in args.tenant]
        except ValueError:
            logger.error("retention_invalid_tenant_id")
            return 2

    install_tenant_guard()
    install_rls_context()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    async def run() -> list[PurgeResult]:
        try:
            return await run_retention(
                settings=settings,
                session_factory=session_factory,
                tenant_ids=tenant_ids,
                execute=args.execute,
            )
        finally:
            await engine.dispose()

    results = asyncio.run(run())
    for result in results:
        logger.info(
            "retention_rows",
            extra={
                "context": {
                    "tenant_id": str(result.tenant_id),
                    "table": result.table,
                    "rows": result.rows,
                    "execute": args.execute,
                }
            },
        )
    total = sum(result.rows for result in results)
    logger.info(
        "retention_complete",
        extra={"context": {"rows": total, "execute": args.execute}},
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
