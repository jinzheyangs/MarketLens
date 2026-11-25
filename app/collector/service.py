"""采集服务装配与租户范围的任务/来源操作。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.enums import AuditAction, AuditOutcome, ResourceType
from app.auth.principal import Principal
from app.collector.enums import CollectionTaskStatus, SourceSnapshotStatus, SourceStatus
from app.collector.errors import BlockedTargetError, InvalidTargetError, SearchProviderError
from app.collector.extraction import BasicContentExtractor, ContentExtractor
from app.collector.network import RobotsManager, SafeFetcher, validate_url
from app.collector.rate_limit import DomainRateLimiter
from app.collector.search import SearchHit, SearchProvider, build_search_provider
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ServiceUnavailableError
from app.db.models import CollectionTask, SourceSnapshot, WebSource
from app.db.tenant_scope import tenant_scoped_select
from app.governance.domain_policies import DomainPolicy, get_domain_policy
from app.governance.quotas import QuotaResource, ensure_quota
from app.rag.errors import ObjectStorageError
from app.rag.storage import ObjectStorage, build_object_storage
from app.schemas.health import ComponentHealth
from app.services.audit import record_event

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CollectorServices:
    settings: Settings
    storage: ObjectStorage
    fetcher: SafeFetcher
    robots: RobotsManager
    extractor: ContentExtractor
    rate_limiter: DomainRateLimiter
    search_provider: SearchProvider | None
    session_factory: async_sessionmaker[AsyncSession]

    async def ensure_ready(self) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 11):
            try:
                await self.storage.ensure_ready()
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "collector_storage_not_ready",
                    extra={
                        "context": {
                            "attempt": attempt,
                            "error": type(exc).__name__,
                        }
                    },
                )
                if attempt < 10:
                    await asyncio.sleep(1)
        if last_error is not None:
            raise last_error

    async def health(self) -> dict[str, ComponentHealth]:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.settings.health_check_timeout_seconds):
                await self.storage.health()
            return {
                "object_storage": ComponentHealth(
                    status="ok",
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            }
        except Exception as exc:
            logger.warning(
                "collector_storage_unhealthy",
                extra={"context": {"error": type(exc).__name__}},
            )
            return {
                "object_storage": ComponentHealth(
                    status="error",
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            }

    async def aclose(self) -> None:
        if self.search_provider is not None:
            try:
                await self.search_provider.aclose()
            except Exception:
                logger.exception("search_provider_close_failed")
        try:
            await self.storage.aclose()
        except Exception:
            logger.exception("collector_storage_close_failed")


def build_collector_services(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    storage: ObjectStorage | None = None,
) -> CollectorServices:
    fetcher = SafeFetcher(settings)
    return CollectorServices(
        settings=settings,
        storage=storage or build_object_storage(settings),
        fetcher=fetcher,
        robots=RobotsManager(settings, fetcher),
        extractor=BasicContentExtractor(settings),
        rate_limiter=DomainRateLimiter(),
        search_provider=build_search_provider(settings),
        session_factory=session_factory,
    )


def _active_key(workspace_id: UUID, url_hash: str) -> str:
    return hashlib.sha256(f"{workspace_id}:{url_hash}".encode()).hexdigest()


async def enqueue_tasks(
    session: AsyncSession,
    *,
    principal: Principal,
    workspace_id: UUID,
    project_id: UUID | None,
    urls: list[str],
    settings: Settings,
) -> list[CollectionTask]:
    if not urls:
        raise InvalidTargetError("no URLs supplied")
    if len(urls) > settings.collector_max_tasks_per_request:
        raise InvalidTargetError("too many URLs in one request")

    safe_urls = []
    seen: set[str] = set()
    for raw_url in urls:
        safe = validate_url(raw_url, settings)
        if safe.url not in seen:
            seen.add(safe.url)
            safe_urls.append(safe)

    policy = await get_domain_policy(
        session,
        tenant_id=principal.tenant_id,
    )
    for safe in safe_urls:
        if not policy.allows(safe.host):
            raise BlockedTargetError("The target domain is blocked by tenant policy.")

    await ensure_quota(
        session,
        tenant_id=principal.tenant_id,
        resource=QuotaResource.COLLECTION_TASKS,
        settings=settings,
        increment=len(safe_urls),
    )
    tasks: list[CollectionTask] = []
    now = datetime.now(UTC)
    for safe in safe_urls:
        active_key = _active_key(workspace_id, safe.url_hash)
        existing = cast(
            CollectionTask | None,
            await session.scalar(
                tenant_scoped_select(CollectionTask).where(CollectionTask.active_key == active_key)
            ),
        )
        if existing is not None:
            tasks.append(existing)
            continue

        task = CollectionTask(
            tenant_id=principal.tenant_id,
            workspace_id=workspace_id,
            project_id=project_id,
            requested_by=principal.user_id,
            url=safe.url,
            url_hash=safe.url_hash,
            domain=safe.host,
            status=CollectionTaskStatus.PENDING.value,
            attempts=0,
            max_attempts=settings.collector_max_attempts,
            next_attempt_at=now,
            active_key=active_key,
        )
        try:
            async with session.begin_nested():
                session.add(task)
                await session.flush()
        except IntegrityError:
            existing = cast(
                CollectionTask | None,
                await session.scalar(
                    tenant_scoped_select(CollectionTask).where(
                        CollectionTask.active_key == active_key
                    )
                ),
            )
            if existing is None:
                raise
            tasks.append(existing)
            continue

        record_event(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            action=AuditAction.COLLECTION_TASK_QUEUED,
            resource_type=ResourceType.COLLECTION_TASK,
            resource_id=task.id,
            details={"domain": task.domain},
        )
        tasks.append(task)

    return tasks


async def list_tasks(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    project_id: UUID | None,
    status: CollectionTaskStatus | None,
    limit: int,
    offset: int,
) -> list[CollectionTask]:
    statement = tenant_scoped_select(CollectionTask).where(
        CollectionTask.workspace_id == workspace_id
    )
    if project_id is not None:
        statement = statement.where(CollectionTask.project_id == project_id)
    if status is not None:
        statement = statement.where(CollectionTask.status == status)
    statement = (
        statement.order_by(CollectionTask.created_at.desc(), CollectionTask.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.scalars(statement)).all())


async def get_task(
    session: AsyncSession,
    *,
    task_id: UUID,
    workspace_id: UUID,
) -> CollectionTask | None:
    return cast(
        CollectionTask | None,
        await session.scalar(
            tenant_scoped_select(CollectionTask).where(
                CollectionTask.id == task_id,
                CollectionTask.workspace_id == workspace_id,
            )
        ),
    )


async def retry_task(
    session: AsyncSession,
    *,
    task: CollectionTask,
    principal: Principal,
) -> CollectionTask:
    if task.status not in {
        CollectionTaskStatus.FAILED.value,
        CollectionTaskStatus.CANCELED.value,
    }:
        raise ConflictError("Only failed or canceled collection tasks can be retried.")

    active_key = task.active_key or _active_key(task.workspace_id, task.url_hash)
    existing = cast(
        CollectionTask | None,
        await session.scalar(
            tenant_scoped_select(CollectionTask).where(
                CollectionTask.active_key == active_key,
                CollectionTask.id != task.id,
            )
        ),
    )
    if existing is not None:
        raise ConflictError("Another active task already exists for this URL.")

    task.status = CollectionTaskStatus.PENDING.value
    task.attempts = 0
    task.next_attempt_at = datetime.now(UTC)
    task.lease_expires_at = None
    task.last_error_code = None
    task.last_error_message = None
    task.active_key = active_key
    await session.flush()
    await session.refresh(task)
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.COLLECTION_TASK_RETRIED,
        resource_type=ResourceType.COLLECTION_TASK,
        resource_id=task.id,
        details={"domain": task.domain},
    )
    return task


async def list_sources(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    domain: str | None,
    limit: int,
    offset: int,
) -> list[WebSource]:
    statement = tenant_scoped_select(WebSource).where(
        WebSource.workspace_id == workspace_id,
        WebSource.status == SourceStatus.ACTIVE.value,
    )
    if domain:
        statement = statement.where(WebSource.domain == domain.lower().rstrip("."))
    statement = (
        statement.order_by(
            WebSource.last_fetched_at.desc().nulls_last(),
            WebSource.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return list((await session.scalars(statement)).all())


async def get_source(
    session: AsyncSession,
    *,
    source_id: UUID,
    workspace_id: UUID,
    active_only: bool = True,
) -> WebSource | None:
    statement = tenant_scoped_select(WebSource).where(
        WebSource.id == source_id,
        WebSource.workspace_id == workspace_id,
    )
    if active_only:
        statement = statement.where(WebSource.status == SourceStatus.ACTIVE.value)
    return cast(WebSource | None, await session.scalar(statement))


async def list_snapshots(
    session: AsyncSession,
    *,
    source_id: UUID,
    limit: int,
    offset: int,
) -> list[SourceSnapshot]:
    statement = (
        tenant_scoped_select(SourceSnapshot)
        .where(
            SourceSnapshot.source_id == source_id,
            SourceSnapshot.status == SourceSnapshotStatus.ACTIVE.value,
        )
        .order_by(SourceSnapshot.fetched_at.desc(), SourceSnapshot.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.scalars(statement)).all())


def filter_search_hits(
    hits: list[SearchHit],
    *,
    settings: Settings,
    limit: int,
    policy: DomainPolicy | None = None,
) -> list[SearchHit]:
    filtered: list[SearchHit] = []
    seen: set[str] = set()
    for hit in hits:
        try:
            safe = validate_url(hit.url, settings)
        except (InvalidTargetError, BlockedTargetError):
            continue
        if policy is not None and not policy.allows(safe.host):
            continue
        if safe.url in seen:
            continue
        seen.add(safe.url)
        filtered.append(SearchHit(url=safe.url, title=hit.title, snippet=hit.snippet))
        if len(filtered) >= limit:
            break
    return filtered


async def delete_source(
    services: CollectorServices,
    *,
    principal: Principal,
    workspace_id: UUID,
    source_id: UUID,
) -> None:
    tenant_id = principal.tenant_id
    async with services.session_factory() as session, session.begin():
        source = cast(
            WebSource | None,
            await session.scalar(
                tenant_scoped_select(WebSource)
                .where(
                    WebSource.id == source_id,
                    WebSource.workspace_id == workspace_id,
                )
                .with_for_update()
            ),
        )
        if source is None:
            raise NotFoundError("The source was not found.")
        if source.status == SourceStatus.DELETED.value:
            return
        source.status = SourceStatus.DELETING.value
        object_keys = list(
            (
                await session.scalars(
                    select(SourceSnapshot.object_key).where(
                        SourceSnapshot.tenant_id == tenant_id,
                        SourceSnapshot.source_id == source_id,
                    )
                )
            ).all()
        )

    try:
        for object_key in object_keys:
            await services.storage.delete(object_key)
    except ObjectStorageError as exc:
        await _mark_source_delete_failed(
            services,
            tenant_id=tenant_id,
            source_id=source_id,
            principal=principal,
            error=exc,
        )
        raise ServiceUnavailableError("Source deletion could not be completed.") from exc

    try:
        async with services.session_factory() as session, session.begin():
            source = cast(
                WebSource | None,
                await session.scalar(
                    tenant_scoped_select(WebSource)
                    .where(WebSource.id == source_id)
                    .with_for_update()
                ),
            )
            if source is None:
                return
            await session.execute(
                delete(SourceSnapshot).where(
                    SourceSnapshot.tenant_id == tenant_id,
                    SourceSnapshot.source_id == source_id,
                )
            )
            source.status = SourceStatus.DELETED.value
            source.latest_snapshot_id = None
            record_event(
                session,
                tenant_id=tenant_id,
                actor_user_id=principal.user_id,
                action=AuditAction.SOURCE_DELETED,
                resource_type=ResourceType.SOURCE,
                resource_id=source_id,
                details={"snapshots_deleted": len(object_keys)},
            )
            await session.flush()
    except Exception as exc:
        await _mark_source_delete_failed(
            services,
            tenant_id=tenant_id,
            source_id=source_id,
            principal=principal,
            error=exc,
        )
        raise ServiceUnavailableError("Source deletion could not be completed.") from exc


async def _mark_source_delete_failed(
    services: CollectorServices,
    *,
    tenant_id: UUID,
    source_id: UUID,
    principal: Principal,
    error: Exception,
) -> None:
    logger.error(
        "source_delete_failed",
        extra={
            "context": {
                "tenant_id": str(tenant_id),
                "source_id": str(source_id),
                "error": type(error).__name__,
            }
        },
        exc_info=error,
    )
    try:
        async with services.session_factory() as session, session.begin():
            source = cast(
                WebSource | None,
                await session.scalar(
                    tenant_scoped_select(WebSource)
                    .where(WebSource.id == source_id)
                    .with_for_update()
                ),
            )
            if source is None or source.status != SourceStatus.DELETING.value:
                return
            source.status = SourceStatus.DELETE_FAILED.value
            record_event(
                session,
                tenant_id=tenant_id,
                actor_user_id=principal.user_id,
                action=AuditAction.SOURCE_DELETE_FAILED,
                resource_type=ResourceType.SOURCE,
                resource_id=source_id,
                outcome=AuditOutcome.DENIED,
                details={"error": type(error).__name__},
            )
            await session.flush()
    except Exception:
        logger.exception("source_delete_failure_state_write_failed")


def require_search_provider(services: CollectorServices) -> SearchProvider:
    if services.search_provider is None:
        raise SearchProviderError("web search is not configured")
    return services.search_provider


__all__ = [
    "CollectorServices",
    "build_collector_services",
    "delete_source",
    "enqueue_tasks",
    "filter_search_hits",
    "get_source",
    "get_task",
    "list_snapshots",
    "list_sources",
    "list_tasks",
    "require_search_provider",
    "retry_task",
]
