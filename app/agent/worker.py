"""持久化调研 worker 命令行入口（``python -m app.agent.worker``）。

同一进程内依次调度调研、证据审查与报告导出三类后台任务；
启用 Temporal 时改由工作流 worker 接管长流程编排。
"""

from __future__ import annotations

import argparse
import asyncio

from app.agent.runner import ResearchRunner
from app.agent.service import build_agent_services
from app.collector.service import build_collector_services
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.rls import install_rls_context
from app.db.session import create_engine, create_session_factory
from app.db.tenant_guard import install_tenant_guard
from app.reporting.runner import ExportRunner, ReviewRunner
from app.reporting.service import ReviewServices, build_review_services
from app.services.rag import build_rag_services

logger = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MarketScope multi-agent research worker")
    parser.add_argument("--once", action="store_true", help="process one batch and exit")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)
    if not settings.agent_enabled:
        logger.error("research agents are not enabled")
        return 1

    install_tenant_guard()
    install_rls_context()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    rag_services = build_rag_services(settings, session_factory) if settings.rag_enabled else None
    collector_services = (
        build_collector_services(settings, session_factory) if settings.collector_enabled else None
    )
    agent_services = build_agent_services(
        settings,
        session_factory,
        rag_services=rag_services,
        collector_services=collector_services,
    )
    runner = ResearchRunner(
        services=agent_services,
        session_factory=session_factory,
    )
    review_services: ReviewServices | None = None
    review_runner: ReviewRunner | None = None
    export_runner: ExportRunner | None = None
    if settings.report_enabled:
        review_services = build_review_services(settings, session_factory)
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

    async def run_batch() -> tuple[int, int, int]:
        researched = await runner.run_once()
        reviewed = await review_runner.run_once() if review_runner is not None else 0
        exported = await export_runner.run_once() if export_runner is not None else 0
        return researched, reviewed, exported

    async def run_forever() -> None:
        poll = settings.agent_poll_interval_seconds
        if review_runner is not None:
            poll = min(poll, settings.report_review_poll_interval_seconds)
        if export_runner is not None:
            poll = min(poll, settings.report_export_poll_interval_seconds)
        while True:
            try:
                researched, reviewed, exported = await run_batch()
            except Exception:
                logger.exception("research_worker_iteration_failed")
                researched = reviewed = exported = 0
            if researched == reviewed == exported == 0:
                await asyncio.sleep(poll)

    async def run() -> None:
        try:
            if rag_services is not None:
                await rag_services.ensure_ready()
            if collector_services is not None:
                await collector_services.ensure_ready()
            if review_services is not None:
                await review_services.ensure_ready()
            if settings.temporal_enabled:
                if args.once:
                    logger.error("temporal worker requires long-running mode")
                    return
                from app.workflow.worker import run_workflow_worker

                await run_workflow_worker(
                    settings=settings,
                    session_factory=session_factory,
                    agent_services=agent_services,
                    review_services=review_services,
                )
                return
            if args.once:
                researched, reviewed, exported = await run_batch()
                logger.info(
                    "research_batch_complete",
                    extra={
                        "context": {
                            "researched": researched,
                            "reviewed": reviewed,
                            "exported": exported,
                        }
                    },
                )
                return
            await run_forever()
        finally:
            if review_services is not None:
                await review_services.aclose()
            await agent_services.aclose()
            if collector_services is not None:
                await collector_services.aclose()
            if rag_services is not None:
                await rag_services.aclose()
            await engine.dispose()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("research_worker_stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
