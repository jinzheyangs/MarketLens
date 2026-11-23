"""Deterministic, citation-indexed report generation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import (
    ResearchCitation,
    ResearchClaim,
    ResearchEvidence,
    ResearchReview,
    ResearchRun,
    ResearchSource,
)
from app.db.tenant_scope import tenant_scoped_select
from app.reporting.enums import ClaimStatus
from app.reporting.rules import sanitize_text, truncate

_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]<>])")


def _md(value: str, *, max_chars: int = 2_000) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", sanitize_text(value, max_chars=max_chars))


def _date(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.astimezone(UTC).date().isoformat()


def report_checksum(markdown: str, structured: dict[str, Any]) -> str:
    canonical = json.dumps(structured, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256()
    digest.update(markdown.encode("utf-8"))
    digest.update(b"|")
    digest.update(canonical.encode("utf-8"))
    return digest.hexdigest()


@dataclass(slots=True)
class ReportDraft:
    title: str
    markdown: str
    structured: dict[str, Any]
    source_list: list[dict[str, Any]]
    citation_index: list[dict[str, Any]]


def _claim_sort_key(claim: ResearchClaim) -> tuple[int, float, str]:
    rank = {
        ClaimStatus.SUPPORTED.value: 0,
        ClaimStatus.PARTIALLY_SUPPORTED.value: 1,
        ClaimStatus.CONFLICTED.value: 2,
        ClaimStatus.INSUFFICIENT.value: 3,
        ClaimStatus.INVALID.value: 4,
    }.get(claim.status, 5)
    return (rank, -claim.original_confidence, str(claim.id))


async def build_report_draft(
    session: AsyncSession,
    *,
    root_run: ResearchRun,
    review: ResearchReview,
    lineage_runs: list[ResearchRun],
    settings: Settings,
) -> ReportDraft:
    sources = list(
        (
            await session.scalars(
                tenant_scoped_select(ResearchSource)
                .where(ResearchSource.root_run_id == root_run.id)
                .order_by(ResearchSource.created_at, ResearchSource.id)
            )
        ).all()
    )
    evidence_rows = list(
        (
            await session.scalars(
                tenant_scoped_select(ResearchEvidence)
                .where(ResearchEvidence.root_run_id == root_run.id)
                .order_by(ResearchEvidence.created_at, ResearchEvidence.id)
            )
        ).all()
    )
    claims = list(
        (
            await session.scalars(
                tenant_scoped_select(ResearchClaim)
                .where(ResearchClaim.root_run_id == root_run.id)
                .order_by(ResearchClaim.created_at, ResearchClaim.id)
            )
        ).all()
    )
    claims.sort(key=_claim_sort_key)
    citation_rows = (
        await session.execute(
            select(ResearchCitation, ResearchEvidence, ResearchSource)
            .join(ResearchEvidence, ResearchEvidence.id == ResearchCitation.evidence_id)
            .join(ResearchSource, ResearchSource.id == ResearchEvidence.source_id)
            .where(
                ResearchCitation.tenant_id == ResearchEvidence.tenant_id,
                ResearchEvidence.tenant_id == ResearchSource.tenant_id,
                ResearchEvidence.root_run_id == root_run.id,
                ResearchSource.root_run_id == root_run.id,
            )
            .order_by(ResearchCitation.created_at, ResearchCitation.id)
        )
    ).all()

    source_labels = {source.id: f"S{index}" for index, source in enumerate(sources, start=1)}
    evidence_labels = {
        evidence.id: f"E{index}" for index, evidence in enumerate(evidence_rows, start=1)
    }
    claim_labels = {claim.id: f"C{index}" for index, claim in enumerate(claims, start=1)}
    citations_by_claim: dict[UUID, list[dict[str, Any]]] = {}
    citation_index: list[dict[str, Any]] = []
    for citation, evidence, source in citation_rows:
        claim_label = claim_labels.get(citation.claim_id, "C?")
        evidence_label = evidence_labels.get(evidence.id, "E?")
        source_label = source_labels.get(source.id, "S?")
        item = {
            "claim_label": claim_label,
            "evidence_label": evidence_label,
            "source_label": source_label,
            "evidence_id": str(evidence.id),
            "source_id": str(source.id),
            "relation": citation.relation,
            "support_score": citation.support_score,
            "review_reason": citation.review_reason,
        }
        citations_by_claim.setdefault(citation.claim_id, []).append(item)
        citation_index.append(item)

    structured_claims: list[dict[str, Any]] = []
    for claim in claims:
        structured_claims.append(
            {
                "label": claim_labels[claim.id],
                "claim_id": str(claim.id),
                "agent_role": claim.agent_role,
                "status": claim.status,
                "claim": sanitize_text(claim.claim, max_chars=1_200),
                "scope": sanitize_text(claim.scope, max_chars=500),
                "limitations": sanitize_text(claim.limitations, max_chars=500),
                "original_confidence": claim.original_confidence,
                "review_summary": sanitize_text(claim.review_summary or "", max_chars=500),
                "independent_source_count": claim.independent_source_count,
                "supported_citation_count": claim.supported_citation_count,
                "stale_evidence_count": claim.stale_evidence_count,
                "citations": citations_by_claim.get(claim.id, []),
            }
        )

    source_list = [
        {
            "label": source_labels[source.id],
            "source_id": str(source.id),
            "source_kind": source.source_kind,
            "external_source_id": str(source.external_source_id),
            "title": sanitize_text(source.title, max_chars=500),
            "url": source.url,
            "domain": source.domain,
            "observed_at": source.observed_at.isoformat(),
            "independence_key": source.independence_key,
            "untrusted": True,
        }
        for source in sources
    ]
    evidence_list = [
        {
            "label": evidence_labels[evidence.id],
            "evidence_id": str(evidence.id),
            "source_label": source_labels.get(evidence.source_id, "S?"),
            "source_id": str(evidence.source_id),
            "quote": sanitize_text(evidence.quote, max_chars=2_000),
            "observed_at": evidence.observed_at.isoformat(),
            "injection_suspected": evidence.injection_suspected,
            "untrusted": True,
        }
        for evidence in evidence_rows
    ]

    supported = [claim for claim in claims if claim.status == ClaimStatus.SUPPORTED.value]
    partial = [claim for claim in claims if claim.status == ClaimStatus.PARTIALLY_SUPPORTED.value]
    conflicted = [claim for claim in claims if claim.status == ClaimStatus.CONFLICTED.value]
    insufficient = [claim for claim in claims if claim.status == ClaimStatus.INSUFFICIENT.value]
    invalid = [claim for claim in claims if claim.status == ClaimStatus.INVALID.value]

    title = truncate(
        sanitize_text(f"Research Report: {root_run.objective}", max_chars=500),
        500,
    )
    lines: list[str] = [
        f"# {_md(title, max_chars=500)}",
        "",
        f"- Root run: `{root_run.id}`",
        f"- Research graph: `{root_run.graph_version}`",
        f"- Review rules: `{review.rules_version}`",
        f"- Follow-up rounds: {review.followup_round}",
        f"- Report generated: {_date(datetime.now(UTC))}",
        "",
        "## Executive Summary",
        "",
    ]
    if supported or partial:
        lines.append(
            "This report contains evidence-reviewed findings. Supported claims are listed "
            "first; partially supported claims are explicitly qualified."
        )
        lines.append("")
        for claim in (supported + partial)[:8]:
            labels = ", ".join(
                f"[{item['evidence_label']}]" for item in citations_by_claim.get(claim.id, [])
            )
            lines.append(
                f"- **{claim_labels[claim.id]}** {_md(claim.claim, max_chars=500)} "
                f"({claim.status}{', ' + labels if labels else ''})"
            )
    else:
        lines.append(
            "**Insufficient evidence:** the review did not establish any supported or "
            "partially supported claim. This report must not be presented as validated "
            "market conclusions."
        )
    lines.extend(
        [
            "",
            "## Methodology",
            "",
            f"- Objective: {_md(root_run.objective, max_chars=2_000)}",
            f"- Source runs: {len(lineage_runs)} (1 primary + {len(lineage_runs) - 1} follow-up)",
            f"- Sources reviewed: {len(sources)}; evidence records: {len(evidence_rows)}",
            f"- Minimum independent sources per supported claim: "
            f"{settings.report_min_independent_sources}",
            f"- Evidence freshness limit: {settings.report_max_evidence_age_days} days",
            "- Citations are accepted only when the source/version/chunk or external snapshot "
            "still exists and the quote occurs in the source text.",
            "- Conflict detection is a conservative lexical opposite-polarity heuristic; it "
            "does not replace human domain review.",
        ]
    )
    if review.followup_questions:
        lines.append("- Follow-up questions:")
        lines.extend(
            f"  - {_md(question, max_chars=700)}" for question in review.followup_questions
        )
    lines.extend(
        [
            "",
            "## Findings",
            "",
        ]
    )
    if not supported and not partial:
        lines.extend(["No supported findings.", ""])
    for claim in supported + partial:
        lines.append(
            f"### {claim_labels[claim.id]} [{claim.status}] {_md(claim.claim, max_chars=1_200)}"
        )
        lines.append("")
        lines.append(f"- Agent: `{claim.agent_role}`")
        lines.append(f"- Scope: {_md(claim.scope, max_chars=500)}")
        lines.append(f"- Limitations: {_md(claim.limitations, max_chars=500)}")
        lines.append(f"- Original confidence: {claim.original_confidence:.2f}")
        lines.append(f"- Review: {_md(claim.review_summary or '', max_chars=500)}")
        lines.append("- Evidence:")
        for item in citations_by_claim.get(claim.id, []):
            lines.append(
                f"  - [{item['evidence_label']}] [{item['source_label']}] "
                f"relation={item['relation']}, support={item['support_score']:.2f}"
            )
        lines.append("")

    lines.extend(["## Risks and Limitations", ""])
    limitations: list[str] = []
    if partial:
        limitations.append(
            f"{len(partial)} claim(s) are only partially supported (single-source, stale or "
            "partially unverifiable evidence)."
        )
    if conflicted:
        limitations.append(f"{len(conflicted)} claim(s) have conflicting evidence.")
    if insufficient or invalid:
        limitations.append(
            f"{len(insufficient) + len(invalid)} claim(s) lack sufficient verifiable evidence."
        )
    if review.followup_round:
        limitations.append(
            f"The review used {review.followup_round} bounded follow-up research round(s)."
        )
    limitations.append(
        "External sources and uploaded documents are untrusted data; quotes are retained for "
        "audit and are not instructions."
    )
    if not limitations:
        limitations.append("No additional limitations were recorded by the reviewer.")
    lines.extend(f"- {_md(item, max_chars=1_000)}" for item in limitations)
    lines.append("")

    lines.extend(["## Conflicts and Gaps", ""])
    gap_claims = conflicted + insufficient + invalid
    if not gap_claims:
        lines.append("No unresolved conflicts or citation gaps were detected.")
    for claim in gap_claims:
        lines.append(
            f"### {claim_labels[claim.id]} [{claim.status}] {_md(claim.claim, max_chars=1_000)}"
        )
        lines.append("")
        lines.append(f"- Review: {_md(claim.review_summary or '', max_chars=500)}")
        if claim.conflict_reason:
            lines.append(f"- Conflict: {_md(claim.conflict_reason, max_chars=500)}")
        lines.append("")

    lines.extend(["## Evidence Links", ""])
    if not evidence_list:
        lines.append("No evidence records.")
    for evidence in evidence_list:
        lines.append(
            f"- [{evidence['label']}] [{evidence['source_label']}] "
            f"observed {evidence['observed_at']}"
            + (" **prompt-injection suspected**" if evidence["injection_suspected"] else "")
        )
        lines.append(f"  > {_md(str(evidence['quote']), max_chars=2_000)}")
    lines.append("")

    lines.extend(["## Sources", ""])
    if not source_list:
        lines.append("No sources.")
    for source in source_list:
        location = source["url"] or "document"
        lines.append(
            f"- [{source['label']}] {_md(str(source['title']), max_chars=500)} — "
            f"{_md(str(location), max_chars=2_000)} "
            f"({source['source_kind']}, observed {source['observed_at']})"
        )
    lines.append("")

    lines.extend(
        [
            "## Review Summary",
            "",
            f"- Claims: {len(claims)}",
            f"- Supported: {len(supported)}",
            f"- Partially supported: {len(partial)}",
            f"- Conflicted: {len(conflicted)}",
            f"- Insufficient: {len(insufficient)}",
            f"- Invalid/unverifiable: {len(invalid)}",
            f"- Sources: {len(sources)}; evidence: {len(evidence_rows)}",
            "",
            "All external text is treated as untrusted evidence. Human approval is required "
            "before this report is treated as approved.",
        ]
    )
    markdown = "\n".join(lines).strip() + "\n"
    if len(markdown) > settings.report_max_content_chars:
        markdown = (
            markdown[: settings.report_max_content_chars - 80].rstrip()
            + "\n\n_Report truncated by the configured content limit._\n"
        )

    structured = {
        "title": title,
        "root_run_id": str(root_run.id),
        "objective": sanitize_text(root_run.objective, max_chars=2_000),
        "model_provider": root_run.model_provider,
        "model_name": root_run.model_name,
        "graph_version": root_run.graph_version,
        "review_rules_version": review.rules_version,
        "generated_at": datetime.now(UTC).isoformat(),
        "lineage_run_ids": [str(run.id) for run in lineage_runs],
        "followup_round": review.followup_round,
        "executive_summary": [
            {
                "label": claim_labels[claim.id],
                "claim": sanitize_text(claim.claim, max_chars=1_200),
                "status": claim.status,
            }
            for claim in supported + partial
        ],
        "methodology": {
            "source_count": len(sources),
            "evidence_count": len(evidence_rows),
            "min_independent_sources": settings.report_min_independent_sources,
            "max_evidence_age_days": settings.report_max_evidence_age_days,
            "conflict_heuristic_min_shared_tokens": settings.report_conflict_min_shared_tokens,
            "rules_version": review.rules_version,
            "followup_questions": [
                sanitize_text(question, max_chars=700) for question in review.followup_questions
            ],
        },
        "claims": structured_claims,
        "sources": source_list,
        "evidence": evidence_list,
        "limitations": limitations,
        "review_counts": {
            "claims": len(claims),
            "supported": len(supported),
            "partially_supported": len(partial),
            "conflicted": len(conflicted),
            "insufficient": len(insufficient),
            "invalid": len(invalid),
            "sources": len(sources),
            "evidence": len(evidence_rows),
        },
    }
    return ReportDraft(
        title=title,
        markdown=markdown,
        structured=structured,
        source_list=source_list,
        citation_index=citation_index,
    )


__all__ = ["ReportDraft", "build_report_draft", "report_checksum"]
