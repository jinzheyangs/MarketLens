"""Aggregate queue-depth exporter for Prometheus.

This is deliberately a separate process: it iterates active tenants, enters each
tenant context, and sums queue counts. Metrics contain no tenant labels, so they
are safe to scrape cluster-wide. Run it as a sidecar/Deployment with a
NetworkPolicy that only permits the monitoring namespace to reach its port.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID

from prometheus_client import CollectorRegistry, Gauge, start_http_server
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.context import tenant_context
from app.core.logging import configure_logging, get_logger
from app.db.models import Tenant
from app.db.rls import install_rls_context
from app.db.session import create_engine, create_session_factory
from app.db.tenant_guard import install_tenant_guard
from app.governance.operations import QueueSnapshot, queue_snapshot

logger = get_logger(__name__)

_QUEUE_FIELDS = (
    "active_research_runs",
    "pending_collection_tasks",
    "pending_reviews",
    "pending_exports",
    "pending_monitor_runs",
    "pending_notification_deliveries",
    "pending_approvals",
    "outbox_pending",
    "outbox_failed",
    "consumer_failures_open",
    "consumer_failures_dead_letter",
)


def _empty() -> dict[str, int]:
    return dict.fromkeys(_QUEUE_FIELDS, 0)


async def collect_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_ids: list[UUID] | None = None,
) -> dict[str, int]:
    async with session_factory() as session:
        statement = select(Tenant.id).where(Tenant.status == "active")
        if tenant_ids is not None:
            statement = statement.where(Tenant.id.in_(tenant_ids))
        active = list((await session.scalars(statement)).all())
    totals = _empty()
    for tenant_id in active:
        with tenant_context(tenant_id):
            async with session_factory() as session:
                snapshot: QueueSnapshot = await queue_snapshot(
                    session,
                    tenant_id=tenant_id,
                )
        for field, value in asdict(snapshot).items():
            if field in totals and isinstance(value, int):
                totals[field] += value
    totals["active_tenants"] = len(active)
    return totals


def _gauges(registry: CollectorRegistry) -> dict[str, Gauge]:
    return {
        field: Gauge(
            f"marketlens_queue_{field}",
            "Aggregate tenants' operational backlog (no tenant labels).",
            registry=registry,
        )
        for field in (*_QUEUE_FIELDS, "active_tenants")
    }


async def run_forever(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    registry = CollectorRegistry(auto_describe=True)
    gauges = _gauges(registry)
    last_success = Gauge(
        "marketlens_queue_last_success_timestamp_seconds",
        "Unix timestamp of the last successful queue snapshot.",
        registry=registry,
    )
    start_http_server(settings.ops_metrics_port, registry=registry)
    logger.info(
        "ops_metrics_exporter_started",
        extra={"context": {"port": settings.ops_metrics_port}},
    )
    while True:
        try:
            totals = await collect_snapshot(session_factory)
            for field, gauge in gauges.items():
                gauge.set(totals.get(field, 0))
            last_success.set(datetime.now(UTC).timestamp())
        except Exception:
            logger.exception("ops_metrics_snapshot_failed")
        await asyncio.sleep(settings.ops_metrics_interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MarketLens queue metrics exporter")
    parser.add_argument(
        "--once",
        action="store_true",
        help="collect and print one aggregate snapshot, then exit",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)
    install_tenant_guard()
    install_rls_context()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    async def run() -> None:
        try:
            if args.once:
                totals = await collect_snapshot(session_factory)
                logger.info("ops_metrics_snapshot", extra={"context": totals})
                return
            await run_forever(settings, session_factory)
        finally:
            await engine.dispose()

    asyncio.run(run())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
