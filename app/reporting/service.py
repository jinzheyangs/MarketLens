"""审查/报告持久化与人工审批/导出操作。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.enums import AuditAction, ResourceType
from app.auth.principal import Principal
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ServiceUnavailableError
from app.db.models import (
    Report,
    ReportExportJob,
    ReportVersion,
    ResearchCitation,
    ResearchClaim,
    ResearchEvidence,
    ResearchReview,
    ResearchRun,
    ResearchSource,
)
from app.db.tenant_scope import tenant_scoped_select
from app.rag.storage import ObjectStorage, build_object_storage
from app.reporting.builder import report_checksum
from app.reporting.enums import (
    REPORT_RULES_VERSION,
    ExportFormat,
    ExportStatus,
    ReportStatus,
    ReportVersionStatus,
    ReviewStatus,
)
from app.services.audit import record_event

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ReviewServices:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    storage: ObjectStorage

    async def ensure_ready(self) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 11):
            try:
                await self.storage.ensure_ready()
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "report_storage_not_ready",
                    extra={"context": {"attempt": attempt, "error": type(exc).__name__}},
                )
                if attempt < 10:
                    await asyncio.sleep(1)
        if last_error is not None:
            raise last_error

    async def aclose(self) -> None:
        await self.storage.aclose()


def build_review_services(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> ReviewServices:
    return ReviewServices(
        settings=settings,
        session_factory=session_factory,
        storage=build_object_storage(settings),
    )


def require_services(services: ReviewServices | None) -> ReviewServices:
    if services is None:
        raise ServiceUnavailableError("Reports are not configured.")
    return services


def root_run_id(run: ResearchRun) -> UUID:
    return run.root_run_id or run.id


async def get_review(
    session: AsyncSession,
    *,
    root_run_id: UUID,
    for_update: bool = False,
) -> ResearchReview | None:
    statement = tenant_scoped_select(ResearchReview).where(
        ResearchReview.root_run_id == root_run_id
    )
    if for_update:
        statement = statement.with_for_update()
    return cast(ResearchReview | None, await session.scalar(statement))


async def start_review(
    session: AsyncSession,
    *,
    root_run: ResearchRun,
    principal: Principal,
    settings: Settings,
) -> ResearchReview:
    review = await get_review(session, root_run_id=root_run.id, for_update=True)
    if review is None:
        review = ResearchReview(
            id=uuid4(),
            tenant_id=root_run.tenant_id,
            root_run_id=root_run.id,
            workspace_id=root_run.workspace_id,
            project_id=root_run.project_id,
            status=ReviewStatus.PENDING.value,
            max_attempts=settings.report_review_max_attempts,
            next_attempt_at=datetime.now(UTC),
            rules_version=REPORT_RULES_VERSION,
        )
        session.add(review)
    else:
        if review.status in {ReviewStatus.RUNNING.value, ReviewStatus.AWAITING_FOLLOWUP.value}:
            raise ConflictError("A review or follow-up research run is already active.")
        review.status = ReviewStatus.PENDING.value
        review.attempts = 0
        review.next_attempt_at = datetime.now(UTC)
        review.lease_expires_at = None
        review.worker_id = None
        review.error_code = None
        review.error_message = None
        review.completed_at = None
        review.max_attempts = settings.report_review_max_attempts
        review.rules_version = REPORT_RULES_VERSION
        review.review_round += 1
    record_event(
        session,
        tenant_id=root_run.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.RESEARCH_REVIEW_QUEUED,
        resource_type=ResourceType.RESEARCH_REVIEW,
        resource_id=review.id,
        details={"root_run_id": str(root_run.id), "manual": True},
    )
    await session.flush()
    await session.refresh(review)
    return review


async def get_report(
    session: AsyncSession,
    *,
    root_run_id: UUID,
    for_update: bool = False,
) -> Report | None:
    statement = tenant_scoped_select(Report).where(Report.root_run_id == root_run_id)
    if for_update:
        statement = statement.with_for_update()
    return cast(Report | None, await session.scalar(statement))


async def get_report_version(
    session: AsyncSession,
    *,
    report_id: UUID,
    version_id: UUID,
) -> ReportVersion | None:
    return cast(
        ReportVersion | None,
        await session.scalar(
            tenant_scoped_select(ReportVersion).where(
                ReportVersion.report_id == report_id,
                ReportVersion.id == version_id,
            )
        ),
    )


async def list_report_versions(
    session: AsyncSession,
    *,
    report_id: UUID,
) -> list[ReportVersion]:
    return list(
        (
            await session.scalars(
                tenant_scoped_select(ReportVersion)
                .where(ReportVersion.report_id == report_id)
                .order_by(ReportVersion.version_number.desc())
            )
        ).all()
    )


async def approve_report_version(
    session: AsyncSession,
    *,
    report: Report,
    version: ReportVersion,
    principal: Principal,
) -> ReportVersion:
    if version.report_id != report.id:
        raise NotFoundError("The report version was not found.")
    if report.current_version_id != version.id:
        raise ConflictError("Only the current report version can be approved.")
    if version.status != ReportVersionStatus.DRAFT.value:
        raise ConflictError("Only a draft report version can be approved.")
    if report_checksum(version.content_markdown, version.structured) != version.checksum_sha256:
        raise ConflictError("The report version failed its integrity check.")
    version.status = ReportVersionStatus.APPROVED.value
    version.approved_by = principal.user_id
    version.approved_at = datetime.now(UTC)
    report.status = ReportStatus.APPROVED.value
    record_event(
        session,
        tenant_id=report.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.REPORT_VERSION_APPROVED,
        resource_type=ResourceType.REPORT_VERSION,
        resource_id=version.id,
        details={
            "report_id": str(report.id),
            "version_number": version.version_number,
            "checksum": version.checksum_sha256,
        },
    )
    await session.flush()
    await session.refresh(version)
    await session.refresh(report)
    return version


async def reject_report_version(
    session: AsyncSession,
    *,
    report: Report,
    version: ReportVersion,
    principal: Principal,
    reason: str,
) -> ReportVersion:
    if version.report_id != report.id:
        raise NotFoundError("The report version was not found.")
    if report.current_version_id != version.id:
        raise ConflictError("Only the current report version can be rejected.")
    if version.status != ReportVersionStatus.DRAFT.value:
        raise ConflictError("Only a draft report version can be rejected.")
    version.status = ReportVersionStatus.REJECTED.value
    version.rejection_reason = reason.strip()[:500]
    report.status = ReportStatus.REJECTED.value
    record_event(
        session,
        tenant_id=report.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.REPORT_VERSION_REJECTED,
        resource_type=ResourceType.REPORT_VERSION,
        resource_id=version.id,
        details={
            "report_id": str(report.id),
            "version_number": version.version_number,
            "reason": version.rejection_reason,
        },
    )
    await session.flush()
    await session.refresh(version)
    await session.refresh(report)
    return version


async def create_export_job(
    session: AsyncSession,
    *,
    report: Report,
    version: ReportVersion,
    export_format: ExportFormat,
    principal: Principal,
    settings: Settings,
) -> ReportExportJob:
    if version.report_id != report.id:
        raise NotFoundError("The report version was not found.")
    if version.status != ReportVersionStatus.APPROVED.value:
        raise ConflictError("Only an approved report version can be exported.")
    job = ReportExportJob(
        id=uuid4(),
        tenant_id=report.tenant_id,
        report_id=report.id,
        version_id=version.id,
        format=export_format.value,
        status=ExportStatus.PENDING.value,
        attempts=0,
        max_attempts=settings.report_export_max_attempts,
        next_attempt_at=datetime.now(UTC),
        requested_by=principal.user_id,
    )
    session.add(job)
    record_event(
        session,
        tenant_id=report.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.REPORT_EXPORT_QUEUED,
        resource_type=ResourceType.REPORT_EXPORT,
        resource_id=job.id,
        details={
            "report_id": str(report.id),
            "version_id": str(version.id),
            "format": export_format.value,
        },
    )
    await session.flush()
    await session.refresh(job)
    return job


async def list_export_jobs(
    session: AsyncSession,
    *,
    report_id: UUID,
    limit: int,
    offset: int,
) -> list[ReportExportJob]:
    return list(
        (
            await session.scalars(
                tenant_scoped_select(ReportExportJob)
                .where(ReportExportJob.report_id == report_id)
                .order_by(ReportExportJob.created_at.desc(), ReportExportJob.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )


async def get_export_job(
    session: AsyncSession,
    *,
    report_id: UUID,
    job_id: UUID,
) -> ReportExportJob | None:
    return cast(
        ReportExportJob | None,
        await session.scalar(
            tenant_scoped_select(ReportExportJob).where(
                ReportExportJob.report_id == report_id,
                ReportExportJob.id == job_id,
            )
        ),
    )


async def list_claims_with_citations(
    session: AsyncSession,
    *,
    root_run_id: UUID,
    limit: int,
    offset: int,
) -> list[tuple[ResearchClaim, list[tuple[ResearchCitation, ResearchEvidence, ResearchSource]]]]:
    claims = list(
        (
            await session.scalars(
                tenant_scoped_select(ResearchClaim)
                .where(ResearchClaim.root_run_id == root_run_id)
                .order_by(ResearchClaim.created_at, ResearchClaim.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    if not claims:
        return []
    claim_ids = [claim.id for claim in claims]
    rows = (
        await session.execute(
            select(ResearchCitation, ResearchEvidence, ResearchSource)
            .join(ResearchEvidence, ResearchEvidence.id == ResearchCitation.evidence_id)
            .join(ResearchSource, ResearchSource.id == ResearchEvidence.source_id)
            .where(
                ResearchCitation.tenant_id == ResearchEvidence.tenant_id,
                ResearchEvidence.tenant_id == ResearchSource.tenant_id,
                ResearchCitation.claim_id.in_(claim_ids),
            )
            .order_by(ResearchCitation.created_at, ResearchCitation.id)
        )
    ).all()
    by_claim: dict[UUID, list[tuple[ResearchCitation, ResearchEvidence, ResearchSource]]] = {}
    for citation, evidence, source in rows:
        by_claim.setdefault(citation.claim_id, []).append((citation, evidence, source))
    return [(claim, by_claim.get(claim.id, [])) for claim in claims]


__all__ = [
    "ReviewServices",
    "approve_report_version",
    "build_review_services",
    "create_export_job",
    "get_export_job",
    "get_report",
    "get_report_version",
    "get_review",
    "list_claims_with_citations",
    "list_export_jobs",
    "list_report_versions",
    "reject_report_version",
    "require_services",
    "root_run_id",
    "start_review",
]
