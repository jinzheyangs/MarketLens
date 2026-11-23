"""Authorized research run, findings, checkpoint, and trace APIs."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.enums import ResearchRunStatus
from app.agent.service import (
    AgentServices,
    cancel_run,
    create_run,
    get_run,
    list_checkpoints,
    list_findings,
    list_runs,
    list_tool_calls,
    pause_run,
    resume_run,
)
from app.api.deps import get_session
from app.auth.authorization import AuthorizationService, ResourceContext
from app.auth.dependencies import get_authorization, get_principal, get_settings_dep
from app.auth.enums import Permission
from app.auth.principal import Principal
from app.core.config import Settings
from app.core.errors import NotFoundError, ServiceUnavailableError
from app.db.models import Organization, Project, ResearchRun, Workspace
from app.schemas.research import (
    ResearchCheckpointRead,
    ResearchFindingRead,
    ResearchRunCreate,
    ResearchRunRead,
    ResearchToolCallRead,
)
from app.services.tenancy import (
    get_project_bundle,
    get_workspace_bundle,
    project_context,
    workspace_context,
)

router = APIRouter(tags=["research"])


def _agent_services(request: Request) -> AgentServices:
    services = getattr(request.app.state, "agent_services", None)
    if services is None:
        raise ServiceUnavailableError("Research agents are not configured.")
    return cast(AgentServices, services)


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


async def _run_context(
    session: AsyncSession,
    run: ResearchRun,
) -> ResourceContext:
    if run.project_id is not None:
        bundle = await get_project_bundle(session, run.project_id)
        if bundle is None or bundle[1].id != run.workspace_id:
            raise NotFoundError("The research scope was not found.")
        return project_context(*bundle)
    workspace_bundle = await get_workspace_bundle(session, run.workspace_id)
    if workspace_bundle is None:
        raise NotFoundError("The research scope was not found.")
    return workspace_context(*workspace_bundle)


async def _require_run(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    run_id: UUID,
    authz: AuthorizationService,
    permission: Permission,
    for_update: bool = False,
) -> ResearchRun:
    run = await get_run(
        session,
        run_id=run_id,
        workspace_id=workspace_id,
        for_update=for_update,
    )
    if run is None:
        raise NotFoundError("The research run was not found.")
    await authz.require(permission, await _run_context(session, run))
    return run


@router.post(
    "/workspaces/{workspace_id}/research-runs",
    response_model=ResearchRunRead,
    status_code=202,
    summary="Create a durable multi-agent research run",
)
async def create_research_run(
    workspace_id: UUID,
    payload: ResearchRunCreate,
    request: Request,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> ResearchRunRead:
    _agent_services(request)
    workspace, organization = await _require_workspace(session, workspace_id)
    if payload.project_id is not None:
        project, _, _ = await _require_project(
            session,
            workspace_id=workspace_id,
            project_id=payload.project_id,
        )
        context = project_context(project, workspace, organization)
    else:
        context = workspace_context(workspace, organization)
    await authz.require(Permission.RESEARCH_RUN_CREATE, context)
    run = await create_run(
        session,
        principal=principal,
        workspace_id=workspace.id,
        project_id=payload.project_id,
        objective=payload.objective,
        request_key=payload.request_key,
        settings=settings,
    )
    return ResearchRunRead.model_validate(run)


@router.get(
    "/workspaces/{workspace_id}/research-runs",
    response_model=list[ResearchRunRead],
    summary="List research runs",
)
async def list_research_runs(
    workspace_id: UUID,
    project_id: UUID | None = Query(default=None),
    status: ResearchRunStatus | None = Query(default=None),
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ResearchRunRead]:
    workspace, organization = await _require_workspace(session, workspace_id)
    if project_id is not None:
        project, _, _ = await _require_project(
            session,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        context = project_context(project, workspace, organization)
    else:
        context = workspace_context(workspace, organization)
    await authz.require(Permission.RESEARCH_RUN_READ, context)
    runs = await list_runs(
        session,
        workspace_id=workspace.id,
        project_id=project_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return [ResearchRunRead.model_validate(run) for run in runs]


@router.get(
    "/workspaces/{workspace_id}/research-runs/{run_id}",
    response_model=ResearchRunRead,
    summary="Get a research run",
)
async def get_research_run(
    workspace_id: UUID,
    run_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ResearchRunRead:
    run = await _require_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.RESEARCH_RUN_READ,
    )
    return ResearchRunRead.model_validate(run)


@router.post(
    "/workspaces/{workspace_id}/research-runs/{run_id}/pause",
    response_model=ResearchRunRead,
    summary="Pause a pending or running research run",
)
async def pause_research_run(
    workspace_id: UUID,
    run_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ResearchRunRead:
    run = await _require_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.RESEARCH_RUN_CONTROL,
        for_update=True,
    )
    run = await pause_run(session, run=run, principal=principal)
    return ResearchRunRead.model_validate(run)


@router.post(
    "/workspaces/{workspace_id}/research-runs/{run_id}/resume",
    response_model=ResearchRunRead,
    status_code=202,
    summary="Resume a paused research run from its persisted checkpoint",
)
async def resume_research_run(
    workspace_id: UUID,
    run_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ResearchRunRead:
    run = await _require_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.RESEARCH_RUN_CONTROL,
        for_update=True,
    )
    run = await resume_run(session, run=run, principal=principal)
    return ResearchRunRead.model_validate(run)


@router.post(
    "/workspaces/{workspace_id}/research-runs/{run_id}/cancel",
    response_model=ResearchRunRead,
    summary="Cancel a research run",
)
async def cancel_research_run(
    workspace_id: UUID,
    run_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ResearchRunRead:
    run = await _require_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.RESEARCH_RUN_CONTROL,
        for_update=True,
    )
    run = await cancel_run(session, run=run, principal=principal)
    return ResearchRunRead.model_validate(run)


@router.get(
    "/workspaces/{workspace_id}/research-runs/{run_id}/findings",
    response_model=list[ResearchFindingRead],
    summary="List evidence-bound research findings",
)
async def list_research_findings(
    workspace_id: UUID,
    run_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ResearchFindingRead]:
    await _require_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.RESEARCH_RUN_READ,
    )
    findings = await list_findings(session, run_id=run_id, limit=limit, offset=offset)
    return [ResearchFindingRead.model_validate(finding) for finding in findings]


@router.get(
    "/workspaces/{workspace_id}/research-runs/{run_id}/checkpoints",
    response_model=list[ResearchCheckpointRead],
    summary="List persisted research checkpoints for replay",
)
async def list_research_checkpoints(
    workspace_id: UUID,
    run_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ResearchCheckpointRead]:
    await _require_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.RESEARCH_RUN_READ,
    )
    checkpoints = await list_checkpoints(session, run_id=run_id, limit=limit, offset=offset)
    return [ResearchCheckpointRead.model_validate(checkpoint) for checkpoint in checkpoints]


@router.get(
    "/workspaces/{workspace_id}/research-runs/{run_id}/tool-calls",
    response_model=list[ResearchToolCallRead],
    summary="List read-only tool calls made by research agents",
)
async def list_research_tool_calls(
    workspace_id: UUID,
    run_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ResearchToolCallRead]:
    await _require_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.RESEARCH_RUN_READ,
    )
    calls = await list_tool_calls(session, run_id=run_id, limit=limit, offset=offset)
    return [ResearchToolCallRead.model_validate(call) for call in calls]
