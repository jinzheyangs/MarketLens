"""Evidence review, report, approval, and export APIs."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.enums import TERMINAL_RUN_STATUSES, AgentRole, FindingSourceKind
from app.api.deps import get_session
from app.api.v1.research_scope import require_root_run
from app.auth.authorization import AuthorizationService
from app.auth.dependencies import get_authorization, get_principal, get_settings_dep
from app.auth.enums import Permission
from app.auth.principal import Principal
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ServiceUnavailableError
from app.db.models import Report, ReportVersion, ResearchReview, ResearchRun
from app.rag.errors import ObjectStorageError
from app.reporting.enums import CitationRelation, ClaimStatus, ExportFormat, ExportStatus
from app.reporting.service import (
    ReviewServices,
    approve_report_version,
    create_export_job,
    get_export_job,
    get_report,
    get_report_version,
    get_review,
    list_claims_with_citations,
    list_export_jobs,
    list_report_versions,
    reject_report_version,
    start_review,
)
from app.schemas.reporting import (
    RejectReportVersionRequest,
    ReportExportCreate,
    ReportExportJobRead,
    ReportRead,
    ReportVersionRead,
    ReportVersionSummary,
    ResearchReviewRead,
    ReviewCitationRead,
    ReviewClaimRead,
    ReviewEvidenceView,
    ReviewSourceView,
)


def get_review_services(
    request: Request,
    principal: Principal = Depends(get_principal),
) -> ReviewServices:
    del principal
    services = getattr(request.app.state, "review_services", None)
    if services is None:
        raise ServiceUnavailableError("Reports are not configured.")
    return cast(ReviewServices, services)


router = APIRouter(
    tags=["reports"],
    dependencies=[Depends(get_review_services)],
)


async def _require_review(
    session: AsyncSession,
    root_run: ResearchRun,
) -> ResearchReview:
    review = await get_review(session, root_run_id=root_run.id)
    if review is None:
        raise NotFoundError("The research review was not found.")
    return review


async def _require_report(
    session: AsyncSession,
    root_run: ResearchRun,
    *,
    for_update: bool = False,
) -> Report:
    report = await get_report(session, root_run_id=root_run.id, for_update=for_update)
    if report is None:
        raise NotFoundError("The report was not found.")
    return report


async def _require_version(
    session: AsyncSession,
    report: Report,
    version_id: UUID,
) -> ReportVersion:
    version = await get_report_version(
        session,
        report_id=report.id,
        version_id=version_id,
    )
    if version is None:
        raise NotFoundError("The report version was not found.")
    return version


@router.post(
    "/workspaces/{workspace_id}/research-runs/{run_id}/review",
    response_model=ResearchReviewRead,
    status_code=202,
    summary="Queue or restart evidence review for a research run",
)
async def queue_review(
    workspace_id: UUID,
    run_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> ResearchReviewRead:
    root = await require_root_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.RESEARCH_RUN_CONTROL,
    )
    if root.status not in {status.value for status in TERMINAL_RUN_STATUSES}:
        raise ConflictError("Only a terminal research run can be reviewed.")
    review = await start_review(
        session,
        root_run=root,
        principal=principal,
        settings=settings,
    )
    return ResearchReviewRead.model_validate(review)


@router.get(
    "/workspaces/{workspace_id}/research-runs/{run_id}/review",
    response_model=ResearchReviewRead,
    summary="Get evidence-review status",
)
async def get_research_review(
    workspace_id: UUID,
    run_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ResearchReviewRead:
    root = await require_root_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.REPORT_READ,
    )
    review = await _require_review(session, root)
    return ResearchReviewRead.model_validate(review)


@router.get(
    "/workspaces/{workspace_id}/research-runs/{run_id}/review/claims",
    response_model=list[ReviewClaimRead],
    summary="List reviewed claims with citations and evidence",
)
async def list_review_claims(
    workspace_id: UUID,
    run_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ReviewClaimRead]:
    root = await require_root_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.REPORT_READ,
    )
    await _require_review(session, root)
    rows = await list_claims_with_citations(
        session,
        root_run_id=root.id,
        limit=limit,
        offset=offset,
    )
    claims: list[ReviewClaimRead] = []
    for claim, citations in rows:
        claims.append(
            ReviewClaimRead(
                id=claim.id,
                claim=claim.claim,
                agent_role=AgentRole(claim.agent_role),
                status=ClaimStatus(claim.status),
                scope=claim.scope,
                limitations=claim.limitations,
                original_confidence=claim.original_confidence,
                citation_count=claim.citation_count,
                supported_citation_count=claim.supported_citation_count,
                independent_source_count=claim.independent_source_count,
                stale_evidence_count=claim.stale_evidence_count,
                review_summary=claim.review_summary,
                conflict_reason=claim.conflict_reason,
                reviewed_at=claim.reviewed_at,
                citations=[
                    ReviewCitationRead(
                        relation=CitationRelation(citation.relation),
                        support_score=citation.support_score,
                        review_reason=citation.review_reason,
                        evidence=ReviewEvidenceView(
                            id=evidence.id,
                            quote=evidence.quote,
                            observed_at=evidence.observed_at,
                            injection_suspected=evidence.injection_suspected,
                            page_number=evidence.page_number,
                            paragraph_number=evidence.paragraph_number,
                        ),
                        source=ReviewSourceView(
                            id=source.id,
                            source_kind=FindingSourceKind(source.source_kind),
                            title=source.title,
                            url=source.url,
                            domain=source.domain,
                            observed_at=source.observed_at,
                        ),
                    )
                    for citation, evidence, source in citations
                ],
            )
        )
    return claims


@router.get(
    "/workspaces/{workspace_id}/research-runs/{run_id}/report",
    response_model=ReportRead,
    summary="Get the current report header",
)
async def get_research_report(
    workspace_id: UUID,
    run_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ReportRead:
    root = await require_root_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.REPORT_READ,
    )
    report = await _require_report(session, root)
    return ReportRead.model_validate(report)


@router.get(
    "/workspaces/{workspace_id}/research-runs/{run_id}/report/versions",
    response_model=list[ReportVersionSummary],
    summary="List report versions",
)
async def list_research_report_versions(
    workspace_id: UUID,
    run_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> list[ReportVersionSummary]:
    root = await require_root_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.REPORT_READ,
    )
    report = await _require_report(session, root)
    versions = await list_report_versions(session, report_id=report.id)
    return [ReportVersionSummary.model_validate(version) for version in versions]


@router.get(
    "/workspaces/{workspace_id}/research-runs/{run_id}/report/versions/{version_id}",
    response_model=ReportVersionRead,
    summary="Get an immutable report version",
)
async def get_research_report_version(
    workspace_id: UUID,
    run_id: UUID,
    version_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ReportVersionRead:
    root = await require_root_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.REPORT_READ,
    )
    report = await _require_report(session, root)
    version = await _require_version(session, report, version_id)
    return ReportVersionRead.model_validate(version)


@router.post(
    "/workspaces/{workspace_id}/research-runs/{run_id}/report/versions/{version_id}/approve",
    response_model=ReportVersionRead,
    summary="Approve the current draft report version",
)
async def approve_research_report_version(
    workspace_id: UUID,
    run_id: UUID,
    version_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ReportVersionRead:
    root = await require_root_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.REPORT_APPROVE,
    )
    report = await _require_report(session, root, for_update=True)
    version = await _require_version(session, report, version_id)
    version = await approve_report_version(
        session,
        report=report,
        version=version,
        principal=principal,
    )
    return ReportVersionRead.model_validate(version)


@router.post(
    "/workspaces/{workspace_id}/research-runs/{run_id}/report/versions/{version_id}/reject",
    response_model=ReportVersionRead,
    summary="Reject the current draft report version",
)
async def reject_research_report_version(
    workspace_id: UUID,
    run_id: UUID,
    version_id: UUID,
    payload: RejectReportVersionRequest,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ReportVersionRead:
    root = await require_root_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.REPORT_APPROVE,
    )
    report = await _require_report(session, root, for_update=True)
    version = await _require_version(session, report, version_id)
    version = await reject_report_version(
        session,
        report=report,
        version=version,
        principal=principal,
        reason=payload.reason,
    )
    return ReportVersionRead.model_validate(version)


@router.post(
    "/workspaces/{workspace_id}/research-runs/{run_id}/report/versions/{version_id}/exports",
    response_model=ReportExportJobRead,
    status_code=202,
    summary="Queue an approved report version export",
)
async def queue_report_export(
    workspace_id: UUID,
    run_id: UUID,
    version_id: UUID,
    payload: ReportExportCreate,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> ReportExportJobRead:
    root = await require_root_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.REPORT_EXPORT,
    )
    report = await _require_report(session, root)
    version = await _require_version(session, report, version_id)
    job = await create_export_job(
        session,
        report=report,
        version=version,
        export_format=payload.format,
        principal=principal,
        settings=settings,
    )
    return ReportExportJobRead.model_validate(job)


@router.get(
    "/workspaces/{workspace_id}/research-runs/{run_id}/report/exports",
    response_model=list[ReportExportJobRead],
    summary="List report export jobs",
)
async def list_research_report_exports(
    workspace_id: UUID,
    run_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ReportExportJobRead]:
    root = await require_root_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.REPORT_READ,
    )
    report = await _require_report(session, root)
    jobs = await list_export_jobs(
        session,
        report_id=report.id,
        limit=limit,
        offset=offset,
    )
    return [ReportExportJobRead.model_validate(job) for job in jobs]


@router.get(
    "/workspaces/{workspace_id}/research-runs/{run_id}/report/exports/{job_id}",
    response_model=ReportExportJobRead,
    summary="Get a report export job",
)
async def get_research_report_export(
    workspace_id: UUID,
    run_id: UUID,
    job_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> ReportExportJobRead:
    root = await require_root_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.REPORT_READ,
    )
    report = await _require_report(session, root)
    job = await get_export_job(session, report_id=report.id, job_id=job_id)
    if job is None:
        raise NotFoundError("The export job was not found.")
    return ReportExportJobRead.model_validate(job)


@router.get(
    "/workspaces/{workspace_id}/research-runs/{run_id}/report/exports/{job_id}/download",
    summary="Download a completed report export",
)
async def download_report_export(
    workspace_id: UUID,
    run_id: UUID,
    job_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    services: ReviewServices = Depends(get_review_services),
) -> Response:
    root = await require_root_run(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        authz=authz,
        permission=Permission.REPORT_READ,
    )
    report = await _require_report(session, root)
    job = await get_export_job(session, report_id=report.id, job_id=job_id)
    if job is None:
        raise NotFoundError("The export job was not found.")
    if job.status != ExportStatus.SUCCEEDED.value or job.object_key is None:
        raise ConflictError("The export is not ready.")
    version = await _require_version(session, report, job.version_id)
    try:
        data = await services.storage.get(job.object_key)
    except ObjectStorageError as exc:
        raise ServiceUnavailableError("The export object is unavailable.") from exc
    extension = "docx" if job.format == ExportFormat.DOCX.value else "md"
    filename = f"research-report-v{version.version_number}.{extension}"
    return Response(
        content=data,
        media_type=job.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
