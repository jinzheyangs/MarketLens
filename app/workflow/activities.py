"""Temporal activities that drive the existing durable research workers."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from app.agent.runner import ResearchRunner
from app.agent.service import AgentServices
from app.core.config import Settings
from app.core.context import tenant_context
from app.db.models import ResearchReview, ResearchRun
from app.db.tenant_scope import tenant_scoped_select
from app.reporting.runner import ExportRunner, ReviewRunner
from app.reporting.service import ReviewServices


class ResearchActivities:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        agent_services: AgentServices,
        review_services: ReviewServices | None,
    ) -> None:
        self._session_factory = session_factory
        self._research_runner = ResearchRunner(
            services=agent_services,
            session_factory=session_factory,
        )
        self._review_runner = (
            ReviewRunner(
                settings=settings,
                services=review_services,
                session_factory=session_factory,
            )
            if review_services is not None
            else None
        )
        self._export_runner = (
            ExportRunner(
                settings=settings,
                services=review_services,
                session_factory=session_factory,
            )
            if review_services is not None
            else None
        )

    @activity.defn(name="get_run_state")
    async def get_run_state(self, tenant_id: str, run_id: str) -> dict[str, object]:
        with tenant_context(UUID(tenant_id)):
            async with self._session_factory() as session:
                run = await session.scalar(
                    tenant_scoped_select(ResearchRun).where(ResearchRun.id == UUID(run_id))
                )
                if run is None:
                    return {
                        "run_status": "missing",
                        "review_status": None,
                        "pause_requested": False,
                        "cancel_requested": False,
                    }
                review_status = await session.scalar(
                    select(ResearchReview.status).where(
                        ResearchReview.tenant_id == run.tenant_id,
                        ResearchReview.root_run_id == run.id,
                    )
                )
                return {
                    "run_status": run.status,
                    "review_status": review_status,
                    "pause_requested": run.pause_requested,
                    "cancel_requested": run.cancel_requested,
                    "run_kind": run.run_kind,
                }

    @activity.defn(name="run_research_batch")
    async def run_research_batch(self, tenant_id: str) -> int:
        return await self._research_runner.run_once(tenant_ids=[UUID(tenant_id)])

    @activity.defn(name="run_review_batch")
    async def run_review_batch(self, tenant_id: str) -> int:
        if self._review_runner is None:
            return 0
        return await self._review_runner.run_once(tenant_ids=[UUID(tenant_id)])

    @activity.defn(name="run_export_batch")
    async def run_export_batch(self, tenant_id: str) -> int:
        if self._export_runner is None:
            return 0
        return await self._export_runner.run_once(tenant_ids=[UUID(tenant_id)])


__all__ = ["ResearchActivities"]
