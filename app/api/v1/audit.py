"""Audit event read API (tenant administrators only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.auth.authorization import AuthorizationService
from app.auth.dependencies import get_authorization
from app.auth.enums import Permission
from app.schemas.audit import AuditEventRead
from app.services.audit import list_events

router = APIRouter(prefix="/audit-events", tags=["audit"])


@router.get("", response_model=list[AuditEventRead], summary="List audit events")
async def list_audit_events(
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[AuditEventRead]:
    await authz.require_tenant_admin(Permission.AUDIT_READ)
    events = await list_events(session, limit=limit, offset=offset)
    return [AuditEventRead.model_validate(event) for event in events]
