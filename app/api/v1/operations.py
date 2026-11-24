"""Tenant-admin operational queue visibility."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.auth.authorization import AuthorizationService
from app.auth.dependencies import get_authorization, get_principal
from app.auth.enums import Permission
from app.auth.principal import Principal
from app.governance.operations import queue_snapshot
from app.schemas.governance import QueueSnapshotRead

router = APIRouter(prefix="/operations", tags=["operations"])


@router.get(
    "/queues",
    response_model=QueueSnapshotRead,
    summary="Get tenant queue and backlog depth",
)
async def get_queue_snapshot(
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> QueueSnapshotRead:
    await authz.require_tenant_admin(Permission.QUOTA_READ)
    snapshot = await queue_snapshot(session, tenant_id=principal.tenant_id)
    return QueueSnapshotRead(**asdict(snapshot))
