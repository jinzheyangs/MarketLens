"""从事务性 outbox 精确一次地启动 Temporal 工作流。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.agent.enums import TERMINAL_RUN_STATUSES, ResearchRunKind
from app.auth.enums import AuditAction
from app.core.config import Settings
from app.core.context import tenant_context
from app.db.models import DomainEvent, ProcessedEvent, ResearchRun, Tenant
from app.db.tenant_scope import tenant_scoped_select
from app.workflow.workflows import ResearchLifecycleWorkflow

logger = logging.getLogger(__name__)

_CONSUMER = "temporal-starter"


class TemporalStarter:
    def __init__(
        self,
        *,
        settings: Settings,
        client: Client,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._client = client
        self._session_factory = session_factory

    async def run_forever(self) -> None:
        while True:
            try:
                started = await self.run_once()
            except Exception:
                logger.exception("temporal_starter_iteration_failed")
                started = 0
            if started == 0:
                await asyncio.sleep(self._settings.temporal_poll_interval_seconds)

    async def run_once(self, *, tenant_ids: Sequence[UUID] | None = None) -> int:
        async with self._session_factory() as session:
            tenant_statement = select(Tenant.id).where(Tenant.status == "active")
            if tenant_ids is not None:
                tenant_statement = tenant_statement.where(Tenant.id.in_(tenant_ids))
            active_tenant_ids = list((await session.scalars(tenant_statement)).all())

        started = 0
        for tenant_id in active_tenant_ids:
            with tenant_context(tenant_id):
                async with self._session_factory() as session:
                    events = list(
                        (
                            await session.scalars(
                                tenant_scoped_select(DomainEvent)
                                .where(
                                    DomainEvent.event_type
                                    == AuditAction.RESEARCH_RUN_CREATED.value,
                                    DomainEvent.aggregate_id.is_not(None),
                                )
                                .order_by(DomainEvent.created_at)
                                .limit(50)
                            )
                        ).all()
                    )
                    for event in events:
                        if await session.scalar(
                            select(ProcessedEvent.id).where(
                                ProcessedEvent.tenant_id == tenant_id,
                                ProcessedEvent.consumer == _CONSUMER,
                                ProcessedEvent.event_id == event.event_id,
                            )
                        ):
                            continue
                        run_id = event.aggregate_id
                        if run_id is None:
                            continue
                        run = await session.scalar(
                            tenant_scoped_select(ResearchRun).where(ResearchRun.id == run_id)
                        )
                        if (
                            run is None
                            or run.run_kind != ResearchRunKind.PRIMARY.value
                            or tenant_id != run.tenant_id
                            or run.status in {status.value for status in TERMINAL_RUN_STATUSES}
                        ):
                            await self._mark_processed(
                                session,
                                tenant_id,
                                event.event_id,
                                event.event_type,
                            )
                            continue
                        with suppress(WorkflowAlreadyStartedError):
                            await self._client.start_workflow(
                                ResearchLifecycleWorkflow.run,
                                {
                                    "tenant_id": str(tenant_id),
                                    "run_id": str(run_id),
                                    "poll_seconds": (self._settings.temporal_poll_interval_seconds),
                                    "deadline_seconds": (
                                        self._settings.temporal_workflow_max_seconds
                                    ),
                                },
                                id=f"research-lifecycle-{run_id}",
                                task_queue=self._settings.temporal_task_queue,
                                execution_timeout=timedelta(
                                    seconds=self._settings.temporal_workflow_max_seconds
                                ),
                                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                            )
                        await self._mark_processed(
                            session,
                            tenant_id,
                            event.event_id,
                            event.event_type,
                        )
                        started += 1
        return started

    async def _mark_processed(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        event_id: UUID,
        event_type: str,
    ) -> None:
        try:
            async with session.begin_nested():
                session.add(
                    ProcessedEvent(
                        tenant_id=tenant_id,
                        consumer=_CONSUMER,
                        event_id=event_id,
                        event_type=event_type,
                    )
                )
                await session.flush()
            await session.commit()
        except IntegrityError:
            return


__all__ = ["TemporalStarter"]
