"""External search, collection tasks, and source snapshot APIs."""

from __future__ import annotations

from typing import NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.auth.authorization import AuthorizationService
from app.auth.dependencies import get_authorization, get_principal, get_settings_dep
from app.auth.enums import Permission
from app.auth.principal import Principal
from app.collector.enums import CollectionTaskStatus
from app.collector.errors import (
    BlockedTargetError,
    InvalidTargetError,
    SearchProviderError,
)
from app.collector.service import (
    CollectorServices,
    delete_source,
    enqueue_tasks,
    filter_search_hits,
    get_source,
    get_task,
    list_snapshots,
    list_sources,
    list_tasks,
    require_search_provider,
    retry_task,
)
from app.core.config import Settings
from app.core.errors import (
    AppError,
    ErrorCode,
    NotFoundError,
    ServiceUnavailableError,
)
from app.db.models import CollectionTask, Organization, Project, Workspace
from app.schemas.collector import (
    CollectionTaskCreate,
    CollectionTaskRead,
    SourceDetail,
    SourceRead,
    SourceSnapshotRead,
    WebSearchHit,
    WebSearchRequest,
    WebSearchResponse,
)
from app.services.tenancy import (
    get_project_bundle,
    get_workspace_bundle,
    workspace_context,
)

router = APIRouter(tags=["collector"])


def _collector_services(request: Request) -> CollectorServices:
    services = getattr(request.app.state, "collector_services", None)
    if services is None:
        raise ServiceUnavailableError("Web collection is not configured.")
    return cast(CollectorServices, services)


def _map_collector_error(exc: Exception) -> NoReturn:
    if isinstance(exc, InvalidTargetError):
        raise AppError(
            code=ErrorCode.BAD_REQUEST,
            message=exc.safe_message,
            status_code=400,
        )
    if isinstance(exc, BlockedTargetError):
        raise AppError(
            code=ErrorCode.FORBIDDEN,
            message=exc.safe_message,
            status_code=403,
        )
    if isinstance(exc, SearchProviderError):
        raise ServiceUnavailableError(exc.safe_message)
    raise AppError(
        code=ErrorCode.INTERNAL_ERROR,
        message="The collection operation failed.",
        status_code=500,
    )


async def _require_workspace(
    session: AsyncSession,
    workspace_id: UUID,
) -> tuple[Workspace, Organization]:
    bundle = await get_workspace_bundle(session, workspace_id)
    if bundle is None:
        raise NotFoundError("The workspace was not found.")
    return bundle


async def _require_project(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    project_id: UUID,
) -> tuple[Project, Workspace, Organization]:
    bundle = await get_project_bundle(session, project_id)
    if bundle is None or bundle[1].id != workspace_id:
        raise NotFoundError("The project was not found.")
    return bundle


def _task_read(task: CollectionTask) -> CollectionTaskRead:
    return CollectionTaskRead.model_validate(task)


@router.post(
    "/workspaces/{workspace_id}/web-search",
    response_model=WebSearchResponse,
    summary="Search the configured external provider (results are untrusted)",
)
async def web_search(
    workspace_id: UUID,
    payload: WebSearchRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> WebSearchResponse:
    workspace, organization = await _require_workspace(session, workspace_id)
    await authz.require(Permission.WORKSPACE_UPDATE, workspace_context(workspace, organization))
    services = _collector_services(request)
    try:
        provider = require_search_provider(services)
        hits = await provider.search(
            payload.query,
            limit=min(payload.limit, settings.web_search_max_results),
        )
    except SearchProviderError as exc:
        _map_collector_error(exc)
    filtered = filter_search_hits(
        hits,
        settings=settings,
        limit=min(payload.limit, settings.web_search_max_results),
    )
    return WebSearchResponse(
        provider=settings.web_search_provider,
        results=[WebSearchHit.model_validate(hit) for hit in filtered],
    )


@router.post(
    "/workspaces/{workspace_id}/collection-tasks",
    response_model=list[CollectionTaskRead],
    status_code=202,
    summary="Enqueue external URL collection tasks",
)
async def enqueue_collection_tasks(
    workspace_id: UUID,
    payload: CollectionTaskCreate,
    request: Request,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> list[CollectionTaskRead]:
    workspace, organization = await _require_workspace(session, workspace_id)
    await authz.require(Permission.WORKSPACE_UPDATE, workspace_context(workspace, organization))
    if payload.project_id is not None:
        await _require_project(session, workspace_id=workspace_id, project_id=payload.project_id)
    _collector_services(request)
    try:
        tasks = await enqueue_tasks(
            session,
            principal=principal,
            workspace_id=workspace.id,
            project_id=payload.project_id,
            urls=payload.urls,
            settings=settings,
        )
    except (InvalidTargetError, BlockedTargetError) as exc:
        _map_collector_error(exc)
    return [_task_read(task) for task in tasks]


@router.get(
    "/workspaces/{workspace_id}/collection-tasks",
    response_model=list[CollectionTaskRead],
    summary="List collection tasks",
)
async def list_collection_tasks(
    workspace_id: UUID,
    project_id: UUID | None = Query(default=None),
    status: CollectionTaskStatus | None = Query(default=None),
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[CollectionTaskRead]:
    workspace, organization = await _require_workspace(session, workspace_id)
    await authz.require(
        Permission.WORKSPACE_CONTENT_READ,
        workspace_context(workspace, organization),
    )
    if project_id is not None:
        await _require_project(session, workspace_id=workspace_id, project_id=project_id)
    tasks = await list_tasks(
        session,
        workspace_id=workspace.id,
        project_id=project_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return [_task_read(task) for task in tasks]


@router.get(
    "/workspaces/{workspace_id}/collection-tasks/{task_id}",
    response_model=CollectionTaskRead,
    summary="Get a collection task",
)
async def get_collection_task(
    workspace_id: UUID,
    task_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> CollectionTaskRead:
    workspace, organization = await _require_workspace(session, workspace_id)
    await authz.require(
        Permission.WORKSPACE_CONTENT_READ,
        workspace_context(workspace, organization),
    )
    task = await get_task(session, task_id=task_id, workspace_id=workspace.id)
    if task is None:
        raise NotFoundError("The collection task was not found.")
    return _task_read(task)


@router.post(
    "/workspaces/{workspace_id}/collection-tasks/{task_id}/retry",
    response_model=CollectionTaskRead,
    summary="Retry a failed collection task",
)
async def retry_collection_task(
    workspace_id: UUID,
    task_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> CollectionTaskRead:
    workspace, organization = await _require_workspace(session, workspace_id)
    await authz.require(Permission.WORKSPACE_UPDATE, workspace_context(workspace, organization))
    task = await get_task(session, task_id=task_id, workspace_id=workspace.id)
    if task is None:
        raise NotFoundError("The collection task was not found.")
    task = await retry_task(session, task=task, principal=principal)
    return _task_read(task)


@router.get(
    "/workspaces/{workspace_id}/sources",
    response_model=list[SourceRead],
    summary="List collected sources",
)
async def list_workspace_sources(
    workspace_id: UUID,
    domain: str | None = Query(default=None, max_length=255),
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[SourceRead]:
    workspace, organization = await _require_workspace(session, workspace_id)
    await authz.require(
        Permission.WORKSPACE_CONTENT_READ,
        workspace_context(workspace, organization),
    )
    sources = await list_sources(
        session,
        workspace_id=workspace.id,
        domain=domain,
        limit=limit,
        offset=offset,
    )
    return [SourceRead.model_validate(source) for source in sources]


@router.get(
    "/workspaces/{workspace_id}/sources/{source_id}",
    response_model=SourceDetail,
    summary="Get a collected source and its snapshots",
)
async def get_workspace_source(
    workspace_id: UUID,
    source_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> SourceDetail:
    workspace, organization = await _require_workspace(session, workspace_id)
    await authz.require(
        Permission.WORKSPACE_CONTENT_READ,
        workspace_context(workspace, organization),
    )
    source = await get_source(session, source_id=source_id, workspace_id=workspace.id)
    if source is None:
        raise NotFoundError("The source was not found.")
    snapshots = await list_snapshots(session, source_id=source.id, limit=20, offset=0)
    latest_text = None
    for snapshot in snapshots:
        if snapshot.id == source.latest_snapshot_id:
            latest_text = snapshot.extracted_text
            break
    base = SourceRead.model_validate(source)
    return SourceDetail(
        **base.model_dump(),
        latest_text=latest_text,
        snapshots=[SourceSnapshotRead.model_validate(snapshot) for snapshot in snapshots],
    )


@router.delete(
    "/workspaces/{workspace_id}/sources/{source_id}",
    status_code=204,
    summary="Delete a source and its snapshots",
)
async def delete_workspace_source(
    workspace_id: UUID,
    source_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> Response:
    workspace, organization = await _require_workspace(session, workspace_id)
    await authz.require(
        Permission.WORKSPACE_MANAGE_STATUS,
        workspace_context(workspace, organization),
    )
    services = _collector_services(request)
    source = await get_source(
        session,
        source_id=source_id,
        workspace_id=workspace.id,
        active_only=False,
    )
    if source is None:
        raise NotFoundError("The source was not found.")
    try:
        await delete_source(
            services,
            principal=principal,
            workspace_id=workspace.id,
            source_id=source.id,
        )
    except ServiceUnavailableError:
        raise
    return Response(status_code=204)
