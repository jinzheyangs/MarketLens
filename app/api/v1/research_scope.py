"""Shared workspace/project/run authorization helpers for research APIs."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.service import get_run
from app.auth.authorization import AuthorizationService, ResourceContext
from app.auth.enums import Permission
from app.core.errors import NotFoundError
from app.db.models import Organization, Project, ResearchRun, Workspace
from app.services.tenancy import (
    get_project_bundle,
    get_workspace_bundle,
    project_context,
    workspace_context,
)


async def require_workspace(
    session: AsyncSession,
    workspace_id: UUID,
) -> tuple[Workspace, Organization]:
    bundle = await get_workspace_bundle(session, workspace_id)
    if bundle is None:
        raise NotFoundError("The workspace was not found.")
    return bundle


async def require_project(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    project_id: UUID,
) -> tuple[Project, Workspace, Organization]:
    bundle = await get_project_bundle(session, project_id)
    if bundle is None or bundle[1].id != workspace_id:
        raise NotFoundError("The project was not found.")
    return bundle


async def run_context(
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


async def require_run(
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
    await authz.require(permission, await run_context(session, run))
    return run


async def require_root_run(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    run_id: UUID,
    authz: AuthorizationService,
    permission: Permission,
    for_update: bool = False,
) -> ResearchRun:
    requested = await get_run(
        session,
        run_id=run_id,
        workspace_id=workspace_id,
        for_update=False,
    )
    if requested is None:
        raise NotFoundError("The research run was not found.")
    root_id = requested.root_run_id or requested.id
    root: ResearchRun | None = requested
    if root_id != requested.id:
        root = await get_run(
            session,
            run_id=root_id,
            workspace_id=workspace_id,
            for_update=for_update,
        )
        if root is None:
            raise NotFoundError("The research run was not found.")
    elif for_update:
        root = await get_run(
            session,
            run_id=requested.id,
            workspace_id=workspace_id,
            for_update=True,
        )
    if root is None:
        raise NotFoundError("The research run was not found.")
    await authz.require(permission, await run_context(session, root))
    return root


__all__ = [
    "require_project",
    "require_root_run",
    "require_run",
    "require_workspace",
    "run_context",
]
