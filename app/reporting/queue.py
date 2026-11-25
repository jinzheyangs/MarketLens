"""调研运行进入终态时排队证据审查。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.enums import ResearchRunKind, ResearchRunStatus
from app.auth.enums import AuditAction, ResourceType
from app.core.config import Settings
from app.db.models import ResearchReview, ResearchRun
from app.db.tenant_scope import tenant_scoped_select
from app.reporting.enums import REPORT_RULES_VERSION, ReviewStatus
from app.services.audit import record_event


async def enqueue_review_for_terminal_run(
    session: AsyncSession,
    *,
    run: ResearchRun,
    settings: Settings,
) -> ResearchReview | None:
    """Create a primary review or wake a review waiting on a follow-up run.

    The caller owns the run transaction. Review rows are unique per root run;
    duplicate enqueue attempts are harmless.
    """
    if not settings.report_enabled:
        return None

    if run.run_kind == ResearchRunKind.FOLLOWUP.value:
        if run.root_run_id is None:
            return None
        review = await session.scalar(
            tenant_scoped_select(ResearchReview)
            .where(ResearchReview.root_run_id == run.root_run_id)
            .with_for_update()
        )
        if review is None:
            return None
        if review.status in {
            ReviewStatus.AWAITING_FOLLOWUP.value,
            ReviewStatus.FAILED.value,
        }:
            review.status = ReviewStatus.PENDING.value
            review.next_attempt_at = datetime.now(UTC)
            review.lease_expires_at = None
            review.worker_id = None
            review.attempts = 0
            review.error_code = None
            review.error_message = None
            record_event(
                session,
                tenant_id=run.tenant_id,
                actor_user_id=run.requested_by,
                action=AuditAction.RESEARCH_REVIEW_QUEUED,
                resource_type=ResourceType.RESEARCH_REVIEW,
                resource_id=review.id,
                details={
                    "root_run_id": str(review.root_run_id),
                    "followup_run_id": str(run.id),
                },
            )
            await session.flush()
            return review
        return review

    if (
        run.run_kind != ResearchRunKind.PRIMARY.value
        or run.status not in {ResearchRunStatus.COMPLETED.value, ResearchRunStatus.PARTIAL.value}
        or not settings.report_auto_review
    ):
        return None

    existing = await session.scalar(
        tenant_scoped_select(ResearchReview).where(ResearchReview.root_run_id == run.id)
    )
    if existing is not None:
        return existing

    review = ResearchReview(
        id=uuid4(),
        tenant_id=run.tenant_id,
        root_run_id=run.id,
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        status=ReviewStatus.PENDING.value,
        max_attempts=settings.report_review_max_attempts,
        next_attempt_at=datetime.now(UTC),
        rules_version=REPORT_RULES_VERSION,
    )
    try:
        async with session.begin_nested():
            session.add(review)
            await session.flush()
    except IntegrityError:
        return cast(
            ResearchReview | None,
            await session.scalar(
                tenant_scoped_select(ResearchReview).where(ResearchReview.root_run_id == run.id)
            ),
        )

    record_event(
        session,
        tenant_id=run.tenant_id,
        actor_user_id=run.requested_by,
        action=AuditAction.RESEARCH_REVIEW_QUEUED,
        resource_type=ResourceType.RESEARCH_REVIEW,
        resource_id=review.id,
        details={"root_run_id": str(run.id), "auto": True},
    )
    return review


def root_run_id(run: ResearchRun) -> UUID:
    return run.root_run_id or run.id


__all__ = ["enqueue_review_for_terminal_run", "root_run_id"]
