"""Durable review and export workers for reports."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.enums import (
    RESEARCH_GRAPH_VERSION,
    TERMINAL_RUN_STATUSES,
    ResearchRunKind,
    ResearchRunStatus,
)
from app.agent.state import budget_for_settings
from app.auth.authorization import AuthorizationService, ResourceContext
from app.auth.enums import AuditAction, AuditOutcome, Permission, ResourceType
from app.auth.principal import Principal, resolve_principal_for_user
from app.core.config import Settings
from app.core.context import tenant_context
from app.core.errors import ForbiddenError
from app.db.models import (
    Report,
    ReportExportJob,
    ReportVersion,
    ResearchReview,
    ResearchRun,
    Tenant,
)
from app.db.tenant_scope import tenant_scoped_select
from app.reporting.builder import build_report_draft, report_checksum
from app.reporting.entities import derive_entities
from app.reporting.enums import (
    ExportFormat,
    ExportStatus,
    ReportStatus,
    ReportVersionStatus,
    ReviewStatus,
)
from app.reporting.errors import (
    ExportNotApprovedError,
    ReportingError,
    ReviewProcessingError,
)
from app.reporting.exports import render_export
from app.reporting.reviewer import ReviewResult, review_claims
from app.reporting.rules import sanitize_text
from app.reporting.service import ReviewServices
from app.services.audit import AuditWriter, record_event
from app.services.tenancy import (
    get_project_bundle,
    get_workspace_bundle,
    project_context,
    workspace_context,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_error(exc: Exception) -> tuple[str, str, bool]:
    if isinstance(exc, ReportingError):
        return exc.code, exc.safe_message, exc.retryable
    return "internal_error", "The reporting operation failed.", True


def _is_terminal_run(status: str) -> bool:
    return status in {value.value for value in TERMINAL_RUN_STATUSES}


@dataclass(frozen=True, slots=True)
class _PreparedReview:
    review_id: UUID
    review_round: int
    followup_round: int
    followup_active: bool
    root_run: ResearchRun
    lineage: list[ResearchRun]
    principal: Principal
    context: ResourceContext


class ReviewRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        services: ReviewServices,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._services = services
        self._session_factory = session_factory
        self._worker_id = f"review-{uuid4().hex[:12]}"

    async def run_once(self, *, tenant_ids: Sequence[UUID] | None = None) -> int:
        reviews = await self._claim_reviews(
            self._settings.report_max_concurrent_reviews,
            tenant_ids=tenant_ids,
        )
        if not reviews:
            return 0
        semaphore = asyncio.Semaphore(self._settings.report_max_concurrent_reviews)

        async def execute(review: ResearchReview) -> None:
            async with semaphore:
                with tenant_context(review.tenant_id):
                    await self._execute_review(review.id)

        await asyncio.gather(*(execute(review) for review in reviews), return_exceptions=True)
        return len(reviews)

    async def _claim_reviews(
        self,
        limit: int,
        *,
        tenant_ids: Sequence[UUID] | None = None,
    ) -> list[ResearchReview]:
        async with self._session_factory() as session:
            tenant_statement = select(Tenant.id).where(Tenant.status == "active")
            if tenant_ids is not None:
                tenant_statement = tenant_statement.where(Tenant.id.in_(tenant_ids))
            active_tenant_ids = list((await session.scalars(tenant_statement)).all())

        claimed: list[ResearchReview] = []
        now = _now()
        for tenant_id in active_tenant_ids:
            remaining = limit - len(claimed)
            if remaining <= 0:
                break
            with tenant_context(tenant_id):
                async with self._session_factory() as session, session.begin():
                    statement = (
                        select(ResearchReview)
                        .where(
                            ResearchReview.tenant_id == tenant_id,
                            or_(
                                and_(
                                    ResearchReview.status.in_(
                                        [
                                            ReviewStatus.PENDING.value,
                                            ReviewStatus.AWAITING_FOLLOWUP.value,
                                        ]
                                    ),
                                    ResearchReview.next_attempt_at <= now,
                                ),
                                and_(
                                    ResearchReview.status == ReviewStatus.RUNNING.value,
                                    or_(
                                        ResearchReview.lease_expires_at.is_(None),
                                        ResearchReview.lease_expires_at <= now,
                                    ),
                                ),
                            ),
                        )
                        .order_by(ResearchReview.next_attempt_at, ResearchReview.id)
                        .limit(remaining)
                        .with_for_update(skip_locked=True)
                    )
                    for review in (await session.scalars(statement)).all():
                        if review.attempts >= review.max_attempts:
                            review.status = ReviewStatus.FAILED.value
                            review.error_code = "max_attempts_exceeded"
                            review.error_message = "The review exceeded its retry budget."
                            review.lease_expires_at = None
                            review.worker_id = None
                            review.completed_at = now
                            record_event(
                                session,
                                tenant_id=tenant_id,
                                actor_user_id=None,
                                action=AuditAction.RESEARCH_REVIEW_FAILED,
                                resource_type=ResourceType.RESEARCH_REVIEW,
                                resource_id=review.id,
                                outcome=AuditOutcome.DENIED,
                                details={"error_code": "max_attempts_exceeded"},
                            )
                            continue
                        review.status = ReviewStatus.RUNNING.value
                        review.attempts += 1
                        review.worker_id = self._worker_id
                        review.lease_expires_at = now + timedelta(
                            seconds=self._settings.report_review_lease_seconds
                        )
                        if review.started_at is None:
                            review.started_at = now
                        claimed.append(review)
        return claimed

    async def _prepare_review(self, review_id: UUID) -> _PreparedReview | None:
        async with self._session_factory() as session:
            review = cast(
                ResearchReview | None,
                await session.scalar(
                    tenant_scoped_select(ResearchReview).where(ResearchReview.id == review_id)
                ),
            )
            if review is None:
                return None
            root_run = cast(
                ResearchRun | None,
                await session.scalar(
                    tenant_scoped_select(ResearchRun).where(ResearchRun.id == review.root_run_id)
                ),
            )
            if root_run is None:
                await self._fail_review(
                    review_id,
                    code="root_run_missing",
                    message="The reviewed research run no longer exists.",
                    retryable=False,
                )
                return None
            if not _is_terminal_run(root_run.status):
                await self._fail_review(
                    review_id,
                    code="run_not_terminal",
                    message="The research run is not terminal.",
                    retryable=False,
                )
                return None
            followup_active = False
            if review.followup_run_id is not None:
                followup = cast(
                    ResearchRun | None,
                    await session.scalar(
                        tenant_scoped_select(ResearchRun).where(
                            ResearchRun.id == review.followup_run_id
                        )
                    ),
                )
                followup_active = followup is not None and not _is_terminal_run(followup.status)
            principal = await self._resolve_principal(session, root_run)
            if principal is None:
                await self._fail_review(
                    review_id,
                    code="authorization_revoked",
                    message="The research owner is no longer authorized.",
                    retryable=False,
                )
                return None
            context = await self._run_context(session, root_run)
            if context is None:
                await self._fail_review(
                    review_id,
                    code="scope_unavailable",
                    message="The research scope is no longer available.",
                    retryable=False,
                )
                return None
            authorization = AuthorizationService(
                principal=principal,
                denial_audit=AuditWriter(self._session_factory),
            )
            decision = authorization.evaluate(Permission.REPORT_READ, context)
            if not decision.allowed:
                await AuditWriter(self._session_factory).record_denial(
                    principal=principal,
                    permission=Permission.REPORT_READ,
                    resource_type=ResourceType.RESEARCH_REVIEW,
                    resource_id=review.id,
                    reason=f"review_{decision.reason}",
                )
                await self._fail_review(
                    review_id,
                    code="authorization_revoked",
                    message="The research owner no longer has report access.",
                    retryable=False,
                )
                return None
            lineage = await self._lineage_runs(session, root_run)
            return _PreparedReview(
                review_id=review.id,
                review_round=review.review_round,
                followup_round=review.followup_round,
                followup_active=followup_active,
                root_run=root_run,
                lineage=lineage,
                principal=principal,
                context=context,
            )

    async def _execute_review(self, review_id: UUID) -> None:
        try:
            prepared = await self._prepare_review(review_id)
            if prepared is None:
                return
            if prepared.followup_active:
                await self._reschedule_awaiting(review_id)
                return
            root_run = prepared.root_run
            async with self._session_factory() as work_session, work_session.begin():
                record_event(
                    work_session,
                    tenant_id=root_run.tenant_id,
                    actor_user_id=root_run.requested_by,
                    action=AuditAction.RESEARCH_REVIEW_STARTED,
                    resource_type=ResourceType.RESEARCH_REVIEW,
                    resource_id=review_id,
                    details={"review_round": prepared.review_round},
                )
                await derive_entities(
                    work_session,
                    root_run=root_run,
                    lineage_run_ids=[run.id for run in prepared.lineage],
                    settings=self._settings,
                )
                result = await review_claims(
                    work_session,
                    root_run_id=root_run.id,
                    settings=self._settings,
                )

            can_followup = (
                prepared.followup_round < self._settings.report_max_followup_rounds
                and bool(result.followup_questions)
                and root_run.requested_by is not None
                and root_run.model_provider == self._settings.agent_provider
                and root_run.model_name == self._settings.agent_model
                and root_run.graph_version == RESEARCH_GRAPH_VERSION
            )
            if can_followup:
                control = AuthorizationService(
                    principal=prepared.principal,
                    denial_audit=AuditWriter(self._session_factory),
                ).evaluate(Permission.RESEARCH_RUN_CONTROL, prepared.context)
                can_followup = control.allowed
                if not can_followup:
                    await AuditWriter(self._session_factory).record_denial(
                        principal=prepared.principal,
                        permission=Permission.RESEARCH_RUN_CONTROL,
                        resource_type=ResourceType.RESEARCH_REVIEW,
                        resource_id=review_id,
                        reason=f"followup_{control.reason}",
                    )
            if can_followup:
                await self._create_followup(
                    review_id=review_id,
                    root_run=root_run,
                    questions=result.followup_questions,
                    result=result,
                    principal=prepared.principal,
                    review_round=prepared.review_round,
                )
                return
            await self._finalize_report(
                review_id=review_id,
                root_run=root_run,
                lineage=prepared.lineage,
                result=result,
                principal=prepared.principal,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code, message, retryable = _safe_error(exc)
            logger.exception(
                "research_review_processing_failed",
                extra={"context": {"review_id": str(review_id), "error": type(exc).__name__}},
            )
            await self._fail_review(
                review_id,
                code=code,
                message=message,
                retryable=retryable,
            )

    async def _resolve_principal(
        self,
        session: AsyncSession,
        root_run: ResearchRun,
    ) -> Principal | None:
        if root_run.requested_by is None:
            return None
        try:
            return await resolve_principal_for_user(
                session,
                tenant_id=root_run.tenant_id,
                user_id=root_run.requested_by,
            )
        except ForbiddenError:
            return None

    async def _run_context(
        self,
        session: AsyncSession,
        root_run: ResearchRun,
    ) -> ResourceContext | None:
        if root_run.project_id is not None:
            bundle = await get_project_bundle(session, root_run.project_id)
            if bundle is None or bundle[1].id != root_run.workspace_id:
                return None
            return project_context(*bundle)
        workspace_bundle = await get_workspace_bundle(session, root_run.workspace_id)
        if workspace_bundle is None:
            return None
        return workspace_context(*workspace_bundle)

    async def _lineage_runs(
        self,
        session: AsyncSession,
        root_run: ResearchRun,
    ) -> list[ResearchRun]:
        children = list(
            (
                await session.scalars(
                    tenant_scoped_select(ResearchRun)
                    .where(
                        ResearchRun.root_run_id == root_run.id,
                        ResearchRun.run_kind == ResearchRunKind.FOLLOWUP.value,
                    )
                    .order_by(ResearchRun.followup_round, ResearchRun.id)
                )
            ).all()
        )
        return [root_run, *children]

    async def _reschedule_awaiting(self, review_id: UUID) -> None:
        async with self._session_factory() as session, session.begin():
            review = cast(
                ResearchReview | None,
                await session.scalar(
                    tenant_scoped_select(ResearchReview)
                    .where(ResearchReview.id == review_id)
                    .with_for_update()
                ),
            )
            if (
                review is None
                or review.worker_id != self._worker_id
                or review.status != ReviewStatus.RUNNING.value
            ):
                return
            review.status = ReviewStatus.AWAITING_FOLLOWUP.value
            review.attempts = max(0, review.attempts - 1)
            review.next_attempt_at = _now() + timedelta(
                seconds=self._settings.report_review_poll_interval_seconds
            )
            review.lease_expires_at = None
            review.worker_id = None
            await session.flush()

    async def _create_followup(
        self,
        *,
        review_id: UUID,
        root_run: ResearchRun,
        questions: Sequence[str],
        result: ReviewResult,
        principal: Principal,
        review_round: int,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            review = cast(
                ResearchReview | None,
                await session.scalar(
                    tenant_scoped_select(ResearchReview)
                    .where(ResearchReview.id == review_id)
                    .with_for_update()
                ),
            )
            if (
                review is None
                or review.worker_id != self._worker_id
                or review.status != ReviewStatus.RUNNING.value
            ):
                return
            current_root = cast(
                ResearchRun | None,
                await session.scalar(
                    tenant_scoped_select(ResearchRun)
                    .where(ResearchRun.id == review.root_run_id)
                    .with_for_update()
                ),
            )
            if current_root is None:
                raise ReviewProcessingError("The root research run disappeared.")
            followup_round = review.followup_round + 1
            sanitized_questions = [
                cleaned
                for question in questions[: self._settings.report_max_followup_questions]
                if (cleaned := sanitize_text(question, max_chars=700))
            ]
            objective = current_root.objective
            addition = (
                "\n\nReview follow-up "
                + str(followup_round)
                + ":\n"
                + "\n".join(f"- {question}" for question in sanitized_questions)
            )
            objective = (objective + addition)[: self._settings.agent_max_objective_chars]
            followup_run = ResearchRun(
                id=uuid4(),
                tenant_id=current_root.tenant_id,
                workspace_id=current_root.workspace_id,
                project_id=current_root.project_id,
                parent_run_id=current_root.id,
                root_run_id=current_root.id,
                run_kind=ResearchRunKind.FOLLOWUP.value,
                followup_round=followup_round,
                requested_by=current_root.requested_by,
                objective=objective,
                model_provider=self._settings.agent_provider,
                model_name=self._settings.agent_model,
                graph_version=RESEARCH_GRAPH_VERSION,
                status=ResearchRunStatus.PENDING.value,
                max_rounds=self._settings.agent_max_rounds,
                budget=budget_for_settings(self._settings),
                usage={},
                plan=[],
                next_attempt_at=_now(),
                attempts=0,
                max_attempts=self._settings.agent_max_run_attempts,
            )
            session.add(followup_run)
            await session.flush()
            record_event(
                session,
                tenant_id=current_root.tenant_id,
                actor_user_id=current_root.requested_by,
                action=AuditAction.RESEARCH_RUN_CREATED,
                resource_type=ResourceType.RESEARCH_RUN,
                resource_id=followup_run.id,
                details={
                    "root_run_id": str(current_root.id),
                    "run_kind": ResearchRunKind.FOLLOWUP.value,
                    "followup_round": followup_round,
                },
            )
            record_event(
                session,
                tenant_id=current_root.tenant_id,
                actor_user_id=current_root.requested_by,
                action=AuditAction.RESEARCH_FOLLOWUP_CREATED,
                resource_type=ResourceType.RESEARCH_REVIEW,
                resource_id=review.id,
                details={
                    "followup_run_id": str(followup_run.id),
                    "followup_round": followup_round,
                },
            )
            review.followup_round = followup_round
            review.followup_run_id = followup_run.id
            review.followup_questions = sanitized_questions
            review.status = ReviewStatus.AWAITING_FOLLOWUP.value
            review.completed_at = None
            review.next_attempt_at = _now() + timedelta(
                seconds=self._settings.report_review_poll_interval_seconds
            )
            review.lease_expires_at = None
            review.worker_id = None
            review.attempts = 0
            review.claim_count = len(result.claims)
            review.supported_count = result.supported_count
            review.partial_count = result.partial_count
            review.insufficient_count = result.insufficient_count
            review.conflicted_count = result.conflicted_count
            review.invalid_count = result.invalid_count
            review.source_count = result.source_count
            review.evidence_count = result.evidence_count
            review.error_code = None
            review.error_message = None
            record_event(
                session,
                tenant_id=current_root.tenant_id,
                actor_user_id=principal.user_id,
                action=AuditAction.RESEARCH_REVIEW_AWAITING_FOLLOWUP,
                resource_type=ResourceType.RESEARCH_REVIEW,
                resource_id=review.id,
                details={
                    "followup_run_id": str(followup_run.id),
                    "followup_round": followup_round,
                    "review_round": review_round,
                },
            )
            await session.flush()

    async def _finalize_report(
        self,
        *,
        review_id: UUID,
        root_run: ResearchRun,
        lineage: list[ResearchRun],
        result: ReviewResult,
        principal: Principal,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            review = cast(
                ResearchReview | None,
                await session.scalar(
                    tenant_scoped_select(ResearchReview)
                    .where(ResearchReview.id == review_id)
                    .with_for_update()
                ),
            )
            if (
                review is None
                or review.worker_id != self._worker_id
                or review.status != ReviewStatus.RUNNING.value
            ):
                return
            report = await session.scalar(
                tenant_scoped_select(Report)
                .where(Report.root_run_id == root_run.id)
                .with_for_update()
            )
            if report is None:
                report = Report(
                    id=uuid4(),
                    tenant_id=root_run.tenant_id,
                    root_run_id=root_run.id,
                    workspace_id=root_run.workspace_id,
                    project_id=root_run.project_id,
                    status=ReportStatus.DRAFT.value,
                    created_by=root_run.requested_by,
                )
                session.add(report)
                await session.flush()
            draft = await build_report_draft(
                session,
                root_run=root_run,
                review=review,
                lineage_runs=lineage,
                settings=self._settings,
            )
            next_version = (
                int(
                    await session.scalar(
                        select(func.coalesce(func.max(ReportVersion.version_number), 0)).where(
                            ReportVersion.tenant_id == root_run.tenant_id,
                            ReportVersion.report_id == report.id,
                        )
                    )
                    or 0
                )
                + 1
            )
            version = ReportVersion(
                id=uuid4(),
                tenant_id=root_run.tenant_id,
                report_id=report.id,
                version_number=next_version,
                status=ReportVersionStatus.DRAFT.value,
                review_id=review.id,
                title=draft.title,
                content_markdown=draft.markdown,
                structured=draft.structured,
                source_list=draft.source_list,
                citation_index=draft.citation_index,
                checksum_sha256=report_checksum(draft.markdown, draft.structured),
                created_by=root_run.requested_by,
            )
            session.add(version)
            await session.flush()
            report.current_version_id = version.id
            report.status = ReportStatus.DRAFT.value
            review.status = ReviewStatus.COMPLETED.value
            review.followup_questions = list(result.followup_questions)
            review.claim_count = len(result.claims)
            review.supported_count = result.supported_count
            review.partial_count = result.partial_count
            review.insufficient_count = result.insufficient_count
            review.conflicted_count = result.conflicted_count
            review.invalid_count = result.invalid_count
            review.source_count = result.source_count
            review.evidence_count = result.evidence_count
            review.error_code = None
            review.error_message = None
            review.completed_at = _now()
            review.lease_expires_at = None
            review.worker_id = None
            review.attempts = 0
            record_event(
                session,
                tenant_id=root_run.tenant_id,
                actor_user_id=principal.user_id,
                action=AuditAction.REPORT_VERSION_CREATED,
                resource_type=ResourceType.REPORT_VERSION,
                resource_id=version.id,
                details={
                    "report_id": str(report.id),
                    "root_run_id": str(root_run.id),
                    "version_number": next_version,
                    "checksum": version.checksum_sha256,
                    "claims": review.claim_count,
                    "supported": review.supported_count,
                },
            )
            record_event(
                session,
                tenant_id=root_run.tenant_id,
                actor_user_id=principal.user_id,
                action=AuditAction.RESEARCH_REVIEW_COMPLETED,
                resource_type=ResourceType.RESEARCH_REVIEW,
                resource_id=review.id,
                details={
                    "report_version_id": str(version.id),
                    "claims": review.claim_count,
                    "followup_round": review.followup_round,
                },
            )
            await session.flush()

    async def _fail_review(
        self,
        review_id: UUID,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            review = cast(
                ResearchReview | None,
                await session.scalar(
                    tenant_scoped_select(ResearchReview)
                    .where(ResearchReview.id == review_id)
                    .with_for_update()
                ),
            )
            if (
                review is None
                or review.worker_id != self._worker_id
                or review.status != ReviewStatus.RUNNING.value
            ):
                return
            review.error_code = code[:64]
            review.error_message = message[:500]
            review.lease_expires_at = None
            review.worker_id = None
            if retryable and review.attempts < review.max_attempts:
                review.status = ReviewStatus.PENDING.value
                review.next_attempt_at = _now() + timedelta(
                    seconds=self._settings.report_review_poll_interval_seconds
                    * (2 ** max(0, review.attempts - 1))
                )
                await session.flush()
                return
            review.status = ReviewStatus.FAILED.value
            review.completed_at = _now()
            record_event(
                session,
                tenant_id=review.tenant_id,
                actor_user_id=None,
                action=AuditAction.RESEARCH_REVIEW_FAILED,
                resource_type=ResourceType.RESEARCH_REVIEW,
                resource_id=review.id,
                outcome=AuditOutcome.DENIED,
                details={"error_code": code[:64]},
            )
            await session.flush()


class ExportRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        services: ReviewServices,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._services = services
        self._session_factory = session_factory
        self._worker_id = f"export-{uuid4().hex[:12]}"

    async def run_once(self, *, tenant_ids: Sequence[UUID] | None = None) -> int:
        jobs = await self._claim_jobs(
            self._settings.report_max_concurrent_exports,
            tenant_ids=tenant_ids,
        )
        if not jobs:
            return 0
        semaphore = asyncio.Semaphore(self._settings.report_max_concurrent_exports)

        async def execute(job: ReportExportJob) -> None:
            async with semaphore:
                with tenant_context(job.tenant_id):
                    await self._execute_job(job.id)

        await asyncio.gather(*(execute(job) for job in jobs), return_exceptions=True)
        return len(jobs)

    async def _claim_jobs(
        self,
        limit: int,
        *,
        tenant_ids: Sequence[UUID] | None = None,
    ) -> list[ReportExportJob]:
        async with self._session_factory() as session:
            tenant_statement = select(Tenant.id).where(Tenant.status == "active")
            if tenant_ids is not None:
                tenant_statement = tenant_statement.where(Tenant.id.in_(tenant_ids))
            active_tenant_ids = list((await session.scalars(tenant_statement)).all())
        claimed: list[ReportExportJob] = []
        now = _now()
        for tenant_id in active_tenant_ids:
            remaining = limit - len(claimed)
            if remaining <= 0:
                break
            with tenant_context(tenant_id):
                async with self._session_factory() as session, session.begin():
                    statement = (
                        select(ReportExportJob)
                        .where(
                            ReportExportJob.tenant_id == tenant_id,
                            or_(
                                and_(
                                    ReportExportJob.status == ExportStatus.PENDING.value,
                                    ReportExportJob.next_attempt_at <= now,
                                ),
                                and_(
                                    ReportExportJob.status == ExportStatus.RUNNING.value,
                                    or_(
                                        ReportExportJob.lease_expires_at.is_(None),
                                        ReportExportJob.lease_expires_at <= now,
                                    ),
                                ),
                            ),
                        )
                        .order_by(ReportExportJob.next_attempt_at, ReportExportJob.id)
                        .limit(remaining)
                        .with_for_update(skip_locked=True)
                    )
                    for job in (await session.scalars(statement)).all():
                        if job.attempts >= job.max_attempts:
                            job.status = ExportStatus.FAILED.value
                            job.error_code = "max_attempts_exceeded"
                            job.error_message = "The export exceeded its retry budget."
                            job.lease_expires_at = None
                            job.worker_id = None
                            job.completed_at = now
                            record_event(
                                session,
                                tenant_id=tenant_id,
                                actor_user_id=job.requested_by,
                                action=AuditAction.REPORT_EXPORT_FAILED,
                                resource_type=ResourceType.REPORT_EXPORT,
                                resource_id=job.id,
                                outcome=AuditOutcome.DENIED,
                                details={"error_code": "max_attempts_exceeded"},
                            )
                            continue
                        job.status = ExportStatus.RUNNING.value
                        job.attempts += 1
                        job.worker_id = self._worker_id
                        job.lease_expires_at = now + timedelta(
                            seconds=self._settings.report_export_lease_seconds
                        )
                        claimed.append(job)
        return claimed

    async def _execute_job(self, job_id: UUID) -> None:
        try:
            async with self._session_factory() as session:
                job = cast(
                    ReportExportJob | None,
                    await session.scalar(
                        tenant_scoped_select(ReportExportJob).where(ReportExportJob.id == job_id)
                    ),
                )
                if job is None:
                    return
                version = cast(
                    ReportVersion | None,
                    await session.scalar(
                        tenant_scoped_select(ReportVersion).where(
                            ReportVersion.id == job.version_id
                        )
                    ),
                )
                if version is None:
                    await self._fail_job(
                        job_id,
                        code="version_missing",
                        message="The report version no longer exists.",
                        retryable=False,
                    )
                    return
                if version.status != ReportVersionStatus.APPROVED.value:
                    await self._fail_job(
                        job_id,
                        code=ExportNotApprovedError.code,
                        message=ExportNotApprovedError.safe_message,
                        retryable=False,
                    )
                    return
                if (
                    report_checksum(version.content_markdown, version.structured)
                    != version.checksum_sha256
                ):
                    await self._fail_job(
                        job_id,
                        code="report_integrity_failed",
                        message="The report version failed its integrity check.",
                        retryable=False,
                    )
                    return
                rendered = render_export(
                    version,
                    export_format=ExportFormat(job.format),
                    settings=self._settings,
                )
                object_key = (
                    f"tenants/{job.tenant_id}/reports/{job.report_id}/"
                    f"versions/{version.id}/exports/{job.id}.{rendered.extension}"
                )
                await self._services.storage.put(
                    object_key,
                    rendered.data,
                    rendered.content_type,
                )
            async with self._session_factory() as session, session.begin():
                current = cast(
                    ReportExportJob | None,
                    await session.scalar(
                        tenant_scoped_select(ReportExportJob)
                        .where(ReportExportJob.id == job_id)
                        .with_for_update()
                    ),
                )
                if (
                    current is None
                    or current.worker_id != self._worker_id
                    or current.status != ExportStatus.RUNNING.value
                ):
                    return
                current.status = ExportStatus.SUCCEEDED.value
                current.object_key = object_key
                current.content_type = rendered.content_type
                current.size_bytes = len(rendered.data)
                current.error_code = None
                current.error_message = None
                current.lease_expires_at = None
                current.worker_id = None
                current.completed_at = _now()
                record_event(
                    session,
                    tenant_id=current.tenant_id,
                    actor_user_id=current.requested_by,
                    action=AuditAction.REPORT_EXPORT_SUCCEEDED,
                    resource_type=ResourceType.REPORT_EXPORT,
                    resource_id=current.id,
                    details={
                        "format": current.format,
                        "version_id": str(current.version_id),
                        "bytes": current.size_bytes,
                    },
                )
                await session.flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code, message, retryable = _safe_error(exc)
            logger.exception(
                "report_export_processing_failed",
                extra={"context": {"job_id": str(job_id), "error": type(exc).__name__}},
            )
            await self._fail_job(
                job_id,
                code=code,
                message=message,
                retryable=retryable,
            )

    async def _fail_job(
        self,
        job_id: UUID,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            job = cast(
                ReportExportJob | None,
                await session.scalar(
                    tenant_scoped_select(ReportExportJob)
                    .where(ReportExportJob.id == job_id)
                    .with_for_update()
                ),
            )
            if (
                job is None
                or job.worker_id != self._worker_id
                or job.status != ExportStatus.RUNNING.value
            ):
                return
            job.error_code = code[:64]
            job.error_message = message[:500]
            job.lease_expires_at = None
            job.worker_id = None
            if retryable and job.attempts < job.max_attempts:
                job.status = ExportStatus.PENDING.value
                job.next_attempt_at = _now() + timedelta(
                    seconds=self._settings.report_export_poll_interval_seconds
                    * (2 ** max(0, job.attempts - 1))
                )
                await session.flush()
                return
            job.status = ExportStatus.FAILED.value
            job.completed_at = _now()
            record_event(
                session,
                tenant_id=job.tenant_id,
                actor_user_id=job.requested_by,
                action=AuditAction.REPORT_EXPORT_FAILED,
                resource_type=ResourceType.REPORT_EXPORT,
                resource_id=job.id,
                outcome=AuditOutcome.DENIED,
                details={"error_code": code[:64]},
            )
            await session.flush()


__all__ = ["ExportRunner", "ReviewRunner"]
