"""Durable research worker CLI (``python -m app.agent.worker``)."""

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
from app.services.rag import build_rag_services

logger = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MarketLens multi-agent research worker")
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

    async def run() -> None:
        try:
            if rag_services is not None:
                await rag_services.ensure_ready()
            if collector_services is not None:
                await collector_services.ensure_ready()
            if args.once:
                processed = await runner.run_once()
                logger.info(
                    "research_batch_complete",
                    extra={"context": {"processed": processed}},
                )
                return
            await runner.run_forever()
        finally:
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
