"""Rule-based evidence review with mandatory citation/source checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.evidence import quote_is_supported
from app.core.config import Settings
from app.db.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    ResearchCitation,
    ResearchClaim,
    ResearchEvidence,
    ResearchSource,
    SourceSnapshot,
    WebSource,
)
from app.db.tenant_scope import tenant_scoped_select
from app.reporting.enums import CitationRelation, ClaimStatus
from app.reporting.rules import (
    claim_conflicts,
    is_stale,
    sanitize_text,
    support_score,
    truncate,
)


@dataclass(slots=True)
class CitationAssessment:
    citation: ResearchCitation
    relation: CitationRelation
    support_score: float
    reason: str
    available: bool
    stale: bool
    injection_suspected: bool
    independence_key: str


@dataclass(slots=True)
class ClaimAssessment:
    claim: ResearchClaim
    status: ClaimStatus
    summary: str
    independent_source_count: int
    supported_citation_count: int
    stale_evidence_count: int
    conflict_reason: str | None
    citations: list[CitationAssessment]


@dataclass(slots=True)
class ReviewResult:
    claims: list[ClaimAssessment]
    followup_questions: list[str]
    source_count: int
    evidence_count: int
    supported_count: int
    partial_count: int
    insufficient_count: int
    conflicted_count: int
    invalid_count: int


@dataclass(frozen=True, slots=True)
class _EvidenceState:
    available: bool
    stale: bool
    text: str
    title: str
    reason: str | None


async def _load_evidence_state(
    session: AsyncSession,
    *,
    evidence: ResearchEvidence,
    source: ResearchSource,
    now: datetime,
    settings: Settings,
) -> _EvidenceState:
    fresh = not is_stale(
        source.observed_at,
        now=now,
        max_age_days=settings.report_max_evidence_age_days,
    )
    if source.source_kind == "external_source":
        web_source = await session.scalar(
            tenant_scoped_select(WebSource).where(
                WebSource.id == source.external_source_id,
                WebSource.status == "active",
            )
        )
        snapshot = None
        if source.source_snapshot_id is not None:
            snapshot = await session.scalar(
                tenant_scoped_select(SourceSnapshot).where(
                    SourceSnapshot.id == source.source_snapshot_id,
                    SourceSnapshot.status == "active",
                )
            )
        if web_source is None or snapshot is None or not snapshot.extracted_text:
            return _EvidenceState(
                available=False,
                stale=False,
                text="",
                title=source.title,
                reason="The external source or snapshot is no longer available.",
            )
        return _EvidenceState(
            available=True,
            stale=(web_source.latest_snapshot_id != snapshot.id or not fresh),
            text=snapshot.extracted_text,
            title=web_source.title or source.title,
            reason=None,
        )

    document = await session.scalar(
        tenant_scoped_select(Document).where(
            Document.id == source.external_source_id,
            Document.status == "active",
        )
    )
    version = None
    if source.document_version_id is not None:
        version = await session.scalar(
            tenant_scoped_select(DocumentVersion).where(
                DocumentVersion.id == source.document_version_id,
                DocumentVersion.document_id == source.external_source_id,
            )
        )
    chunk = None
    if evidence.chunk_id is not None:
        chunk = await session.scalar(
            tenant_scoped_select(DocumentChunk).where(
                DocumentChunk.id == evidence.chunk_id,
                DocumentChunk.document_id == source.external_source_id,
            )
        )
    if document is None or version is None or chunk is None:
        return _EvidenceState(
            available=False,
            stale=False,
            text="",
            title=source.title,
            reason="The cited document version or chunk is no longer available.",
        )
    return _EvidenceState(
        available=True,
        stale=(document.active_version_id != version.id or not fresh),
        text=chunk.content,
        title=document.title,
        reason=None,
    )


def _followup_question(status: ClaimStatus, claim: str) -> str:
    if status is ClaimStatus.CONFLICTED:
        return f"Resolve the conflicting evidence about: {claim}"
    if status is ClaimStatus.INVALID:
        return f"Find an active, independent source that directly supports or refutes: {claim}"
    if status is ClaimStatus.INSUFFICIENT:
        return f"Find evidence that directly supports or refutes: {claim}"
    return f"Find independent corroboration for: {claim}"


async def review_claims(
    session: AsyncSession,
    *,
    root_run_id: UUID,
    settings: Settings,
    now: datetime | None = None,
) -> ReviewResult:
    reviewed_at = now or datetime.now(UTC)
    claims = list(
        (
            await session.scalars(
                tenant_scoped_select(ResearchClaim)
                .where(ResearchClaim.root_run_id == root_run_id)
                .order_by(ResearchClaim.created_at, ResearchClaim.id)
            )
        ).all()
    )
    rows = (
        await session.execute(
            select(ResearchCitation, ResearchEvidence, ResearchSource)
            .join(ResearchEvidence, ResearchEvidence.id == ResearchCitation.evidence_id)
            .join(ResearchSource, ResearchSource.id == ResearchEvidence.source_id)
            .where(
                ResearchCitation.tenant_id == ResearchEvidence.tenant_id,
                ResearchEvidence.tenant_id == ResearchSource.tenant_id,
                ResearchEvidence.root_run_id == root_run_id,
                ResearchSource.root_run_id == root_run_id,
            )
            .order_by(ResearchCitation.created_at, ResearchCitation.id)
        )
    ).all()

    by_claim: dict[UUID, list[tuple[ResearchCitation, ResearchEvidence, ResearchSource]]] = {}
    for citation, evidence, source in rows:
        by_claim.setdefault(citation.claim_id, []).append((citation, evidence, source))

    assessments: list[ClaimAssessment] = []
    conflict_map = claim_conflicts(
        [(str(claim.id), claim.claim) for claim in claims],
        min_shared_tokens=settings.report_conflict_min_shared_tokens,
    )

    for claim in claims:
        citation_assessments: list[CitationAssessment] = []
        for citation, evidence, source in by_claim.get(claim.id, []):
            state = await _load_evidence_state(
                session,
                evidence=evidence,
                source=source,
                now=reviewed_at,
                settings=settings,
            )
            if not state.available:
                relation = CitationRelation.UNAVAILABLE
                score = 0.0
                reason = state.reason or "The source is unavailable."
            elif not quote_is_supported(evidence.quote, state.text):
                relation = CitationRelation.UNAVAILABLE
                score = 0.0
                reason = "The evidence quote no longer occurs in the cited source."
            else:
                score = support_score(claim.claim, evidence.quote, state.title)
                if score < settings.report_min_support_score:
                    relation = CitationRelation.INSUFFICIENT
                    reason = "The evidence does not sufficiently overlap the claim."
                else:
                    relation = CitationRelation.SUPPORTS
                    reason = "Citation and quote are present in the source."
            citation.relation = relation.value
            citation.support_score = score
            citation.review_reason = reason[:500]
            citation_assessments.append(
                CitationAssessment(
                    citation=citation,
                    relation=relation,
                    support_score=score,
                    reason=reason,
                    available=state.available,
                    stale=state.stale,
                    injection_suspected=evidence.injection_suspected,
                    independence_key=source.independence_key,
                )
            )

        supported = [
            item for item in citation_assessments if item.relation is CitationRelation.SUPPORTS
        ]
        independent = {item.independence_key for item in supported}
        stale_count = sum(1 for item in supported if item.stale)
        suspicious_count = sum(1 for item in supported if item.injection_suspected)
        conflict_reason = conflict_map.get(str(claim.id))

        if not citation_assessments:
            status = ClaimStatus.INVALID
            summary = "No citation is attached to this claim."
        elif not supported:
            if all(item.relation is CitationRelation.UNAVAILABLE for item in citation_assessments):
                status = ClaimStatus.INVALID
                summary = "None of the attached citations can be verified."
            else:
                status = ClaimStatus.INSUFFICIENT
                summary = "Available evidence does not sufficiently support this claim."
        elif conflict_reason:
            status = ClaimStatus.CONFLICTED
            summary = conflict_reason
        elif (
            stale_count > 0
            or suspicious_count > 0
            or len(independent) < settings.report_min_independent_sources
            or len(supported) < len(citation_assessments)
        ):
            status = ClaimStatus.PARTIALLY_SUPPORTED
            reasons: list[str] = []
            if len(independent) < settings.report_min_independent_sources:
                reasons.append("insufficient independent sources")
            if stale_count:
                reasons.append("stale evidence")
            if suspicious_count:
                reasons.append("prompt-injection heuristics flagged evidence")
            if len(supported) < len(citation_assessments):
                reasons.append("some citations were not verifiable")
            summary = "Partially supported: " + ", ".join(reasons) + "."
        else:
            status = ClaimStatus.SUPPORTED
            summary = "Supported by verifiable evidence."

        claim.status = status.value
        claim.citation_count = len(citation_assessments)
        claim.supported_citation_count = len(supported)
        claim.independent_source_count = len(independent)
        claim.stale_evidence_count = stale_count
        claim.conflict_reason = conflict_reason[:500] if conflict_reason else None
        claim.review_summary = summary[:500]
        claim.reviewed_at = reviewed_at
        assessments.append(
            ClaimAssessment(
                claim=claim,
                status=status,
                summary=summary,
                independent_source_count=len(independent),
                supported_citation_count=len(supported),
                stale_evidence_count=stale_count,
                conflict_reason=conflict_reason,
                citations=citation_assessments,
            )
        )

    await session.flush()

    followup: list[str] = []
    for assessment in assessments:
        needs_followup = assessment.status in {
            ClaimStatus.INSUFFICIENT,
            ClaimStatus.INVALID,
            ClaimStatus.CONFLICTED,
        } or (
            assessment.status is ClaimStatus.PARTIALLY_SUPPORTED
            and assessment.independent_source_count < settings.report_min_independent_sources
        )
        if not needs_followup:
            continue
        question = truncate(
            sanitize_text(
                _followup_question(assessment.status, assessment.claim.claim),
                max_chars=700,
            ),
            700,
        )
        if question not in followup:
            followup.append(question)
        if len(followup) >= settings.report_max_followup_questions:
            break

    counts = dict.fromkeys(ClaimStatus, 0)
    for assessment in assessments:
        counts[assessment.status] += 1
    source_count = len({source.id for _, _, source in rows})
    evidence_count = len({evidence.id for _, evidence, _ in rows})
    return ReviewResult(
        claims=assessments,
        followup_questions=followup,
        source_count=source_count,
        evidence_count=evidence_count,
        supported_count=counts[ClaimStatus.SUPPORTED],
        partial_count=counts[ClaimStatus.PARTIALLY_SUPPORTED],
        insufficient_count=counts[ClaimStatus.INSUFFICIENT],
        conflicted_count=counts[ClaimStatus.CONFLICTED],
        invalid_count=counts[ClaimStatus.INVALID],
    )


__all__ = [
    "CitationAssessment",
    "ClaimAssessment",
    "ReviewResult",
    "review_claims",
]
