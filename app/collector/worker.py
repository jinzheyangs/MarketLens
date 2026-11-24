"""Isolated collection worker with DB-backed leases, retries, and rate limits.

Run as a separate process (``python -m app.collector.worker``), not in the API
process. The worker iterates active tenants and binds tenant context before any
tenant-scoped query so RLS remains enforced without a bypass role.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.enums import AuditAction, AuditOutcome, ResourceType
from app.collector.enums import (
    CollectionTaskStatus,
    SourceSnapshotStatus,
    SourceStatus,
)
from app.collector.errors import CollectorError, RobotsDisallowedError
from app.collector.extraction import ExtractedContent
from app.collector.network import FetchResult, SafeUrl, validate_url
from app.collector.service import CollectorServices, build_collector_services
from app.core.config import Settings, get_settings
from app.core.context import tenant_context
from app.core.errors import ServiceUnavailableError
from app.core.logging import configure_logging, get_logger
from app.db.models import CollectionTask, SourceSnapshot, Tenant, WebSource
from app.db.rls import install_rls_context
from app.db.session import create_engine, create_session_factory
from app.db.tenant_guard import install_tenant_guard
from app.db.tenant_scope import tenant_scoped_select
from app.rag.errors import ObjectStorageError
from app.services.audit import record_event

logger = get_logger(__name__)


class CollectorWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        services: CollectorServices,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._services = services
        self._session_factory = session_factory

    async def run_forever(self) -> None:
        while True:
            try:
                processed = await self.run_once()
            except Exception:
                logger.exception("collector_worker_iteration_failed")
                processed = 0
            if processed == 0:
                await asyncio.sleep(self._settings.collector_poll_interval_seconds)

    async def run_once(self, *, tenant_ids: Sequence[UUID] | None = None) -> int:
        tasks = await self._claim_tasks(
            self._settings.collector_max_concurrent_tasks,
            tenant_ids=tenant_ids,
        )
        if not tasks:
            return 0
        semaphore = asyncio.Semaphore(self._settings.collector_max_concurrent_tasks)

        async def run(task: CollectionTask) -> None:
            async with semaphore:
                with tenant_context(task.tenant_id):
                    await self._process_task(task)

        await asyncio.gather(*(run(task) for task in tasks), return_exceptions=True)
        return len(tasks)

    async def _claim_tasks(
        self,
        limit: int,
        *,
        tenant_ids: Sequence[UUID] | None = None,
    ) -> list[CollectionTask]:
        async with self._session_factory() as session:
            tenant_statement = select(Tenant.id).where(Tenant.status == "active")
            if tenant_ids is not None:
                tenant_statement = tenant_statement.where(Tenant.id.in_(tenant_ids))
            active_tenant_ids = list((await session.scalars(tenant_statement)).all())

        claimed: list[CollectionTask] = []
        now = datetime.now(UTC)
        for tenant_id in active_tenant_ids:
            remaining = limit - len(claimed)
            if remaining <= 0:
                break
            with tenant_context(tenant_id):
                async with self._session_factory() as session, session.begin():
                    statement = (
                        select(CollectionTask)
                        .where(
                            CollectionTask.tenant_id == tenant_id,
                            or_(
                                and_(
                                    CollectionTask.status.in_(
                                        [
                                            CollectionTaskStatus.PENDING.value,
                                            CollectionTaskStatus.RETRY.value,
                                        ]
                                    ),
                                    CollectionTask.next_attempt_at <= now,
                                ),
                                and_(
                                    CollectionTask.status == CollectionTaskStatus.RUNNING.value,
                                    or_(
                                        CollectionTask.lease_expires_at.is_(None),
                                        CollectionTask.lease_expires_at <= now,
                                    ),
                                ),
                            ),
                        )
                        .order_by(
                            CollectionTask.next_attempt_at,
                            CollectionTask.id,
                        )
                        .limit(remaining)
                        .with_for_update(skip_locked=True)
                    )
                    for task in (await session.scalars(statement)).all():
                        if task.attempts >= task.max_attempts:
                            task.status = CollectionTaskStatus.FAILED.value
                            task.active_key = None
                            task.lease_expires_at = None
                            task.last_error_code = "max_attempts_exceeded"
                            task.last_error_message = "The task exceeded its retry budget."
                            record_event(
                                session,
                                tenant_id=tenant_id,
                                actor_user_id=task.requested_by,
                                action=AuditAction.COLLECTION_TASK_FAILED,
                                resource_type=ResourceType.COLLECTION_TASK,
                                resource_id=task.id,
                                outcome=AuditOutcome.DENIED,
                                details={"error_code": "max_attempts_exceeded"},
                            )
                            continue

                        task.status = CollectionTaskStatus.RUNNING.value
                        task.attempts += 1
                        task.lease_expires_at = now + timedelta(
                            seconds=self._settings.collector_task_lease_seconds
                        )
                        claimed.append(task)
        return claimed

    async def _process_task(self, task: CollectionTask) -> None:
        try:
            safe = validate_url(task.url, self._settings)
            robots_policy = await self._services.robots.policy_for(safe)
            interval = max(
                self._settings.collector_domain_min_interval_seconds,
                min(robots_policy.crawl_delay or 0.0, 60.0),
            )
            async with self._session_factory() as session, session.begin():
                slot = await self._services.rate_limiter.reserve(
                    session,
                    tenant_id=task.tenant_id,
                    domain=task.domain,
                    interval_seconds=interval,
                )
            if slot > datetime.now(UTC) + timedelta(milliseconds=50):
                await self._reschedule_rate_limited(task, slot)
                return

            path = safe.path or "/"
            if safe.query:
                path = f"{path}?{safe.query}"
            if not robots_policy.allows(path):
                raise RobotsDisallowedError("robots policy disallows this path")

            fetch_result = await self._services.fetcher.fetch(task.url)
            extracted = await asyncio.to_thread(
                self._services.extractor.extract,
                fetch_result.body,
                content_type=fetch_result.content_type,
                charset=fetch_result.charset,
            )
            await self._store_result(task, safe, fetch_result, extracted)
        except CollectorError as exc:
            await self._handle_failure(
                task,
                code=exc.code,
                message=exc.safe_message,
                retryable=exc.retryable,
            )
        except ObjectStorageError:
            await self._handle_failure(
                task,
                code="object_storage_failed",
                message="Snapshot storage is unavailable.",
                retryable=True,
            )
        except Exception as exc:
            logger.exception(
                "collection_task_unexpected_error",
                extra={
                    "context": {
                        "task_id": str(task.id),
                        "tenant_id": str(task.tenant_id),
                        "error": type(exc).__name__,
                    }
                },
            )
            await self._handle_failure(
                task,
                code="internal_error",
                message="The collection task failed.",
                retryable=True,
            )

    async def _reschedule_rate_limited(
        self,
        task: CollectionTask,
        slot: datetime,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            current = await session.scalar(
                tenant_scoped_select(CollectionTask)
                .where(CollectionTask.id == task.id)
                .with_for_update()
            )
            if current is None or current.status != CollectionTaskStatus.RUNNING.value:
                return
            current.status = CollectionTaskStatus.RETRY.value
            current.next_attempt_at = slot
            current.lease_expires_at = None
            current.attempts = max(0, current.attempts - 1)
            await session.flush()

    async def _handle_failure(
        self,
        task: CollectionTask,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            current = await session.scalar(
                tenant_scoped_select(CollectionTask)
                .where(CollectionTask.id == task.id)
                .with_for_update()
            )
            if current is None or current.status != CollectionTaskStatus.RUNNING.value:
                return
            current.last_error_code = code[:64]
            current.last_error_message = message[:500]
            current.lease_expires_at = None
            if retryable and current.attempts < current.max_attempts:
                delay = min(
                    self._settings.collector_retry_base_seconds
                    * (2 ** max(0, current.attempts - 1)),
                    self._settings.collector_retry_max_seconds,
                )
                current.status = CollectionTaskStatus.RETRY.value
                current.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
                await session.flush()
                return

            current.status = CollectionTaskStatus.FAILED.value
            current.active_key = None
            record_event(
                session,
                tenant_id=task.tenant_id,
                actor_user_id=task.requested_by,
                action=AuditAction.COLLECTION_TASK_FAILED,
                resource_type=ResourceType.COLLECTION_TASK,
                resource_id=task.id,
                outcome=AuditOutcome.DENIED,
                details={"error_code": code[:64], "domain": task.domain},
            )
            await session.flush()

    async def _store_result(
        self,
        task: CollectionTask,
        requested_safe: SafeUrl,
        fetch_result: FetchResult,
        extracted: ExtractedContent,
    ) -> None:
        del requested_safe
        final_safe = validate_url(fetch_result.final_url, self._settings)
        now = datetime.now(UTC)
        snapshot_id = uuid4()
        object_key = (
            f"tenants/{task.tenant_id}/workspaces/{task.workspace_id}/sources/{snapshot_id}"
        )
        title = (extracted.title or final_safe.host)[:500]

        async with self._session_factory() as session:
            source = await session.scalar(
                tenant_scoped_select(WebSource).where(
                    WebSource.workspace_id == task.workspace_id,
                    WebSource.url_hash == final_safe.url_hash,
                )
            )
            if (
                source is not None
                and source.status == SourceStatus.ACTIVE.value
                and source.content_sha256 == fetch_result.content_sha256
                and source.latest_snapshot_id is not None
            ):
                await self._mark_success_same_content(
                    task=task,
                    source=source,
                    title=title,
                )
                return

        try:
            await self._services.storage.put(
                object_key,
                fetch_result.body,
                fetch_result.content_type,
            )
        except ObjectStorageError:
            raise

        try:
            async with self._session_factory() as session, session.begin():
                current_task = await session.scalar(
                    tenant_scoped_select(CollectionTask)
                    .where(CollectionTask.id == task.id)
                    .with_for_update()
                )
                if (
                    current_task is None
                    or current_task.status != CollectionTaskStatus.RUNNING.value
                ):
                    await self._delete_object_quietly(object_key)
                    return

                source = await session.scalar(
                    tenant_scoped_select(WebSource)
                    .where(
                        WebSource.workspace_id == task.workspace_id,
                        WebSource.url_hash == final_safe.url_hash,
                    )
                    .with_for_update()
                )
                created_source = source is None
                if source is None:
                    source = WebSource(
                        id=uuid4(),
                        tenant_id=task.tenant_id,
                        workspace_id=task.workspace_id,
                        url=final_safe.url,
                        url_hash=final_safe.url_hash,
                        domain=final_safe.host,
                        title=title,
                        status=SourceStatus.ACTIVE.value,
                        created_by=task.requested_by,
                    )
                    session.add(source)
                    try:
                        await session.flush()
                    except IntegrityError as exc:
                        raise ServiceUnavailableError(
                            "Source deduplication conflict; retry the task."
                        ) from exc

                existing_snapshot = await session.scalar(
                    tenant_scoped_select(SourceSnapshot).where(
                        SourceSnapshot.source_id == source.id,
                        SourceSnapshot.content_sha256 == fetch_result.content_sha256,
                        SourceSnapshot.status == SourceSnapshotStatus.ACTIVE.value,
                    )
                )
                if existing_snapshot is None:
                    snapshot = SourceSnapshot(
                        id=snapshot_id,
                        tenant_id=task.tenant_id,
                        workspace_id=task.workspace_id,
                        source_id=source.id,
                        status=SourceSnapshotStatus.ACTIVE.value,
                        object_key=object_key,
                        content_type=fetch_result.content_type,
                        size_bytes=len(fetch_result.body),
                        content_sha256=fetch_result.content_sha256,
                        final_url=final_safe.url,
                        http_status=fetch_result.status_code,
                        extracted_text=extracted.text,
                        extracted_char_count=len(extracted.text),
                        fetched_at=now,
                    )
                    session.add(snapshot)
                    latest_snapshot_id = snapshot.id
                else:
                    latest_snapshot_id = existing_snapshot.id
                    await self._delete_object_quietly(object_key)

                source.url = final_safe.url
                source.url_hash = final_safe.url_hash
                source.domain = final_safe.host
                source.title = title
                source.status = SourceStatus.ACTIVE.value
                source.content_sha256 = fetch_result.content_sha256
                source.latest_snapshot_id = latest_snapshot_id
                source.last_fetched_at = now

                current_task.status = CollectionTaskStatus.SUCCEEDED.value
                current_task.source_id = source.id
                current_task.active_key = None
                current_task.lease_expires_at = None
                current_task.last_error_code = None
                current_task.last_error_message = None

                record_event(
                    session,
                    tenant_id=task.tenant_id,
                    actor_user_id=task.requested_by,
                    action=(
                        AuditAction.SOURCE_CREATED if created_source else AuditAction.SOURCE_UPDATED
                    ),
                    resource_type=ResourceType.SOURCE,
                    resource_id=source.id,
                    details={
                        "domain": source.domain,
                        "snapshot_id": str(latest_snapshot_id),
                        "content_sha256": fetch_result.content_sha256,
                    },
                )
                record_event(
                    session,
                    tenant_id=task.tenant_id,
                    actor_user_id=task.requested_by,
                    action=AuditAction.COLLECTION_TASK_SUCCEEDED,
                    resource_type=ResourceType.COLLECTION_TASK,
                    resource_id=task.id,
                    details={"source_id": str(source.id), "domain": source.domain},
                )
                await session.flush()
        except Exception:
            await self._delete_object_quietly(object_key)
            raise

    async def _mark_success_same_content(
        self,
        *,
        task: CollectionTask,
        source: WebSource,
        title: str,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            current_task = await session.scalar(
                tenant_scoped_select(CollectionTask)
                .where(CollectionTask.id == task.id)
                .with_for_update()
            )
            if current_task is None or current_task.status != CollectionTaskStatus.RUNNING.value:
                return
            current_source = await session.scalar(
                tenant_scoped_select(WebSource).where(WebSource.id == source.id).with_for_update()
            )
            if current_source is None:
                raise ServiceUnavailableError("Source disappeared during collection.")
            current_source.last_fetched_at = datetime.now(UTC)
            current_source.title = title
            current_task.status = CollectionTaskStatus.SUCCEEDED.value
            current_task.source_id = current_source.id
            current_task.active_key = None
            current_task.lease_expires_at = None
            current_task.last_error_code = None
            current_task.last_error_message = None
            record_event(
                session,
                tenant_id=task.tenant_id,
                actor_user_id=task.requested_by,
                action=AuditAction.COLLECTION_TASK_SUCCEEDED,
                resource_type=ResourceType.COLLECTION_TASK,
                resource_id=task.id,
                details={"source_id": str(current_source.id), "deduplicated": True},
            )
            await session.flush()

    async def _delete_object_quietly(self, object_key: str) -> None:
        try:
            await self._services.storage.delete(object_key)
        except Exception:
            logger.exception(
                "collector_snapshot_cleanup_failed",
                extra={"context": {"object_key": object_key}},
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MarketScope collection worker")
    parser.add_argument("--once", action="store_true", help="process one batch and exit")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)
    if not settings.collector_enabled:
        logger.error("collector is not enabled")
        return 1

    install_tenant_guard()
    install_rls_context()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    services = build_collector_services(settings, session_factory)
    worker = CollectorWorker(
        settings=settings,
        services=services,
        session_factory=session_factory,
    )

    async def run() -> None:
        try:
            await services.ensure_ready()
            if args.once:
                processed = await worker.run_once()
                logger.info(
                    "collector_batch_complete",
                    extra={"context": {"processed": processed}},
                )
                return
            await worker.run_forever()
        finally:
            await services.aclose()
            await engine.dispose()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("collector_worker_stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
