"""Durable research runner: DB leases, LangGraph streaming, and checkpoints.

LangGraph owns in-graph control flow. This module persists the full JSON state
after each superstep (checkpoint), so pause/resume and crash recovery do not
depend on in-process memory. Native LangGraph checkpoint tables are intentionally
not used because they would bypass the tenant/RLS model.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.enums import (
    RESEARCH_GRAPH_VERSION,
    TERMINAL_RUN_STATUSES,
    ResearchPhase,
    ResearchRunStatus,
    StopReason,
    TaskStatus,
)
from app.agent.errors import AgentError, RunLeaseLostError
from app.agent.graph import build_research_graph
from app.agent.service import AgentServices
from app.agent.state import (
    ResearchState,
    initial_state,
    latest_task_results,
    unique_findings,
    unresolved_errors,
    usage_totals,
)
from app.agent.tools import ResearchToolbox
from app.auth.authorization import AuthorizationService
from app.auth.enums import (
    AuditAction,
    AuditOutcome,
    Permission,
    ResourceType,
    ScopeRole,
)
from app.auth.principal import Principal, resolve_principal_for_user
from app.core.context import tenant_context
from app.core.errors import ForbiddenError
from app.db.models import (
    ResearchCheckpoint,
    ResearchFinding,
    ResearchRun,
    Tenant,
)
from app.db.tenant_scope import tenant_scoped_select
from app.rag.access import build_document_access_scope
from app.rag.types import DocumentAccessScope
from app.services.audit import AuditWriter, record_event
from app.services.tenancy import (
    get_project_bundle,
    get_workspace_bundle,
    project_context,
    workspace_context,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunControl:
    pause_requested: bool
    cancel_requested: bool
    status: str


CapabilitySnapshot = tuple[bool, bool, bool, tuple[str, ...] | None, str | None]

_EDITOR_ROLES = frozenset({ScopeRole.EDITOR, ScopeRole.ADMIN})


@dataclass(frozen=True, slots=True)
class PreparedRun:
    run: ResearchRun
    principal: Principal
    document_scope: DocumentAccessScope | None
    organization_id: UUID
    workspace_id: UUID
    capability_snapshot: CapabilitySnapshot


def _safe_state_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, AgentError):
        return exc.code, exc.safe_message
    return "internal_error", "The research run failed."


def _iso_now() -> datetime:
    return datetime.now(UTC)


def _capability_snapshot(
    principal: Principal,
    *,
    organization_id: UUID,
    workspace_id: UUID,
    document_scope: DocumentAccessScope | None,
) -> CapabilitySnapshot:
    org_role = principal.organization_roles.get(organization_id)
    workspace_role = principal.workspace_roles.get(workspace_id)
    workspace_access = (
        principal.is_tenant_admin or org_role is not None or workspace_role is not None
    )
    workspace_mutation_access = (
        principal.is_tenant_admin
        or (org_role is not None and org_role in _EDITOR_ROLES)
        or (workspace_role is not None and workspace_role in _EDITOR_ROLES)
    )
    if document_scope is None:
        return (workspace_access, workspace_mutation_access, False, None, None)
    allowed = (
        tuple(sorted(str(project_id) for project_id in document_scope.allowed_project_ids))
        if document_scope.allowed_project_ids is not None
        else None
    )
    return (
        workspace_access,
        workspace_mutation_access,
        document_scope.include_workspace_documents,
        allowed,
        (
            str(document_scope.selected_project_id)
            if document_scope.selected_project_id is not None
            else None
        ),
    )


class ResearchRunner:
    def __init__(
        self,
        *,
        services: AgentServices,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._services = services
        self._settings = services.settings
        self._session_factory = session_factory
        self._worker_id = f"{uuid4().hex[:12]}"

    async def run_forever(self) -> None:
        while True:
            try:
                processed = await self.run_once()
            except Exception:
                logger.exception("research_worker_iteration_failed")
                processed = 0
            if processed == 0:
                await asyncio.sleep(self._settings.agent_poll_interval_seconds)

    async def run_once(self, *, tenant_ids: Sequence[UUID] | None = None) -> int:
        runs = await self._claim_runs(
            self._settings.agent_max_concurrent_runs,
            tenant_ids=tenant_ids,
        )
        if not runs:
            return 0
        semaphore = asyncio.Semaphore(self._settings.agent_max_concurrent_runs)

        async def execute(claimed_run: ResearchRun) -> None:
            async with semaphore:
                with tenant_context(claimed_run.tenant_id):
                    await self._execute_run(claimed_run.id)

        results = await asyncio.gather(
            *(execute(claimed_run) for claimed_run in runs),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.error(
                    "research_run_unhandled_failure",
                    extra={"context": {"error": type(result).__name__}},
                )
        return len(runs)

    async def _claim_runs(
        self,
        limit: int,
        *,
        tenant_ids: Sequence[UUID] | None = None,
    ) -> list[ResearchRun]:
        async with self._session_factory() as session:
            tenant_statement = select(Tenant.id).where(Tenant.status == "active")
            if tenant_ids is not None:
                tenant_statement = tenant_statement.where(Tenant.id.in_(tenant_ids))
            active_tenant_ids = list((await session.scalars(tenant_statement)).all())

        claimed: list[ResearchRun] = []
        now = _iso_now()
        for tenant_id in active_tenant_ids:
            remaining = limit - len(claimed)
            if remaining <= 0:
                break
            with tenant_context(tenant_id):
                async with self._session_factory() as session, session.begin():
                    statement = (
                        select(ResearchRun)
                        .where(
                            ResearchRun.tenant_id == tenant_id,
                            or_(
                                and_(
                                    ResearchRun.status == ResearchRunStatus.PENDING.value,
                                    ResearchRun.next_attempt_at <= now,
                                ),
                                and_(
                                    ResearchRun.status == ResearchRunStatus.RUNNING.value,
                                    or_(
                                        ResearchRun.lease_expires_at.is_(None),
                                        ResearchRun.lease_expires_at <= now,
                                    ),
                                ),
                            ),
                        )
                        .order_by(ResearchRun.next_attempt_at, ResearchRun.id)
                        .limit(remaining)
                        .with_for_update(skip_locked=True)
                    )
                    for run in (await session.scalars(statement)).all():
                        if run.attempts >= run.max_attempts:
                            run.status = ResearchRunStatus.FAILED.value
                            run.phase = ResearchPhase.FAILED.value
                            run.stop_reason = StopReason.TASK_FAILURES.value
                            run.error_code = "max_attempts_exceeded"
                            run.error_message = "The run exceeded its retry budget."
                            run.lease_expires_at = None
                            run.worker_id = None
                            run.pause_requested = False
                            run.cancel_requested = False
                            run.completed_at = now
                            record_event(
                                session,
                                tenant_id=tenant_id,
                                actor_user_id=run.requested_by,
                                action=AuditAction.RESEARCH_RUN_FAILED,
                                resource_type=ResourceType.RESEARCH_RUN,
                                resource_id=run.id,
                                outcome=AuditOutcome.DENIED,
                                details={"error_code": "max_attempts_exceeded"},
                            )
                            continue
                        run.status = ResearchRunStatus.RUNNING.value
                        run.phase = ResearchPhase.PLANNING.value
                        run.attempts += 1
                        run.worker_id = self._worker_id
                        run.lease_expires_at = now + timedelta(
                            seconds=self._settings.agent_run_lease_seconds
                        )
                        if run.started_at is None:
                            run.started_at = now
                        run.next_attempt_at = now
                        claimed.append(run)
        return claimed

    async def _prepare_run(self, run_id: UUID) -> PreparedRun | None:
        async with self._session_factory() as session:
            run = cast(
                ResearchRun | None,
                await session.scalar(
                    tenant_scoped_select(ResearchRun).where(ResearchRun.id == run_id)
                ),
            )
            if run is None:
                return None
            if run.cancel_requested:
                await self._finish_canceled(run.id)
                return None
            if run.pause_requested:
                await self._finish_paused(run.id)
                return None
            if run.requested_by is None:
                await self._fail_run(
                    run,
                    code="missing_actor",
                    message="The research run has no authorized owner.",
                )
                return None
            if (
                run.model_provider != self._settings.agent_provider
                or run.model_name != self._settings.agent_model
                or run.graph_version != RESEARCH_GRAPH_VERSION
            ):
                await self._fail_run(
                    run,
                    code="model_configuration_changed",
                    message="The research run model configuration no longer matches.",
                )
                return None
            try:
                principal = await resolve_principal_for_user(
                    session,
                    tenant_id=run.tenant_id,
                    user_id=run.requested_by,
                )
            except ForbiddenError:
                await self._fail_run(
                    run,
                    code="authorization_revoked",
                    message="The research owner is no longer authorized.",
                )
                return None

            if run.project_id is not None:
                bundle = await get_project_bundle(session, run.project_id)
                if bundle is None or bundle[1].id != run.workspace_id:
                    await self._fail_run(
                        run,
                        code="scope_unavailable",
                        message="The research project is no longer available.",
                    )
                    return None
                project, workspace, organization = bundle
                context = project_context(project, workspace, organization)
            else:
                workspace_bundle = await get_workspace_bundle(session, run.workspace_id)
                if workspace_bundle is None:
                    await self._fail_run(
                        run,
                        code="scope_unavailable",
                        message="The research workspace is no longer available.",
                    )
                    return None
                workspace, organization = workspace_bundle
                context = workspace_context(workspace, organization)

            decision = AuthorizationService(
                principal=principal,
                denial_audit=AuditWriter(self._session_factory),
            ).evaluate(Permission.RESEARCH_RUN_CONTROL, context)
            if not decision.allowed:
                await AuditWriter(self._session_factory).record_denial(
                    principal=principal,
                    permission=Permission.RESEARCH_RUN_CONTROL,
                    resource_type=ResourceType.RESEARCH_RUN,
                    resource_id=run.id,
                    reason=f"worker_{decision.reason}",
                )
                await self._fail_run(
                    run,
                    code="authorization_revoked",
                    message="The research owner is no longer authorized.",
                )
                return None

            document_scope: DocumentAccessScope | None = None
            if self._services.rag_services is not None:
                try:
                    document_scope = build_document_access_scope(
                        principal,
                        organization_id=organization.id,
                        workspace_id=workspace.id,
                        project_id=run.project_id,
                    )
                except ForbiddenError:
                    await self._fail_run(
                        run,
                        code="authorization_revoked",
                        message="The research owner no longer has document access.",
                    )
                    return None

            return PreparedRun(
                run=run,
                principal=principal,
                document_scope=document_scope,
                organization_id=organization.id,
                workspace_id=workspace.id,
                capability_snapshot=_capability_snapshot(
                    principal,
                    organization_id=organization.id,
                    workspace_id=workspace.id,
                    document_scope=document_scope,
                ),
            )

    async def _execute_run(self, run_id: UUID) -> None:
        prepared = await self._prepare_run(run_id)
        if prepared is None:
            return
        run = prepared.run
        control = await self._read_control(run.id)
        if control.cancel_requested:
            await self._finish_canceled(run.id)
            return
        if control.pause_requested:
            await self._finish_paused(run.id)
            return

        toolbox = ResearchToolbox(
            settings=self._settings,
            session_factory=self._session_factory,
            principal=prepared.principal,
            organization_id=prepared.organization_id,
            workspace_id=prepared.workspace_id,
            project_id=run.project_id,
            run_id=run.id,
            document_scope=prepared.document_scope,
            rag_services=self._services.rag_services,
            collector_services=self._services.collector_services,
        )
        if not toolbox.available_tools:
            await self._fail_run(
                run,
                code="no_authorized_tools",
                message="No authorized research tools are available.",
            )
            return

        state = await self._load_or_initial_state(run)
        state_budget = state.get("budget", {})
        max_seconds = float(state_budget.get("max_seconds", self._settings.agent_max_seconds))
        graph = build_research_graph(model=self._services.model, toolbox=toolbox)

        last_state: dict[str, Any] = state
        failed_with: Exception | None = None
        paused = False
        canceled = False
        timed_out = False
        authorization_lost = False
        heartbeat = asyncio.create_task(self._heartbeat_loop(run.id))
        try:
            if run.checkpoint_count == 0:
                await self._save_checkpoint(
                    run.id,
                    state,
                    node="runner_init",
                )
            stream = graph.astream(
                cast(ResearchState, state),
                config={"recursion_limit": 100},
                stream_mode="values",
            )
            try:
                async with asyncio.timeout(max_seconds):
                    async for value in stream:
                        last_state = dict(cast(dict[str, Any], value))
                        await self._save_checkpoint(
                            run.id,
                            last_state,
                            node=str(last_state.get("phase", "running")),
                        )
                        control = await self._read_control(run.id)
                        if control.cancel_requested or (
                            control.status == ResearchRunStatus.CANCELED.value
                        ):
                            canceled = True
                            break
                        if control.pause_requested or (
                            control.status == ResearchRunStatus.PAUSED.value
                        ):
                            paused = True
                            break
                        if not await self._capabilities_unchanged(prepared):
                            authorization_lost = True
                            break
            except TimeoutError:
                timed_out = True
            finally:
                close_stream = getattr(stream, "aclose", None)
                if close_stream is not None:
                    with suppress(Exception):
                        await close_stream()
        except asyncio.CancelledError:
            logger.info(
                "research_run_cancelled_by_worker",
                extra={"context": {"run_id": str(run.id)}},
            )
            raise
        except Exception as exc:
            failed_with = exc
            logger.exception(
                "research_run_execution_failed",
                extra={"context": {"run_id": str(run.id)}},
            )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("research_run_heartbeat_shutdown_failed")

        if failed_with is not None:
            code, message = _safe_state_error(failed_with)
            await self._fail_run(run, code=code, message=message, state=last_state)
            return
        if authorization_lost:
            await self._fail_run(
                run,
                code="authorization_changed",
                message="The research owner's authorization changed during the run.",
                state=last_state,
            )
            return
        if canceled:
            last_state = {
                **last_state,
                "phase": ResearchPhase.CANCELED.value,
                "stop_reason": StopReason.CANCELED.value,
            }
            await self._save_checkpoint(run.id, last_state, node="canceled")
            await self._finish_canceled(run.id)
            return
        if paused:
            last_state = {
                **last_state,
                "phase": ResearchPhase.PAUSED.value,
                "stop_reason": StopReason.PAUSED.value,
            }
            await self._save_checkpoint(run.id, last_state, node="paused")
            await self._finish_paused(run.id)
            return
        if timed_out:
            last_state = {
                **last_state,
                "phase": ResearchPhase.FINALIZING.value,
                "stop_reason": StopReason.TIME_BUDGET.value,
            }
            await self._save_checkpoint(run.id, last_state, node="time_budget")
            await self._finish_from_state(
                run.id,
                last_state,
                forced_status=ResearchRunStatus.BUDGET_EXHAUSTED,
            )
            return

        if last_state.get("phase") != ResearchPhase.FINALIZED.value:
            last_state = {
                **last_state,
                "phase": ResearchPhase.FINALIZED.value,
                "stop_reason": last_state.get("stop_reason") or StopReason.COMPLETED.value,
            }
        await self._save_checkpoint(run.id, last_state, node="finalized")
        await self._finish_from_state(run.id, last_state)

    async def _capabilities_unchanged(self, prepared: PreparedRun) -> bool:
        run = prepared.run
        async with self._session_factory() as session:
            try:
                principal = await resolve_principal_for_user(
                    session,
                    tenant_id=run.tenant_id,
                    user_id=prepared.principal.user_id,
                )
            except ForbiddenError:
                return False
            if run.project_id is not None:
                bundle = await get_project_bundle(session, run.project_id)
                if bundle is None or bundle[1].id != prepared.workspace_id:
                    return False
                project, workspace, organization = bundle
                context = project_context(project, workspace, organization)
            else:
                workspace_bundle = await get_workspace_bundle(session, prepared.workspace_id)
                if workspace_bundle is None:
                    return False
                workspace, organization = workspace_bundle
                context = workspace_context(workspace, organization)

            decision = AuthorizationService(
                principal=principal,
                denial_audit=AuditWriter(self._session_factory),
            ).evaluate(Permission.RESEARCH_RUN_CONTROL, context)
            if not decision.allowed:
                await AuditWriter(self._session_factory).record_denial(
                    principal=principal,
                    permission=Permission.RESEARCH_RUN_CONTROL,
                    resource_type=ResourceType.RESEARCH_RUN,
                    resource_id=run.id,
                    reason=f"worker_recheck_{decision.reason}",
                )
                return False
            document_scope = prepared.document_scope
            if self._services.rag_services is not None:
                try:
                    document_scope = build_document_access_scope(
                        principal,
                        organization_id=organization.id,
                        workspace_id=workspace.id,
                        project_id=run.project_id,
                    )
                except ForbiddenError:
                    return False
            snapshot = _capability_snapshot(
                principal,
                organization_id=organization.id,
                workspace_id=workspace.id,
                document_scope=document_scope,
            )
            return snapshot == prepared.capability_snapshot

    async def _load_or_initial_state(self, run: ResearchRun) -> dict[str, Any]:
        async with self._session_factory() as session:
            checkpoint = cast(
                ResearchCheckpoint | None,
                await session.scalar(
                    tenant_scoped_select(ResearchCheckpoint)
                    .where(ResearchCheckpoint.run_id == run.id)
                    .order_by(ResearchCheckpoint.sequence.desc())
                    .limit(1)
                ),
            )
        if checkpoint is not None and isinstance(checkpoint.state, dict):
            state = dict(checkpoint.state)
            state["run_id"] = str(run.id)
            state["budget"] = (
                state.get("budget") if isinstance(state.get("budget"), dict) else run.budget
            )
            # A pause checkpoint is a resumable snapshot, not a terminal graph
            # decision. Clear the control marker before re-entering the graph.
            if state.get("stop_reason") in {
                StopReason.PAUSED.value,
                StopReason.CANCELED.value,
            }:
                state["stop_reason"] = None
                state["phase"] = ResearchPhase.INITIALIZED.value
            return state
        return initial_state(
            run_id=run.id,
            objective=run.objective,
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            settings=self._settings,
        )

    async def _read_control(self, run_id: UUID) -> RunControl:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        ResearchRun.pause_requested,
                        ResearchRun.cancel_requested,
                        ResearchRun.status,
                    ).where(ResearchRun.id == run_id)
                )
            ).first()
        if row is None:
            return RunControl(False, True, ResearchRunStatus.CANCELED.value)
        return RunControl(bool(row[0]), bool(row[1]), str(row[2]))

    async def _heartbeat_loop(self, run_id: UUID) -> None:
        interval = max(5.0, self._settings.agent_run_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                async with self._session_factory() as session, session.begin():
                    run = await session.scalar(
                        tenant_scoped_select(ResearchRun)
                        .where(
                            ResearchRun.id == run_id,
                            ResearchRun.worker_id == self._worker_id,
                            ResearchRun.status == ResearchRunStatus.RUNNING.value,
                        )
                        .with_for_update()
                    )
                    if run is not None:
                        run.lease_expires_at = _iso_now() + timedelta(
                            seconds=self._settings.agent_run_lease_seconds
                        )
                        await session.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "research_run_heartbeat_failed",
                    extra={"context": {"run_id": str(run_id)}},
                )

    async def _save_checkpoint(
        self,
        run_id: UUID,
        state: dict[str, Any],
        *,
        node: str,
    ) -> None:
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > self._settings.agent_max_checkpoint_bytes:
            raise AgentError("The research checkpoint exceeded its size limit.")
        async with self._session_factory() as session, session.begin():
            run = cast(
                ResearchRun | None,
                await session.scalar(
                    tenant_scoped_select(ResearchRun)
                    .where(ResearchRun.id == run_id)
                    .with_for_update()
                ),
            )
            if run is None:
                return
            if run.worker_id != self._worker_id or (run.status != ResearchRunStatus.RUNNING.value):
                raise RunLeaseLostError
            sequence = run.checkpoint_count + 1
            checkpoint = ResearchCheckpoint(
                id=uuid4(),
                tenant_id=run.tenant_id,
                run_id=run.id,
                sequence=sequence,
                node=node[:64],
                phase=str(state.get("phase", run.phase))[:32],
                round=max(0, int(state.get("round", 0))),
                state=state,
            )
            session.add(checkpoint)
            await session.flush()
            await self._persist_findings(session, run=run, state=state)
            totals = usage_totals(cast(ResearchState, state))
            run.phase = checkpoint.phase
            run.round = checkpoint.round
            run.plan = state.get("plan", []) if isinstance(state.get("plan"), list) else []
            run.usage = totals.as_dict()
            run.error_count = len(unresolved_errors(cast(ResearchState, state)))
            run.checkpoint_count = sequence
            run.last_checkpoint_id = checkpoint.id
            run.finding_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ResearchFinding)
                    .where(ResearchFinding.run_id == run.id)
                )
                or 0
            )
            await session.flush()

    async def _persist_findings(
        self,
        session: AsyncSession,
        *,
        run: ResearchRun,
        state: dict[str, Any],
    ) -> None:
        workspace_id = run.workspace_id
        project_id = run.project_id
        for finding in unique_findings(cast(ResearchState, state)):
            try:
                source_id = UUID(str(finding["source_id"]))
                observed_at = datetime.fromisoformat(str(finding["observed_at"]))
                if observed_at.tzinfo is None:
                    observed_at = observed_at.replace(tzinfo=UTC)
                row = ResearchFinding(
                    id=uuid4(),
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    task_key=str(finding["task_key"])[:128],
                    agent_role=str(finding["agent_role"])[:32],
                    claim=str(finding["claim"]),
                    evidence=str(finding["evidence"]),
                    source_kind=str(finding["source_kind"])[:32],
                    source_id=source_id,
                    document_id=(
                        UUID(str(finding["document_id"])) if finding.get("document_id") else None
                    ),
                    document_version_id=(
                        UUID(str(finding["document_version_id"]))
                        if finding.get("document_version_id")
                        else None
                    ),
                    chunk_id=(UUID(str(finding["chunk_id"])) if finding.get("chunk_id") else None),
                    source_snapshot_id=(
                        UUID(str(finding["source_snapshot_id"]))
                        if finding.get("source_snapshot_id")
                        else None
                    ),
                    page_number=(
                        int(finding["page_number"])
                        if finding.get("page_number") is not None
                        else None
                    ),
                    paragraph_number=(
                        int(finding["paragraph_number"])
                        if finding.get("paragraph_number") is not None
                        else None
                    ),
                    start_offset=(
                        int(finding["start_offset"])
                        if finding.get("start_offset") is not None
                        else None
                    ),
                    end_offset=(
                        int(finding["end_offset"])
                        if finding.get("end_offset") is not None
                        else None
                    ),
                    source_title=(
                        str(finding["source_title"])[:500] if finding.get("source_title") else None
                    ),
                    source_url=(str(finding["source_url"]) if finding.get("source_url") else None),
                    observed_at=observed_at,
                    scope=str(finding["scope"])[:500],
                    limitations=str(finding["limitations"])[:500],
                    confidence=float(finding["confidence"]),
                    injection_suspected=bool(finding.get("injection_suspected", False)),
                    evidence_untrusted=True,
                    finding_key=str(finding["finding_key"])[:128],
                )
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "research_finding_invalid",
                    extra={"context": {"run_id": str(run.id)}},
                )
                continue
            try:
                async with session.begin_nested():
                    session.add(row)
                    await session.flush()
            except IntegrityError:
                # Unique finding keys make checkpoint replay idempotent.
                continue

    async def _finish_from_state(
        self,
        run_id: UUID,
        state: dict[str, Any],
        *,
        forced_status: ResearchRunStatus | None = None,
    ) -> None:
        unique = unique_findings(cast(ResearchState, state))
        latest = latest_task_results(cast(ResearchState, state))
        reason = state.get("stop_reason")
        failed = any(result.get("status") == TaskStatus.FAILED.value for result in latest.values())
        if forced_status is not None:
            status = forced_status
        elif reason in {StopReason.BUDGET_EXHAUSTED.value, StopReason.TIME_BUDGET.value}:
            status = ResearchRunStatus.BUDGET_EXHAUSTED
        elif reason == StopReason.CANCELED.value:
            status = ResearchRunStatus.CANCELED
        elif reason == StopReason.PAUSED.value:
            status = ResearchRunStatus.PAUSED
        elif not unique:
            status = ResearchRunStatus.FAILED
        elif (
            failed
            or unresolved_errors(cast(ResearchState, state))
            or reason == StopReason.PARTIAL_FAILURE.value
        ):
            status = ResearchRunStatus.PARTIAL
        else:
            status = ResearchRunStatus.COMPLETED

        if status == ResearchRunStatus.CANCELED:
            await self._finish_canceled(run_id)
            return
        if status == ResearchRunStatus.PAUSED:
            await self._finish_paused(run_id)
            return
        await self._finish_run(
            run_id,
            status=status,
            state=state,
            finding_count=len(unique),
        )

    async def _finish_run(
        self,
        run_id: UUID,
        *,
        status: ResearchRunStatus,
        state: dict[str, Any] | None = None,
        finding_count: int = 0,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            run = cast(
                ResearchRun | None,
                await session.scalar(
                    tenant_scoped_select(ResearchRun)
                    .where(ResearchRun.id == run_id)
                    .with_for_update()
                ),
            )
            if run is None:
                return
            if run.status in {value.value for value in TERMINAL_RUN_STATUSES}:
                return
            if run.worker_id not in {None, self._worker_id}:
                return
            state = state or {}
            reason = str(state.get("stop_reason") or status.value)
            run.status = status.value
            run.phase = (
                str(state.get("phase", ResearchPhase.FINALIZED.value))[:32]
                if status not in {ResearchRunStatus.CANCELED}
                else ResearchPhase.CANCELED.value
            )
            run.stop_reason = reason[:64]
            run.lease_expires_at = None
            run.worker_id = None
            run.pause_requested = False
            run.cancel_requested = False
            run.completed_at = _iso_now()
            if state:
                totals = usage_totals(cast(ResearchState, state))
                run.usage = totals.as_dict()
                run.error_count = len(unresolved_errors(cast(ResearchState, state)))
                run.round = max(0, int(state.get("round", run.round)))
                run.plan = (
                    state.get("plan", []) if isinstance(state.get("plan"), list) else run.plan
                )
            run.finding_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ResearchFinding)
                    .where(ResearchFinding.run_id == run.id)
                )
                or 0
            )
            if status == ResearchRunStatus.COMPLETED:
                run.error_code = None
                run.error_message = None
                action = AuditAction.RESEARCH_RUN_COMPLETED
                outcome = AuditOutcome.SUCCESS
                message = "The research run completed."
            elif status == ResearchRunStatus.PARTIAL:
                run.error_code = "partial_failure"
                run.error_message = "The run completed with unresolved task errors."
                action = AuditAction.RESEARCH_RUN_COMPLETED
                outcome = AuditOutcome.SUCCESS
                message = "The research run completed partially."
            elif status == ResearchRunStatus.BUDGET_EXHAUSTED:
                run.error_code = "budget_exhausted"
                run.error_message = "The research run exhausted its configured budget."
                action = AuditAction.RESEARCH_RUN_BUDGET_EXHAUSTED
                outcome = AuditOutcome.DENIED
                message = "The research run exhausted its configured budget."
            else:
                if reason == StopReason.NO_FINDINGS.value:
                    run.error_code = "no_findings"
                    run.error_message = "No evidence-backed findings were produced."
                elif reason == StopReason.PROVIDER_ERROR.value:
                    run.error_code = "provider_error"
                    run.error_message = "The model provider failed."
                else:
                    run.error_code = "task_failures"
                    run.error_message = "The research tasks failed."
                action = AuditAction.RESEARCH_RUN_FAILED
                outcome = AuditOutcome.DENIED
                message = run.error_message
            record_event(
                session,
                tenant_id=run.tenant_id,
                actor_user_id=run.requested_by,
                action=action,
                resource_type=ResourceType.RESEARCH_RUN,
                resource_id=run.id,
                outcome=outcome,
                details={
                    "status": status.value,
                    "stop_reason": reason,
                    "findings": finding_count,
                    "round": run.round,
                },
            )
            logger.info(
                "research_run_finished",
                extra={
                    "context": {
                        "run_id": str(run.id),
                        "status": status.value,
                        "stop_reason": reason,
                        "findings": finding_count,
                        "message": message,
                    }
                },
            )
            await session.flush()

    async def _finish_paused(self, run_id: UUID) -> None:
        async with self._session_factory() as session, session.begin():
            run = cast(
                ResearchRun | None,
                await session.scalar(
                    tenant_scoped_select(ResearchRun)
                    .where(ResearchRun.id == run_id)
                    .with_for_update()
                ),
            )
            if run is None or run.status == ResearchRunStatus.PAUSED.value:
                return
            if run.status in {value.value for value in TERMINAL_RUN_STATUSES}:
                return
            if run.worker_id not in {None, self._worker_id}:
                return
            run.status = ResearchRunStatus.PAUSED.value
            run.phase = ResearchPhase.PAUSED.value
            run.stop_reason = StopReason.PAUSED.value
            run.pause_requested = False
            run.cancel_requested = False
            run.lease_expires_at = None
            run.worker_id = None
            record_event(
                session,
                tenant_id=run.tenant_id,
                actor_user_id=run.requested_by,
                action=AuditAction.RESEARCH_RUN_PAUSED,
                resource_type=ResourceType.RESEARCH_RUN,
                resource_id=run.id,
                details={"requested": False, "checkpoint": str(run.last_checkpoint_id or "")},
            )
            await session.flush()

    async def _finish_canceled(self, run_id: UUID) -> None:
        async with self._session_factory() as session, session.begin():
            run = cast(
                ResearchRun | None,
                await session.scalar(
                    tenant_scoped_select(ResearchRun)
                    .where(ResearchRun.id == run_id)
                    .with_for_update()
                ),
            )
            if run is None or run.status == ResearchRunStatus.CANCELED.value:
                return
            if run.status in {value.value for value in TERMINAL_RUN_STATUSES}:
                return
            if run.worker_id not in {None, self._worker_id}:
                return
            run.status = ResearchRunStatus.CANCELED.value
            run.phase = ResearchPhase.CANCELED.value
            run.stop_reason = StopReason.CANCELED.value
            run.pause_requested = False
            run.cancel_requested = False
            run.lease_expires_at = None
            run.worker_id = None
            run.completed_at = _iso_now()
            record_event(
                session,
                tenant_id=run.tenant_id,
                actor_user_id=run.requested_by,
                action=AuditAction.RESEARCH_RUN_CANCELED,
                resource_type=ResourceType.RESEARCH_RUN,
                resource_id=run.id,
                details={"requested": False},
            )
            await session.flush()

    async def _fail_run(
        self,
        run: ResearchRun,
        *,
        code: str,
        message: str,
        state: dict[str, Any] | None = None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            current = cast(
                ResearchRun | None,
                await session.scalar(
                    tenant_scoped_select(ResearchRun)
                    .where(ResearchRun.id == run.id)
                    .with_for_update()
                ),
            )
            if current is None or current.status in {
                value.value for value in TERMINAL_RUN_STATUSES
            }:
                return
            if current.worker_id not in {None, self._worker_id}:
                return
            current.status = ResearchRunStatus.FAILED.value
            current.phase = ResearchPhase.FAILED.value
            current.stop_reason = code[:64]
            current.error_code = code[:64]
            current.error_message = message[:500]
            current.pause_requested = False
            current.cancel_requested = False
            current.lease_expires_at = None
            current.worker_id = None
            current.completed_at = _iso_now()
            if state:
                current.usage = usage_totals(cast(ResearchState, state)).as_dict()
                current.error_count = len(unresolved_errors(cast(ResearchState, state)))
            current.error_count = max(1, current.error_count)
            record_event(
                session,
                tenant_id=current.tenant_id,
                actor_user_id=current.requested_by,
                action=AuditAction.RESEARCH_RUN_FAILED,
                resource_type=ResourceType.RESEARCH_RUN,
                resource_id=current.id,
                outcome=AuditOutcome.DENIED,
                details={"error_code": code[:64]},
            )
            logger.error(
                "research_run_failed",
                extra={
                    "context": {
                        "run_id": str(current.id),
                        "error_code": code[:64],
                        "message": message[:200],
                    }
                },
            )
            await session.flush()


__all__ = ["ResearchRunner"]
