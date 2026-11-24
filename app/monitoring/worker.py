"""Scheduled monitoring, change detection, and notification delivery worker."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.enums import AuditAction, AuditOutcome, ResourceType
from app.auth.principal import Principal, resolve_principal_for_user
from app.collector.errors import CollectorError
from app.collector.service import (
    build_collector_services,
    enqueue_tasks,
    filter_search_hits,
    require_search_provider,
)
from app.core.config import Settings, get_settings
from app.core.context import tenant_context
from app.core.errors import QuotaExceededError
from app.core.logging import configure_logging, get_logger
from app.db.models import (
    CollectionTask,
    MonitorChange,
    MonitorRun,
    MonitorSeenSource,
    MonitorSubscription,
    NotificationChannel,
    NotificationDelivery,
    SourceSnapshot,
    Tenant,
    WebSource,
)
from app.db.rls import install_rls_context
from app.db.session import create_engine, create_session_factory
from app.db.tenant_guard import install_tenant_guard
from app.db.tenant_scope import tenant_scoped_select
from app.governance.domain_policies import get_domain_policy
from app.governance.quotas import QuotaResource, ensure_quota
from app.monitoring.crypto import ChannelSecretCipher
from app.monitoring.enums import (
    ChangeType,
    DeliveryStatus,
    MonitorRunStatus,
    MonitorStatus,
    NotificationChannelKind,
    severity_rank,
)
from app.monitoring.errors import (
    ChannelSecretError,
    EmailNotConfiguredError,
    EmailSendError,
    NotificationSendError,
    NotificationTargetError,
)
from app.monitoring.service import (
    MonitoringServices,
    build_monitoring_services,
    get_alert_rules,
)
from app.services.audit import record_event

logger = get_logger(__name__)

_TERMINAL_RUN_STATUSES = {
    MonitorRunStatus.COMPLETED.value,
    MonitorRunStatus.FAILED.value,
    MonitorRunStatus.CANCELED.value,
}
_ACTIVE_TASK_STATUSES = {"pending", "running", "retry"}


def _now() -> datetime:
    return datetime.now(UTC)


def _delivery_key(channel_id: UUID, change_id: UUID) -> str:
    return hashlib.sha256(f"{channel_id}:{change_id}".encode()).hexdigest()


def _slack_escape(value: str) -> str:
    """Escape Slack control characters so untrusted titles cannot mention/ping."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class MonitoringWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        services: MonitoringServices,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._services = services
        self._session_factory = session_factory
        self._worker_id = f"monitor-{uuid4().hex[:12]}"

    async def run_forever(self) -> None:
        while True:
            try:
                scheduled, runs, deliveries = await self.run_once()
            except Exception:
                logger.exception("monitoring_worker_iteration_failed")
                scheduled = runs = deliveries = 0
            if scheduled == runs == deliveries == 0:
                await asyncio.sleep(self._settings.monitoring_poll_interval_seconds)

    async def run_once(
        self,
        *,
        tenant_ids: Sequence[UUID] | None = None,
    ) -> tuple[int, int, int]:
        async with self._session_factory() as session:
            tenant_statement = select(Tenant.id).where(Tenant.status == "active")
            if tenant_ids is not None:
                tenant_statement = tenant_statement.where(Tenant.id.in_(tenant_ids))
            active_tenant_ids = list((await session.scalars(tenant_statement)).all())

        scheduled = 0
        runs = 0
        deliveries = 0
        for tenant_id in active_tenant_ids:
            with tenant_context(tenant_id):
                scheduled += await self._schedule_due(tenant_id)
                runs += await self._process_runs(tenant_id)
                deliveries += await self._process_deliveries(tenant_id)
        return scheduled, runs, deliveries

    async def _schedule_due(self, tenant_id: UUID) -> int:
        now = _now()
        created = 0
        async with self._session_factory() as session, session.begin():
            subscriptions = list(
                (
                    await session.scalars(
                        tenant_scoped_select(MonitorSubscription)
                        .where(
                            MonitorSubscription.tenant_id == tenant_id,
                            MonitorSubscription.status == MonitorStatus.ACTIVE.value,
                            MonitorSubscription.next_run_at <= now,
                        )
                        .order_by(MonitorSubscription.next_run_at, MonitorSubscription.id)
                        .limit(20)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for subscription in subscriptions:
                active_run = await session.scalar(
                    select(MonitorRun.id).where(
                        MonitorRun.tenant_id == tenant_id,
                        MonitorRun.subscription_id == subscription.id,
                        MonitorRun.status.in_(
                            [MonitorRunStatus.PENDING.value, MonitorRunStatus.COLLECTING.value]
                        ),
                    )
                )
                if active_run is not None:
                    subscription.next_run_at = now + timedelta(
                        seconds=self._settings.monitoring_poll_interval_seconds
                    )
                    continue
                try:
                    await ensure_quota(
                        session,
                        tenant_id=tenant_id,
                        resource=QuotaResource.MONITOR_RUNS,
                        settings=self._settings,
                    )
                except QuotaExceededError:
                    subscription.next_run_at = now + timedelta(
                        seconds=self._settings.monitoring_poll_interval_seconds
                    )
                    continue
                session.add(
                    MonitorRun(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        subscription_id=subscription.id,
                        triggered_by="schedule",
                        status=MonitorRunStatus.PENDING.value,
                        attempts=0,
                        max_attempts=self._settings.monitoring_run_max_attempts,
                        next_attempt_at=now,
                    )
                )
                subscription.next_run_at = now + timedelta(minutes=subscription.cadence_minutes)
                subscription.last_run_at = now
                created += 1
            await session.flush()
        return created

    async def _process_runs(self, tenant_id: UUID) -> int:
        runs = await self._claim_runs(tenant_id)
        if not runs:
            return 0
        semaphore = asyncio.Semaphore(self._settings.monitoring_max_concurrent_runs)

        async def execute(run: MonitorRun) -> None:
            async with semaphore:
                await self._execute_run(run.id)

        await asyncio.gather(*(execute(run) for run in runs), return_exceptions=True)
        return len(runs)

    async def _claim_runs(self, tenant_id: UUID) -> list[MonitorRun]:
        now = _now()
        async with self._session_factory() as session, session.begin():
            statement = (
                select(MonitorRun)
                .where(
                    MonitorRun.tenant_id == tenant_id,
                    MonitorRun.status.in_(
                        [MonitorRunStatus.PENDING.value, MonitorRunStatus.COLLECTING.value]
                    ),
                    MonitorRun.next_attempt_at <= now,
                    or_(
                        MonitorRun.lease_expires_at.is_(None),
                        MonitorRun.lease_expires_at <= now,
                    ),
                )
                .order_by(MonitorRun.next_attempt_at, MonitorRun.id)
                .limit(self._settings.monitoring_max_concurrent_runs)
                .with_for_update(skip_locked=True)
            )
            claimed = list((await session.scalars(statement)).all())
            for run in claimed:
                if run.attempts >= run.max_attempts:
                    run.status = MonitorRunStatus.FAILED.value
                    run.error_code = "max_attempts_exceeded"
                    run.error_message = "The monitor run exceeded its retry budget."
                    run.lease_expires_at = None
                    run.worker_id = None
                    run.completed_at = now
                    continue
                run.worker_id = self._worker_id
                run.lease_expires_at = now + timedelta(
                    seconds=self._settings.monitoring_run_lease_seconds
                )
                if run.started_at is None:
                    run.started_at = now
            await session.flush()
        return [run for run in claimed if run.status != MonitorRunStatus.FAILED.value]

    async def _execute_run(self, run_id: UUID) -> None:
        try:
            async with self._session_factory() as session:
                run = await session.scalar(
                    tenant_scoped_select(MonitorRun).where(MonitorRun.id == run_id)
                )
                if run is None:
                    return
                subscription = await session.scalar(
                    tenant_scoped_select(MonitorSubscription).where(
                        MonitorSubscription.id == run.subscription_id
                    )
                )
                if subscription is None:
                    await self._fail_run(
                        run_id,
                        code="subscription_missing",
                        message="The monitoring subscription no longer exists.",
                        retryable=False,
                    )
                    return
                if (
                    subscription.status != MonitorStatus.ACTIVE.value
                    and run.status == MonitorRunStatus.PENDING.value
                ):
                    await self._fail_run(
                        run_id,
                        code="subscription_inactive",
                        message="The monitoring subscription is not active.",
                        retryable=False,
                    )
                    return
                task_ids = [UUID(value) for value in run.task_ids]
                if run.status == MonitorRunStatus.PENDING.value and not task_ids:
                    await session.close()
                    hits = await self._search(subscription)
                    if not hits:
                        await self._complete_without_changes(run_id)
                        return
                    principal = await self._resolve_principal(subscription)
                    if principal is None:
                        await self._fail_run(
                            run_id,
                            code="subscription_owner_missing",
                            message="The subscription owner is not authorized.",
                            retryable=False,
                        )
                        return
                    async with self._session_factory() as task_session, task_session.begin():
                        current = await task_session.scalar(
                            tenant_scoped_select(MonitorRun)
                            .where(MonitorRun.id == run_id)
                            .with_for_update()
                        )
                        if (
                            current is None
                            or current.worker_id != self._worker_id
                            or current.status != MonitorRunStatus.PENDING.value
                        ):
                            return
                        tasks = await enqueue_tasks(
                            task_session,
                            principal=principal,
                            workspace_id=subscription.workspace_id,
                            project_id=subscription.project_id,
                            urls=hits,
                            settings=self._settings,
                        )
                        current.task_ids = [str(task.id) for task in tasks]
                        current.status = MonitorRunStatus.COLLECTING.value
                        current.next_attempt_at = _now() + timedelta(
                            seconds=self._settings.monitoring_poll_interval_seconds
                        )
                        current.lease_expires_at = None
                        current.worker_id = None
                        await task_session.flush()
                    return

                if not task_ids:
                    await self._complete_without_changes(run_id)
                    return
                tasks = list(
                    (
                        await session.scalars(
                            tenant_scoped_select(CollectionTask).where(
                                CollectionTask.id.in_(task_ids)
                            )
                        )
                    ).all()
                )
                if any(task.status in _ACTIVE_TASK_STATUSES for task in tasks):
                    await self._reschedule_run(run_id)
                    return
                await self._complete_run(run_id)
        except QuotaExceededError:
            await self._fail_run(
                run_id,
                code="quota_exceeded",
                message="The tenant collection quota has been exceeded.",
                retryable=True,
            )
        except CollectorError as exc:
            await self._fail_run(
                run_id,
                code=exc.code,
                message=exc.safe_message,
                retryable=exc.retryable,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "monitor_run_failed",
                extra={"context": {"run_id": str(run_id), "error": type(exc).__name__}},
            )
            await self._fail_run(
                run_id,
                code="monitor_internal_error",
                message="The monitoring run failed.",
                retryable=True,
            )

    async def _search(self, subscription: MonitorSubscription) -> list[str]:
        if self._services.collector_services is None:
            raise CollectorError("search provider is unavailable")
        provider = require_search_provider(self._services.collector_services)
        hits = await provider.search(
            subscription.query,
            limit=self._settings.monitoring_max_sources_per_run,
        )
        async with self._services.session_factory() as session:
            policy = await get_domain_policy(
                session,
                tenant_id=subscription.tenant_id,
            )
        filtered = filter_search_hits(
            hits,
            settings=self._settings,
            limit=self._settings.monitoring_max_sources_per_run,
            policy=policy,
        )
        return [hit.url for hit in filtered]

    async def _resolve_principal(self, subscription: MonitorSubscription) -> Principal | None:
        if subscription.created_by is None:
            return None
        async with self._session_factory() as session:
            try:
                return await resolve_principal_for_user(
                    session,
                    tenant_id=subscription.tenant_id,
                    user_id=subscription.created_by,
                )
            except Exception:
                return None

    async def _reschedule_run(self, run_id: UUID) -> None:
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                tenant_scoped_select(MonitorRun).where(MonitorRun.id == run_id).with_for_update()
            )
            if (
                run is None
                or run.worker_id != self._worker_id
                or run.status
                not in {
                    MonitorRunStatus.PENDING.value,
                    MonitorRunStatus.COLLECTING.value,
                }
            ):
                return
            run.next_attempt_at = _now() + timedelta(
                seconds=self._settings.monitoring_poll_interval_seconds
            )
            run.lease_expires_at = None
            run.worker_id = None
            await session.flush()

    async def _complete_without_changes(self, run_id: UUID) -> None:
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                tenant_scoped_select(MonitorRun).where(MonitorRun.id == run_id).with_for_update()
            )
            if run is None or run.worker_id != self._worker_id:
                return
            if run.status in _TERMINAL_RUN_STATUSES:
                return
            run.status = MonitorRunStatus.COMPLETED.value
            run.change_count = 0
            run.completed_at = _now()
            run.lease_expires_at = None
            run.worker_id = None
            record_event(
                session,
                tenant_id=run.tenant_id,
                actor_user_id=None,
                action=AuditAction.MONITOR_RUN_COMPLETED,
                resource_type=ResourceType.MONITOR_RUN,
                resource_id=run.id,
                details={"changes": 0},
            )
            await session.flush()

    async def _complete_run(self, run_id: UUID) -> None:
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                tenant_scoped_select(MonitorRun).where(MonitorRun.id == run_id).with_for_update()
            )
            if run is None or run.worker_id != self._worker_id:
                return
            if run.status in _TERMINAL_RUN_STATUSES:
                return
            subscription = await session.scalar(
                tenant_scoped_select(MonitorSubscription).where(
                    MonitorSubscription.id == run.subscription_id
                )
            )
            if subscription is None:
                return
            task_ids = [UUID(value) for value in run.task_ids]
            tasks = list(
                (
                    await session.scalars(
                        tenant_scoped_select(CollectionTask).where(CollectionTask.id.in_(task_ids))
                    )
                ).all()
            )
            succeeded_ids = {
                task.source_id for task in tasks if task.status == "succeeded" and task.source_id
            }
            failed_tasks = [task for task in tasks if task.status == "failed"]
            if not succeeded_ids and failed_tasks:
                await self._fail_run_in_session(
                    session,
                    run=run,
                    code="all_collection_tasks_failed",
                    message="No monitored sources could be collected.",
                    retryable=False,
                )
                return
            changes: list[MonitorChange] = []
            for source_id in succeeded_ids:
                change = await self._detect_source_change(
                    session,
                    run=run,
                    subscription=subscription,
                    source_id=source_id,
                )
                if change is not None:
                    changes.append(change)
            await self._create_deliveries(
                session,
                subscription=subscription,
                changes=changes,
            )
            run.status = MonitorRunStatus.COMPLETED.value
            run.change_count = len(changes)
            run.error_code = "partial_failure" if failed_tasks else None
            run.error_message = "Some collection tasks failed." if failed_tasks else None
            run.completed_at = _now()
            run.lease_expires_at = None
            run.worker_id = None
            subscription.last_run_at = _now()
            record_event(
                session,
                tenant_id=run.tenant_id,
                actor_user_id=subscription.created_by,
                action=AuditAction.MONITOR_RUN_COMPLETED,
                resource_type=ResourceType.MONITOR_RUN,
                resource_id=run.id,
                details={
                    "subscription_id": str(subscription.id),
                    "changes": len(changes),
                    "failed_tasks": len(failed_tasks),
                },
            )
            await session.flush()

    async def _detect_source_change(
        self,
        session: AsyncSession,
        *,
        run: MonitorRun,
        subscription: MonitorSubscription,
        source_id: UUID,
    ) -> MonitorChange | None:
        source = await session.scalar(
            tenant_scoped_select(WebSource).where(
                WebSource.id == source_id,
                WebSource.status == "active",
            )
        )
        if source is None or source.latest_snapshot_id is None:
            return None
        snapshot = await session.scalar(
            tenant_scoped_select(SourceSnapshot).where(
                SourceSnapshot.id == source.latest_snapshot_id,
                SourceSnapshot.status == "active",
            )
        )
        if snapshot is None:
            return None
        seen = await session.scalar(
            tenant_scoped_select(MonitorSeenSource).where(
                MonitorSeenSource.subscription_id == subscription.id,
                MonitorSeenSource.source_id == source.id,
            )
        )
        change_type: ChangeType | None = None
        previous_snapshot_id: UUID | None = None
        severity = self._settings.monitoring_new_source_severity
        if seen is None:
            change_type = ChangeType.NEW_SOURCE
        elif seen.last_content_sha256 != snapshot.content_sha256:
            change_type = ChangeType.CONTENT_CHANGED
            previous_snapshot_id = seen.last_snapshot_id
            severity = self._settings.monitoring_change_severity
        if seen is None:
            session.add(
                MonitorSeenSource(
                    tenant_id=subscription.tenant_id,
                    subscription_id=subscription.id,
                    source_id=source.id,
                    last_snapshot_id=snapshot.id,
                    last_content_sha256=snapshot.content_sha256,
                    first_seen_at=_now(),
                    last_seen_at=_now(),
                )
            )
        else:
            seen.last_snapshot_id = snapshot.id
            seen.last_content_sha256 = snapshot.content_sha256
            seen.last_seen_at = _now()
        if change_type is None:
            return None
        existing = await session.scalar(
            select(MonitorChange.id).where(
                MonitorChange.tenant_id == subscription.tenant_id,
                MonitorChange.run_id == run.id,
                MonitorChange.source_id == source.id,
                MonitorChange.change_type == change_type.value,
            )
        )
        if existing is not None:
            return None
        summary = (
            f"New source discovered for {subscription.name}: {source.title or source.domain}"
            if change_type is ChangeType.NEW_SOURCE
            else f"Source content changed for {subscription.name}: {source.title or source.domain}"
        )[:1_000]
        change = MonitorChange(
            id=uuid4(),
            tenant_id=subscription.tenant_id,
            subscription_id=subscription.id,
            run_id=run.id,
            source_id=source.id,
            previous_snapshot_id=previous_snapshot_id,
            current_snapshot_id=snapshot.id,
            change_type=change_type.value,
            severity=severity,
            summary=summary,
            detected_at=_now(),
        )
        session.add(change)
        record_event(
            session,
            tenant_id=subscription.tenant_id,
            actor_user_id=subscription.created_by,
            action=AuditAction.MONITOR_CHANGE_DETECTED,
            resource_type=ResourceType.MONITOR_CHANGE,
            resource_id=change.id,
            details={
                "subscription_id": str(subscription.id),
                "source_id": str(source.id),
                "change_type": change_type.value,
                "severity": severity,
            },
        )
        await session.flush()
        return change

    async def _create_deliveries(
        self,
        session: AsyncSession,
        *,
        subscription: MonitorSubscription,
        changes: Sequence[MonitorChange],
    ) -> None:
        if not changes:
            return
        rules = await get_alert_rules(session, subscription_id=subscription.id)
        if not rules:
            return
        channels = {
            channel.id: channel
            for channel in (
                await session.scalars(
                    tenant_scoped_select(NotificationChannel).where(
                        NotificationChannel.workspace_id == subscription.workspace_id,
                        NotificationChannel.enabled.is_(True),
                    )
                )
            ).all()
        }
        for change in changes:
            for rule in rules:
                channel = channels.get(rule.channel_id)
                if channel is None:
                    continue
                if severity_rank(change.severity) < severity_rank(rule.min_severity):
                    continue
                delivery_id = uuid4()
                key = _delivery_key(channel.id, change.id)
                existing = await session.scalar(
                    select(NotificationDelivery.id).where(
                        NotificationDelivery.tenant_id == subscription.tenant_id,
                        NotificationDelivery.delivery_key == key,
                    )
                )
                if existing is not None:
                    continue
                try:
                    await ensure_quota(
                        session,
                        tenant_id=subscription.tenant_id,
                        resource=QuotaResource.NOTIFICATION_DELIVERIES,
                        settings=self._settings,
                    )
                except QuotaExceededError:
                    logger.warning(
                        "notification_quota_exceeded",
                        extra={"context": {"tenant_id": str(subscription.tenant_id)}},
                    )
                    return
                source = await session.scalar(
                    tenant_scoped_select(WebSource).where(WebSource.id == change.source_id)
                )
                payload = {
                    "delivery_id": str(delivery_id),
                    "tenant_id": str(subscription.tenant_id),
                    "subscription_id": str(subscription.id),
                    "subscription_name": subscription.name,
                    "change_id": str(change.id),
                    "change_type": change.change_type,
                    "severity": change.severity,
                    "summary": change.summary,
                    "source_id": str(change.source_id),
                    "source_title": source.title if source is not None else None,
                    "source_url": source.url if source is not None else None,
                    "untrusted": True,
                    "detected_at": change.detected_at.isoformat(),
                }
                session.add(
                    NotificationDelivery(
                        id=delivery_id,
                        tenant_id=subscription.tenant_id,
                        channel_id=channel.id,
                        subscription_id=subscription.id,
                        change_id=change.id,
                        delivery_key=key,
                        status=DeliveryStatus.PENDING.value,
                        attempts=0,
                        max_attempts=self._settings.notification_max_attempts,
                        next_attempt_at=_now(),
                        payload=payload,
                    )
                )
                await session.flush()
        await session.flush()

    async def _fail_run(
        self,
        run_id: UUID,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                tenant_scoped_select(MonitorRun).where(MonitorRun.id == run_id).with_for_update()
            )
            if run is None or run.worker_id != self._worker_id:
                return
            await self._fail_run_in_session(
                session,
                run=run,
                code=code,
                message=message,
                retryable=retryable,
            )

    async def _fail_run_in_session(
        self,
        session: AsyncSession,
        *,
        run: MonitorRun,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        run.attempts += 1
        run.error_code = code[:64]
        run.error_message = message[:500]
        run.lease_expires_at = None
        run.worker_id = None
        if retryable and run.attempts < run.max_attempts:
            run.next_attempt_at = _now() + timedelta(
                seconds=self._settings.monitoring_poll_interval_seconds
                * (2 ** max(0, run.attempts - 1))
            )
            await session.flush()
            return
        run.status = MonitorRunStatus.FAILED.value
        run.completed_at = _now()
        record_event(
            session,
            tenant_id=run.tenant_id,
            actor_user_id=None,
            action=AuditAction.MONITOR_RUN_FAILED,
            resource_type=ResourceType.MONITOR_RUN,
            resource_id=run.id,
            outcome=AuditOutcome.DENIED,
            details={"error_code": code[:64]},
        )
        await session.flush()

    async def _process_deliveries(self, tenant_id: UUID) -> int:
        deliveries = await self._claim_deliveries(tenant_id)
        if not deliveries:
            return 0
        semaphore = asyncio.Semaphore(self._settings.notification_max_concurrent_deliveries)

        async def execute(delivery: NotificationDelivery) -> None:
            async with semaphore:
                await self._execute_delivery(delivery.id)

        await asyncio.gather(
            *(execute(delivery) for delivery in deliveries),
            return_exceptions=True,
        )
        return len(deliveries)

    async def _claim_deliveries(self, tenant_id: UUID) -> list[NotificationDelivery]:
        now = _now()
        async with self._session_factory() as session, session.begin():
            statement = (
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.tenant_id == tenant_id,
                    NotificationDelivery.status.in_(
                        [DeliveryStatus.PENDING.value, DeliveryStatus.RUNNING.value]
                    ),
                    NotificationDelivery.next_attempt_at <= now,
                    or_(
                        NotificationDelivery.lease_expires_at.is_(None),
                        NotificationDelivery.lease_expires_at <= now,
                    ),
                )
                .order_by(NotificationDelivery.next_attempt_at, NotificationDelivery.id)
                .limit(self._settings.notification_max_concurrent_deliveries)
                .with_for_update(skip_locked=True)
            )
            claimed = list((await session.scalars(statement)).all())
            for delivery in claimed:
                if delivery.attempts >= delivery.max_attempts:
                    delivery.status = DeliveryStatus.FAILED.value
                    delivery.last_error = "max_attempts_exceeded"
                    delivery.completed_at = now
                    delivery.lease_expires_at = None
                    delivery.worker_id = None
                    continue
                delivery.status = DeliveryStatus.RUNNING.value
                delivery.attempts += 1
                delivery.worker_id = self._worker_id
                delivery.lease_expires_at = now + timedelta(
                    seconds=self._settings.notification_timeout_seconds + 10
                )
            await session.flush()
        return [d for d in claimed if d.status == DeliveryStatus.RUNNING.value]

    async def _execute_delivery(self, delivery_id: UUID) -> None:
        try:
            async with self._session_factory() as session:
                delivery = await session.scalar(
                    tenant_scoped_select(NotificationDelivery).where(
                        NotificationDelivery.id == delivery_id
                    )
                )
                if delivery is None:
                    return
                channel = await session.scalar(
                    tenant_scoped_select(NotificationChannel).where(
                        NotificationChannel.id == delivery.channel_id
                    )
                )
                if channel is None or not channel.enabled:
                    await self._finish_delivery(
                        delivery_id,
                        success=False,
                        code="channel_disabled",
                        retryable=False,
                    )
                    return
                config: dict[str, str] = {}
                if channel.kind in (
                    NotificationChannelKind.WEBHOOK.value,
                    NotificationChannelKind.SLACK.value,
                    NotificationChannelKind.EMAIL.value,
                ):
                    try:
                        config = ChannelSecretCipher(self._settings).decrypt(
                            channel.encrypted_config or ""
                        )
                    except ChannelSecretError:
                        await self._finish_delivery(
                            delivery_id,
                            success=False,
                            code="channel_secret_error",
                            retryable=False,
                        )
                        return
                payload = delivery.payload
                if channel.kind == NotificationChannelKind.SLACK.value:
                    text_parts = [
                        f"[{str(payload.get('severity', 'info')).upper()}] "
                        f"{_slack_escape(str(payload.get('summary', '')))}"
                    ]
                    if payload.get("source_url"):
                        text_parts.append(_slack_escape(str(payload["source_url"])))
                    body = json.dumps(
                        {"text": "\n".join(text_parts)[:3_000]},
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                else:
                    body = json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                if len(body) > self._settings.notification_max_payload_bytes:
                    await self._finish_delivery(
                        delivery_id,
                        success=False,
                        code="payload_too_large",
                        retryable=False,
                    )
                    return
                await session.close()
                if channel.kind == NotificationChannelKind.LOG.value:
                    logger.info(
                        "monitoring_notification",
                        extra={"context": {"delivery_id": str(delivery.id)}},
                    )
                    response_status = 200
                elif channel.kind == NotificationChannelKind.EMAIL.value:
                    raw_recipients = config.get("to")
                    recipients: list[str] = (
                        [str(value) for value in raw_recipients]
                        if isinstance(raw_recipients, list)
                        else []
                    )
                    if not recipients:
                        await self._finish_delivery(
                            delivery_id,
                            success=False,
                            code="email_recipients_missing",
                            retryable=False,
                        )
                        return
                    severity = str(payload.get("severity", "info")).upper()
                    summary = str(payload.get("summary", ""))
                    source_url = str(payload.get("source_url") or "")
                    email_body = (
                        f"{summary}\n\n"
                        f"Source: {source_url}\n\n"
                        "This notification contains untrusted external content. "
                        "Verify the source before acting."
                    )
                    try:
                        response_status = await self._services.email.send(
                            recipients=recipients,
                            subject=f"[MarketLens][{severity}] {summary[:120]}",
                            body=email_body,
                            delivery_id=str(delivery.id),
                        )
                    except EmailNotConfiguredError:
                        await self._finish_delivery(
                            delivery_id,
                            success=False,
                            code="email_not_configured",
                            retryable=False,
                        )
                        return
                    except EmailSendError:
                        await self._finish_delivery(
                            delivery_id,
                            success=False,
                            code="email_send_failed",
                            retryable=True,
                        )
                        return
                else:
                    headers = {
                        "X-MarketLens-Delivery": str(delivery.id),
                        "X-MarketLens-Idempotency-Key": str(delivery.id),
                    }
                    secret = config.get("signing_secret")
                    if secret:
                        signature = hmac.new(
                            secret.encode("utf-8"),
                            body,
                            hashlib.sha256,
                        ).hexdigest()
                        headers[self._settings.notification_signing_header] = f"sha256={signature}"
                    try:
                        result = await self._services.webhook.post_json(
                            url=config.get("url", ""),
                            body=body,
                            headers=headers,
                        )
                        response_status = result.status_code
                    except NotificationTargetError:
                        await self._finish_delivery(
                            delivery_id,
                            success=False,
                            code="target_rejected",
                            retryable=False,
                        )
                        return
                    except NotificationSendError:
                        await self._finish_delivery(
                            delivery_id,
                            success=False,
                            code="send_failed",
                            retryable=True,
                        )
                        return
                await self._finish_delivery(
                    delivery_id,
                    success=True,
                    response_status=response_status,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "notification_delivery_failed",
                extra={"context": {"delivery_id": str(delivery_id), "error": type(exc).__name__}},
            )
            await self._finish_delivery(
                delivery_id,
                success=False,
                code="internal_error",
                retryable=True,
            )

    async def _finish_delivery(
        self,
        delivery_id: UUID,
        *,
        success: bool,
        code: str = "",
        retryable: bool = False,
        response_status: int | None = None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            delivery = await session.scalar(
                tenant_scoped_select(NotificationDelivery)
                .where(NotificationDelivery.id == delivery_id)
                .with_for_update()
            )
            if (
                delivery is None
                or delivery.worker_id != self._worker_id
                or delivery.status != DeliveryStatus.RUNNING.value
            ):
                return
            delivery.lease_expires_at = None
            delivery.worker_id = None
            delivery.response_status = response_status
            if success:
                delivery.status = DeliveryStatus.SENT.value
                delivery.sent_at = _now()
                delivery.completed_at = _now()
                delivery.last_error = None
                record_event(
                    session,
                    tenant_id=delivery.tenant_id,
                    actor_user_id=None,
                    action=AuditAction.NOTIFICATION_SENT,
                    resource_type=ResourceType.NOTIFICATION_DELIVERY,
                    resource_id=delivery.id,
                    details={"channel_id": str(delivery.channel_id)},
                )
            else:
                delivery.last_error = code[:500]
                if retryable and delivery.attempts < delivery.max_attempts:
                    delivery.status = DeliveryStatus.PENDING.value
                    delay = min(
                        self._settings.notification_retry_base_seconds
                        * (2 ** max(0, delivery.attempts - 1)),
                        self._settings.notification_retry_max_seconds,
                    )
                    delivery.next_attempt_at = _now() + timedelta(seconds=delay)
                else:
                    delivery.status = DeliveryStatus.FAILED.value
                    delivery.completed_at = _now()
                    record_event(
                        session,
                        tenant_id=delivery.tenant_id,
                        actor_user_id=None,
                        action=AuditAction.NOTIFICATION_FAILED,
                        resource_type=ResourceType.NOTIFICATION_DELIVERY,
                        resource_id=delivery.id,
                        outcome=AuditOutcome.DENIED,
                        details={"error_code": code[:64]},
                    )
            await session.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MarketLens monitoring worker")
    parser.add_argument("--once", action="store_true", help="process one batch and exit")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)
    if not settings.monitoring_enabled:
        logger.error("monitoring is not enabled")
        return 1

    install_tenant_guard()
    install_rls_context()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    collector_services = (
        build_collector_services(settings, session_factory) if settings.collector_enabled else None
    )
    services = build_monitoring_services(
        settings,
        session_factory,
        collector_services=collector_services,
    )
    worker = MonitoringWorker(
        settings=settings,
        services=services,
        session_factory=session_factory,
    )

    async def run() -> None:
        try:
            if collector_services is not None:
                await collector_services.ensure_ready()
            if args.once:
                scheduled, runs, deliveries = await worker.run_once()
                logger.info(
                    "monitoring_batch_complete",
                    extra={
                        "context": {
                            "scheduled": scheduled,
                            "runs": runs,
                            "deliveries": deliveries,
                        }
                    },
                )
                return
            await worker.run_forever()
        finally:
            await services.aclose()
            if collector_services is not None:
                await collector_services.aclose()
            await engine.dispose()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("monitoring_worker_stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
