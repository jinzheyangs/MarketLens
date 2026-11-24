"""Outbox dispatcher and Kafka event consumer CLI."""

from __future__ import annotations

import argparse
import asyncio

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.rls import install_rls_context
from app.db.session import create_engine, create_session_factory
from app.db.tenant_guard import install_tenant_guard
from app.events.consumer import EventConsumer
from app.events.handlers import build_event_handlers
from app.events.outbox import OutboxDispatcher
from app.events.publisher import build_event_publisher

logger = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MarketScope outbox/event worker")
    parser.add_argument("--once", action="store_true", help="dispatch one batch and exit")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)
    if not settings.events_outbox_enabled and not settings.event_consumer_enabled:
        logger.error("event outbox and consumer are disabled")
        return 1

    install_tenant_guard()
    install_rls_context()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    publisher = build_event_publisher(settings)
    dispatcher = OutboxDispatcher(
        settings=settings,
        publisher=publisher,
        session_factory=session_factory,
    )
    consumer = (
        EventConsumer(
            settings=settings,
            session_factory=session_factory,
            handlers=build_event_handlers(settings),
        )
        if settings.event_consumer_enabled
        else None
    )

    async def run() -> None:
        try:
            if args.once:
                dispatched = await dispatcher.dispatch_once()
                consumed = await consumer.consume_once() if consumer is not None else 0
                logger.info(
                    "event_batch_complete",
                    extra={"context": {"dispatched": dispatched, "consumed": consumed}},
                )
                return
            tasks = [asyncio.create_task(dispatcher.run_forever())]
            if consumer is not None:
                tasks.append(asyncio.create_task(consumer.run_forever()))
            await asyncio.gather(*tasks)
        finally:
            if consumer is not None:
                await consumer.aclose()
            await publisher.aclose()
            await engine.dispose()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("event_worker_stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
