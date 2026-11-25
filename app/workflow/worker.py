"""Temporal worker 进程与报告队列安全循环。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.worker import Worker

from app.agent.service import AgentServices
from app.core.config import Settings
from app.reporting.runner import ExportRunner, ReviewRunner
from app.reporting.service import ReviewServices
from app.workflow.activities import ResearchActivities
from app.workflow.starter import TemporalStarter
from app.workflow.workflows import ResearchLifecycleWorkflow

logger = logging.getLogger(__name__)


async def _report_safety_loop(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    review_services: ReviewServices | None,
) -> None:
    """Process manual/late review and export jobs outside workflow lifetimes."""
    if review_services is None:
        return
    review_runner = ReviewRunner(
        settings=settings,
        services=review_services,
        session_factory=session_factory,
    )
    export_runner = ExportRunner(
        settings=settings,
        services=review_services,
        session_factory=session_factory,
    )
    while True:
        try:
            reviewed = await review_runner.run_once()
            exported = await export_runner.run_once()
        except Exception:
            logger.exception("report_safety_loop_failed")
            reviewed = exported = 0
        if reviewed == 0 and exported == 0:
            await asyncio.sleep(settings.report_review_poll_interval_seconds)


async def run_workflow_worker(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    agent_services: AgentServices,
    review_services: ReviewServices | None,
) -> None:
    if not settings.temporal_enabled or not settings.temporal_host:
        raise RuntimeError("Temporal is not configured")
    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
        tls=settings.temporal_tls,
    )
    activities = ResearchActivities(
        settings=settings,
        session_factory=session_factory,
        agent_services=agent_services,
        review_services=review_services,
    )
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[ResearchLifecycleWorkflow],
        activities=[
            activities.get_run_state,
            activities.run_research_batch,
            activities.run_review_batch,
            activities.run_export_batch,
        ],
        max_concurrent_activities=max(
            settings.agent_max_concurrent_runs,
            settings.report_max_concurrent_reviews,
        ),
        graceful_shutdown_timeout=timedelta(seconds=30),
    )
    starter = TemporalStarter(
        settings=settings,
        client=client,
        session_factory=session_factory,
    )
    logger.info(
        "temporal_worker_started",
        extra={
            "context": {
                "task_queue": settings.temporal_task_queue,
                "namespace": settings.temporal_namespace,
            }
        },
    )
    await asyncio.gather(
        worker.run(),
        starter.run_forever(),
        _report_safety_loop(
            settings=settings,
            session_factory=session_factory,
            review_services=review_services,
        ),
    )


__all__: Sequence[str] = ["run_workflow_worker"]
