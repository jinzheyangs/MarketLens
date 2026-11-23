"""Derive distinct source, evidence, claim, and citation entities from findings."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.evidence import normalize_text
from app.core.config import Settings
from app.db.models import (
    ResearchCitation,
    ResearchClaim,
    ResearchEvidence,
    ResearchFinding,
    ResearchRun,
    ResearchSource,
)
from app.db.tenant_scope import tenant_scoped_select
from app.reporting.enums import CitationRelation, ClaimStatus
from app.reporting.rules import stable_key


@dataclass(frozen=True, slots=True)
class EntityStats:
    source_count: int
    evidence_count: int
    claim_count: int
    citation_count: int


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    try:
        hostname = urlparse(url).hostname
    except ValueError:
        return None
    return hostname.lower() if hostname else None


async def derive_entities(
    session: AsyncSession,
    *,
    root_run: ResearchRun,
    lineage_run_ids: Sequence[UUID],
    settings: Settings,
) -> EntityStats:
    findings = list(
        (
            await session.scalars(
                tenant_scoped_select(ResearchFinding)
                .where(ResearchFinding.run_id.in_(list(lineage_run_ids)))
                .order_by(ResearchFinding.created_at, ResearchFinding.id)
                .limit(settings.report_max_lineage_findings)
            )
        ).all()
    )

    for finding in findings:
        try:
            source_id = UUID(str(finding.source_id))
        except (TypeError, ValueError):
            continue
        source_key = stable_key(
            "source",
            finding.source_kind,
            str(source_id),
            str(finding.document_version_id or ""),
            str(finding.source_snapshot_id or ""),
        )
        independence_key = (
            _domain(finding.source_url) or str(source_id)
            if finding.source_kind == "external_source"
            else str(source_id)
        )

        source = await session.scalar(
            tenant_scoped_select(ResearchSource).where(
                ResearchSource.root_run_id == root_run.id,
                ResearchSource.source_key == source_key,
            )
        )
        if source is None:
            source = ResearchSource(
                tenant_id=root_run.tenant_id,
                root_run_id=root_run.id,
                origin_run_id=finding.run_id,
                source_key=source_key,
                independence_key=independence_key[:128],
                source_kind=finding.source_kind,
                external_source_id=source_id,
                document_version_id=finding.document_version_id,
                source_snapshot_id=finding.source_snapshot_id,
                title=(finding.source_title or "Untitled source")[:500],
                url=finding.source_url,
                domain=_domain(finding.source_url),
                observed_at=finding.observed_at,
            )
            session.add(source)
            await session.flush()
        elif finding.observed_at > source.observed_at:
            source.observed_at = finding.observed_at

        quote = normalize_text(finding.evidence)
        evidence_key = stable_key(
            "evidence",
            source_key,
            str(finding.chunk_id or ""),
            str(finding.page_number or ""),
            quote,
        )
        evidence = await session.scalar(
            tenant_scoped_select(ResearchEvidence).where(
                ResearchEvidence.root_run_id == root_run.id,
                ResearchEvidence.evidence_key == evidence_key,
            )
        )
        if evidence is None:
            evidence = ResearchEvidence(
                tenant_id=root_run.tenant_id,
                root_run_id=root_run.id,
                source_id=source.id,
                origin_run_id=finding.run_id,
                origin_finding_id=finding.id,
                evidence_key=evidence_key,
                quote=finding.evidence,
                evidence_sha256=hashlib.sha256(quote.encode()).hexdigest(),
                observed_at=finding.observed_at,
                injection_suspected=finding.injection_suspected,
                chunk_id=finding.chunk_id,
                page_number=finding.page_number,
                paragraph_number=finding.paragraph_number,
                start_offset=finding.start_offset,
                end_offset=finding.end_offset,
            )
            session.add(evidence)
            await session.flush()

        claim_text = normalize_text(finding.claim)
        scope = normalize_text(finding.scope)[:500]
        claim_key = stable_key("claim", claim_text.casefold(), scope.casefold())
        claim = await session.scalar(
            tenant_scoped_select(ResearchClaim).where(
                ResearchClaim.root_run_id == root_run.id,
                ResearchClaim.claim_key == claim_key,
            )
        )
        if claim is None:
            current_claim_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ResearchClaim)
                    .where(
                        ResearchClaim.tenant_id == root_run.tenant_id,
                        ResearchClaim.root_run_id == root_run.id,
                    )
                )
                or 0
            )
            if current_claim_count >= settings.report_max_claims:
                continue
            claim = ResearchClaim(
                tenant_id=root_run.tenant_id,
                root_run_id=root_run.id,
                origin_run_id=finding.run_id,
                origin_finding_id=finding.id,
                claim_key=claim_key,
                agent_role=finding.agent_role,
                claim=claim_text,
                scope=scope or "Not specified.",
                limitations=finding.limitations,
                original_confidence=finding.confidence,
                status=ClaimStatus.CANDIDATE.value,
            )
            session.add(claim)
            await session.flush()

        citation = await session.scalar(
            tenant_scoped_select(ResearchCitation).where(
                ResearchCitation.claim_id == claim.id,
                ResearchCitation.evidence_id == evidence.id,
            )
        )
        if citation is None:
            claim_citation_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ResearchCitation)
                    .where(
                        ResearchCitation.tenant_id == root_run.tenant_id,
                        ResearchCitation.claim_id == claim.id,
                    )
                )
                or 0
            )
            if claim_citation_count >= settings.report_max_evidence_per_claim:
                continue
            session.add(
                ResearchCitation(
                    tenant_id=root_run.tenant_id,
                    claim_id=claim.id,
                    evidence_id=evidence.id,
                    origin_run_id=finding.run_id,
                    origin_finding_id=finding.id,
                    relation=CitationRelation.SUPPORTS.value,
                    support_score=0.0,
                )
            )
            await session.flush()

    await session.flush()
    source_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ResearchSource)
            .where(
                ResearchSource.tenant_id == root_run.tenant_id,
                ResearchSource.root_run_id == root_run.id,
            )
        )
        or 0
    )
    evidence_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ResearchEvidence)
            .where(
                ResearchEvidence.tenant_id == root_run.tenant_id,
                ResearchEvidence.root_run_id == root_run.id,
            )
        )
        or 0
    )
    claim_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ResearchClaim)
            .where(ResearchClaim.root_run_id == root_run.id)
        )
        or 0
    )
    citation_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ResearchCitation)
            .join(ResearchClaim, ResearchClaim.id == ResearchCitation.claim_id)
            .where(
                ResearchCitation.tenant_id == root_run.tenant_id,
                ResearchClaim.tenant_id == root_run.tenant_id,
                ResearchClaim.root_run_id == root_run.id,
            )
        )
        or 0
    )
    return EntityStats(
        source_count=source_count,
        evidence_count=evidence_count,
        claim_count=claim_count,
        citation_count=citation_count,
    )


__all__ = ["EntityStats", "derive_entities"]
