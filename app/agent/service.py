"""调研运行的持久化、面向授权层的操作与服务装配。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.enums import (
    RESEARCH_GRAPH_VERSION,
    TERMINAL_RUN_STATUSES,
    ResearchPhase,
    ResearchRunStatus,
    StopReason,
)
from app.agent.providers import ResearchModel, build_model_provider
from app.agent.state import budget_for_settings
from app.auth.enums import AuditAction, ResourceType
from app.auth.principal import Principal
from app.collector.service import CollectorServices
from app.core.config import Settings
from app.core.errors import AppError, ConflictError, ErrorCode, NotFoundError
from app.db.models import (
    ResearchCheckpoint,
    ResearchFinding,
    ResearchRun,
    ResearchToolCall,
)
from app.db.tenant_scope import tenant_scoped_select
from app.governance.quotas import QuotaResource, ensure_quota
from app.services.audit import record_event
from app.services.rag import RagServices

_REQUEST_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


@dataclass(slots=True)
class AgentServices:
    """Agent 领域服务集合：配置、模型供应商、会话工厂与依赖的外部服务。"""
    settings: Settings
    model: ResearchModel
    session_factory: async_sessionmaker[AsyncSession]
    rag_services: RagServices | None = None
    collector_services: CollectorServices | None = None

    async def aclose(self) -> None:
        await self.model.aclose()


def build_agent_services(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    rag_services: RagServices | None = None,
    collector_services: CollectorServices | None = None,
) -> AgentServices:
    """装配 Agent 服务集合。"""
    return AgentServices(
        settings=settings,
        model=build_model_provider(settings),
        session_factory=session_factory,
        rag_services=rag_services,
        collector_services=collector_services,
    )


def validate_request_key(value: str | None) -> str | None:
    """校验幂等键格式（安全字符集、长度）；空值返回 None。"""
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if not _REQUEST_KEY_PATTERN.fullmatch(candidate):
        raise ValueError("request_key must be 8-128 safe characters")
    return candidate


async def create_run(
    session: AsyncSession,
    *,
    principal: Principal,
    workspace_id: UUID,
    project_id: UUID | None,
    objective: str,
    request_key: str | None,
    settings: Settings,
) -> ResearchRun:
    """创建调研运行：校验目标/幂等键、检查配额、落库并写审计。"""
    normalized_objective = objective.strip()
    if not normalized_objective:
        raise AppError(
            code=ErrorCode.BAD_REQUEST,
            message="The research objective must not be blank.",
            status_code=400,
        )
    if len(normalized_objective) > settings.agent_max_objective_chars:
        raise AppError(
            code=ErrorCode.BAD_REQUEST,
            message="The research objective exceeds the configured length limit.",
            status_code=400,
        )
    try:
        key = validate_request_key(request_key)
    except ValueError as exc:
        raise AppError(
            code=ErrorCode.BAD_REQUEST,
            message="The idempotency key is invalid.",
            status_code=400,
        ) from exc

    if key is not None:
        existing = cast(
            ResearchRun | None,
            await session.scalar(
                tenant_scoped_select(ResearchRun).where(ResearchRun.request_key == key)
            ),
        )
        if existing is not None:
            if (
                existing.workspace_id != workspace_id
                or existing.project_id != project_id
                or existing.objective != normalized_objective
            ):
                raise ConflictError("The idempotency key is already used for another run.")
            return existing

    await ensure_quota(
        session,
        tenant_id=principal.tenant_id,
        resource=QuotaResource.RESEARCH_RUNS,
        settings=settings,
    )
    budget = budget_for_settings(settings)
    run = ResearchRun(
        id=uuid4(),
        tenant_id=principal.tenant_id,
        workspace_id=workspace_id,
        project_id=project_id,
        run_kind="primary",
        followup_round=0,
        requested_by=principal.user_id,
        request_key=key,
        objective=normalized_objective,
        model_provider=settings.agent_provider,
        model_name=settings.agent_model,
        graph_version=RESEARCH_GRAPH_VERSION,
        status=ResearchRunStatus.PENDING.value,
        max_rounds=settings.agent_max_rounds,
        budget=budget,
        usage={},
        plan=[],
        next_attempt_at=datetime.now(UTC),
        attempts=0,
        max_attempts=settings.agent_max_run_attempts,
    )
    try:
        async with session.begin_nested():
            session.add(run)
            await session.flush()
    except IntegrityError:
        if key is None:
            raise
        existing = cast(
            ResearchRun | None,
            await session.scalar(
                tenant_scoped_select(ResearchRun).where(ResearchRun.request_key == key)
            ),
        )
        if existing is None:
            raise
        if (
            existing.workspace_id != workspace_id
            or existing.project_id != project_id
            or existing.objective != normalized_objective
        ):
            raise ConflictError("The idempotency key is already used for another run.") from None
        return existing

    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.RESEARCH_RUN_CREATED,
        resource_type=ResourceType.RESEARCH_RUN,
        resource_id=run.id,
        details={
            "workspace_id": str(workspace_id),
            "project_id": str(project_id) if project_id else None,
            "max_rounds": run.max_rounds,
        },
    )
    await session.flush()
    await session.refresh(run)
    return run


async def get_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    workspace_id: UUID,
    for_update: bool = False,
) -> ResearchRun | None:
    """按 ID + 工作区范围查询运行记录（可加行锁）。"""
    statement = tenant_scoped_select(ResearchRun).where(
        ResearchRun.id == run_id,
        ResearchRun.workspace_id == workspace_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return cast(ResearchRun | None, await session.scalar(statement))


async def require_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    workspace_id: UUID,
) -> ResearchRun:
    """查询运行记录，不存在则抛 404。"""
    run = await get_run(session, run_id=run_id, workspace_id=workspace_id)
    if run is None:
        raise NotFoundError("The research run was not found.")
    return run


async def list_runs(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    project_id: UUID | None,
    status: ResearchRunStatus | None,
    limit: int,
    offset: int,
) -> list[ResearchRun]:
    """按工作区/项目/状态分页列出调研运行。"""
    statement = tenant_scoped_select(ResearchRun).where(ResearchRun.workspace_id == workspace_id)
    if project_id is not None:
        statement = statement.where(ResearchRun.project_id == project_id)
    if status is not None:
        statement = statement.where(ResearchRun.status == status.value)
    statement = (
        statement.order_by(ResearchRun.created_at.desc(), ResearchRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.scalars(statement)).all())


async def list_findings(
    session: AsyncSession,
    *,
    run_id: UUID,
    limit: int,
    offset: int,
) -> list[ResearchFinding]:
    """分页列出某次运行的发现结论。"""
    statement = (
        tenant_scoped_select(ResearchFinding)
        .where(ResearchFinding.run_id == run_id)
        .order_by(ResearchFinding.created_at, ResearchFinding.id)
        .limit(limit)
        .offset(offset)
    )
    return list((await session.scalars(statement)).all())


async def list_checkpoints(
    session: AsyncSession,
    *,
    run_id: UUID,
    limit: int,
    offset: int,
) -> list[ResearchCheckpoint]:
    """按序号分页列出某次运行的检查点。"""
    statement = (
        tenant_scoped_select(ResearchCheckpoint)
        .where(ResearchCheckpoint.run_id == run_id)
        .order_by(ResearchCheckpoint.sequence)
        .limit(limit)
        .offset(offset)
    )
    return list((await session.scalars(statement)).all())


async def list_tool_calls(
    session: AsyncSession,
    *,
    run_id: UUID,
    limit: int,
    offset: int,
) -> list[ResearchToolCall]:
    """分页列出某次运行的工具调用轨迹。"""
    statement = (
        tenant_scoped_select(ResearchToolCall)
        .where(ResearchToolCall.run_id == run_id)
        .order_by(ResearchToolCall.created_at, ResearchToolCall.id)
        .limit(limit)
        .offset(offset)
    )
    return list((await session.scalars(statement)).all())


def _audit_control(
    session: AsyncSession,
    *,
    run: ResearchRun,
    principal: Principal,
    action: AuditAction,
    details: dict[str, object] | None = None,
) -> None:
    """写入一条调研运行相关的审计事件。"""
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=action,
        resource_type=ResourceType.RESEARCH_RUN,
        resource_id=run.id,
        details=details or {},
    )


async def pause_run(
    session: AsyncSession,
    *,
    run: ResearchRun,
    principal: Principal,
) -> ResearchRun:
    """请求暂停运行；运行中则置暂停标志，否则直接置为暂停状态。"""
    if run.status in {status.value for status in TERMINAL_RUN_STATUSES}:
        raise ConflictError("A terminal research run cannot be paused.")
    if run.status == ResearchRunStatus.PAUSED.value:
        return run
    if run.status == ResearchRunStatus.RUNNING.value:
        if run.pause_requested:
            return run
        run.pause_requested = True
        _audit_control(
            session,
            run=run,
            principal=principal,
            action=AuditAction.RESEARCH_RUN_PAUSED,
            details={"requested": True},
        )
    else:
        run.status = ResearchRunStatus.PAUSED.value
        run.phase = ResearchPhase.PAUSED.value
        run.pause_requested = False
        run.lease_expires_at = None
        run.worker_id = None
        _audit_control(
            session,
            run=run,
            principal=principal,
            action=AuditAction.RESEARCH_RUN_PAUSED,
            details={"requested": False},
        )
    await session.flush()
    await session.refresh(run)
    return run


async def resume_run(
    session: AsyncSession,
    *,
    run: ResearchRun,
    principal: Principal,
) -> ResearchRun:
    """恢复暂停的运行：重置控制标志与错误状态，回到待执行。"""
    if run.status != ResearchRunStatus.PAUSED.value:
        raise ConflictError("Only a paused research run can be resumed.")
    run.status = ResearchRunStatus.PENDING.value
    run.phase = ResearchPhase.INITIALIZED.value
    run.stop_reason = None
    run.error_code = None
    run.error_message = None
    run.pause_requested = False
    run.cancel_requested = False
    run.lease_expires_at = None
    run.worker_id = None
    run.completed_at = None
    run.next_attempt_at = datetime.now(UTC)
    run.attempts = 0
    _audit_control(
        session,
        run=run,
        principal=principal,
        action=AuditAction.RESEARCH_RUN_RESUMED,
    )
    await session.flush()
    await session.refresh(run)
    return run


async def cancel_run(
    session: AsyncSession,
    *,
    run: ResearchRun,
    principal: Principal,
) -> ResearchRun:
    """请求取消运行；运行中则置取消标志，否则直接置为已取消。"""
    if run.status == ResearchRunStatus.CANCELED.value:
        return run
    if run.status in {status.value for status in TERMINAL_RUN_STATUSES}:
        raise ConflictError("A terminal research run cannot be canceled.")
    if run.status == ResearchRunStatus.RUNNING.value:
        if run.cancel_requested:
            return run
        run.cancel_requested = True
        _audit_control(
            session,
            run=run,
            principal=principal,
            action=AuditAction.RESEARCH_RUN_CANCELED,
            details={"requested": True},
        )
    else:
        run.status = ResearchRunStatus.CANCELED.value
        run.phase = ResearchPhase.CANCELED.value
        run.stop_reason = StopReason.CANCELED.value
        run.cancel_requested = False
        run.pause_requested = False
        run.lease_expires_at = None
        run.worker_id = None
        run.completed_at = datetime.now(UTC)
        _audit_control(
            session,
            run=run,
            principal=principal,
            action=AuditAction.RESEARCH_RUN_CANCELED,
            details={"requested": False},
        )
    await session.flush()
    await session.refresh(run)
    return run


def run_usage(run: ResearchRun) -> dict[str, int]:
    """提取运行的用量汇总（token、成本、工具调用数）。"""
    usage = run.usage if isinstance(run.usage, dict) else {}
    return {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "cost_micros": int(usage.get("cost_micros", 0)),
        "tool_calls": int(usage.get("tool_calls", 0)),
    }


__all__ = [
    "AgentServices",
    "build_agent_services",
    "cancel_run",
    "create_run",
    "get_run",
    "list_checkpoints",
    "list_findings",
    "list_runs",
    "list_tool_calls",
    "pause_run",
    "require_run",
    "resume_run",
    "run_usage",
    "validate_request_key",
]
