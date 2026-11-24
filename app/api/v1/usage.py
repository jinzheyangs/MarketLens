"""Tenant cost attribution and usage reporting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.auth.authorization import AuthorizationService
from app.auth.dependencies import get_authorization, get_principal
from app.auth.enums import Permission
from app.auth.principal import Principal
from app.core.errors import AppError, ErrorCode
from app.governance.costs import cost_summary
from app.schemas.governance import CostBucketRead, CostSummaryRead

router = APIRouter(prefix="/usage", tags=["usage"])

_MAX_RANGE = timedelta(days=366)


@router.get(
    "/costs",
    response_model=CostSummaryRead,
    summary="Get tenant token and model cost attribution",
)
async def get_costs(
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
) -> CostSummaryRead:
    await authz.require_tenant_admin(Permission.QUOTA_READ)
    now = datetime.now(UTC)
    end = until or now
    start = since or (end - timedelta(days=30))
    if start.tzinfo is None or end.tzinfo is None:
        raise AppError(
            code=ErrorCode.BAD_REQUEST,
            message="since and until must include a timezone.",
            status_code=400,
        )
    if end <= start:
        raise AppError(
            code=ErrorCode.BAD_REQUEST,
            message="until must be after since.",
            status_code=400,
        )
    if end - start > _MAX_RANGE:
        raise AppError(
            code=ErrorCode.BAD_REQUEST,
            message="The requested cost range is too large.",
            status_code=400,
        )
    summary = await cost_summary(
        session,
        tenant_id=principal.tenant_id,
        since=start,
        until=end,
    )
    return CostSummaryRead(
        since=summary.since,
        until=summary.until,
        runs=summary.runs,
        input_tokens=summary.input_tokens,
        output_tokens=summary.output_tokens,
        cost_micros=summary.cost_micros,
        tool_calls=summary.tool_calls,
        buckets=[
            CostBucketRead(
                workspace_id=bucket.workspace_id,
                project_id=bucket.project_id,
                model_provider=bucket.model_provider,
                model_name=bucket.model_name,
                runs=bucket.runs,
                input_tokens=bucket.input_tokens,
                output_tokens=bucket.output_tokens,
                cost_micros=bucket.cost_micros,
                tool_calls=bucket.tool_calls,
            )
            for bucket in summary.buckets
        ],
    )
