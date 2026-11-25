"""Markdown 与 DOCX 报告导出器。"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from docx import Document as DocxDocument
from docx.shared import Pt

from app.core.config import Settings
from app.db.models import ReportVersion
from app.reporting.enums import ExportFormat
from app.reporting.errors import ExportProcessingError, ExportUnsupportedFormatError
from app.reporting.rules import sanitize_text


@dataclass(frozen=True, slots=True)
class RenderedExport:
    data: bytes
    content_type: str
    extension: str


def _render_markdown(version: ReportVersion) -> bytes:
    return version.content_markdown.encode("utf-8")


def _safe(value: Any, *, max_chars: int = 20_000) -> str:
    return sanitize_text(str(value or ""), max_chars=max_chars)


def _render_docx(version: ReportVersion) -> bytes:
    structured = version.structured if isinstance(version.structured, dict) else {}
    document = DocxDocument()
    document.add_heading(_safe(structured.get("title") or version.title, max_chars=500), 0)

    metadata = [
        f"Root run: {structured.get('root_run_id', '')}",
        f"Graph: {structured.get('graph_version', '')}",
        f"Model: {structured.get('model_provider', '')}/{structured.get('model_name', '')}",
        f"Review rules: {structured.get('review_rules_version', '')}",
        f"Generated: {structured.get('generated_at', '')}",
        f"Follow-up rounds: {structured.get('followup_round', 0)}",
    ]
    for item in metadata:
        paragraph = document.add_paragraph(item)
        paragraph.runs[0].font.size = Pt(9)

    document.add_heading("Executive Summary", level=1)
    summary = structured.get("executive_summary")
    if isinstance(summary, list) and summary:
        for item in summary:
            if isinstance(item, dict):
                document.add_paragraph(
                    f"{_safe(item.get('label'), max_chars=32)} "
                    f"[{_safe(item.get('status'), max_chars=32)}]: "
                    f"{_safe(item.get('claim'), max_chars=2_000)}",
                    style="List Bullet",
                )
    else:
        document.add_paragraph(
            "Insufficient evidence: no supported or partially supported claim was established."
        )

    document.add_heading("Methodology", level=1)
    methodology = structured.get("methodology")
    if isinstance(methodology, dict):
        for key, value in methodology.items():
            document.add_paragraph(f"{_safe(key, max_chars=100)}: {_safe(value, max_chars=500)}")

    document.add_heading("Findings", level=1)
    claims = structured.get("claims")
    if isinstance(claims, list):
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            status = _safe(claim.get("status"), max_chars=32)
            if status not in {"supported", "partially_supported"}:
                continue
            document.add_heading(
                f"{_safe(claim.get('label'), max_chars=32)} [{status}] "
                f"{_safe(claim.get('claim'), max_chars=1_200)}",
                level=2,
            )
            document.add_paragraph(f"Agent: {_safe(claim.get('agent_role'), max_chars=32)}")
            document.add_paragraph(f"Scope: {_safe(claim.get('scope'), max_chars=500)}")
            document.add_paragraph(f"Limitations: {_safe(claim.get('limitations'), max_chars=500)}")
            document.add_paragraph(f"Review: {_safe(claim.get('review_summary'), max_chars=500)}")
            citations = claim.get("citations")
            if isinstance(citations, list):
                for citation in citations:
                    if not isinstance(citation, dict):
                        continue
                    document.add_paragraph(
                        f"[{_safe(citation.get('evidence_label'), max_chars=32)}] "
                        f"[{_safe(citation.get('source_label'), max_chars=32)}] "
                        f"{_safe(citation.get('relation'), max_chars=32)} "
                        f"(support {float(citation.get('support_score') or 0):.2f})",
                        style="List Bullet",
                    )

    document.add_heading("Conflicts and Gaps", level=1)
    if isinstance(claims, list):
        gap_claims = [
            claim
            for claim in claims
            if isinstance(claim, dict)
            and claim.get("status") in {"conflicted", "insufficient", "invalid"}
        ]
        if not gap_claims:
            document.add_paragraph("No unresolved conflicts or citation gaps were detected.")
        for claim in gap_claims:
            document.add_heading(
                f"{_safe(claim.get('label'), max_chars=32)} "
                f"[{_safe(claim.get('status'), max_chars=32)}] "
                f"{_safe(claim.get('claim'), max_chars=1_000)}",
                level=2,
            )
            document.add_paragraph(f"Review: {_safe(claim.get('review_summary'), max_chars=500)}")
            if claim.get("conflict_reason"):
                document.add_paragraph(
                    f"Conflict: {_safe(claim.get('conflict_reason'), max_chars=500)}"
                )

    document.add_heading("Risks and Limitations", level=1)
    limitations = structured.get("limitations")
    if isinstance(limitations, list):
        for item in limitations:
            document.add_paragraph(_safe(item, max_chars=1_000), style="List Bullet")

    document.add_heading("Evidence", level=1)
    evidence = structured.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if not isinstance(item, dict):
                continue
            paragraph = document.add_paragraph()
            paragraph.add_run(
                f"[{_safe(item.get('label'), max_chars=32)}] "
                f"[{_safe(item.get('source_label'), max_chars=32)}] "
            ).bold = True
            paragraph.add_run(_safe(item.get("quote"), max_chars=2_000))
            document.add_paragraph(
                "Untrusted source excerpt. "
                + (
                    "Prompt-injection heuristics flagged this evidence."
                    if item.get("injection_suspected")
                    else ""
                ),
                style="Intense Quote",
            )

    document.add_heading("Sources", level=1)
    sources = structured.get("sources")
    if isinstance(sources, list):
        for item in sources:
            if not isinstance(item, dict):
                continue
            document.add_paragraph(
                f"[{_safe(item.get('label'), max_chars=32)}] "
                f"{_safe(item.get('title'), max_chars=500)} — "
                f"{_safe(item.get('url') or 'document', max_chars=2_000)} "
                f"({_safe(item.get('source_kind'), max_chars=32)})",
                style="List Bullet",
            )

    counts = structured.get("review_counts")
    document.add_heading("Review Summary", level=1)
    if isinstance(counts, dict):
        for key, value in counts.items():
            document.add_paragraph(f"{_safe(key, max_chars=100)}: {_safe(value, max_chars=100)}")

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def render_export(
    version: ReportVersion,
    *,
    export_format: ExportFormat,
    settings: Settings,
) -> RenderedExport:
    if export_format is ExportFormat.MARKDOWN:
        data = _render_markdown(version)
        content_type = "text/markdown; charset=utf-8"
        extension = "md"
    elif export_format is ExportFormat.DOCX:
        data = _render_docx(version)
        content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        extension = "docx"
    else:
        raise ExportUnsupportedFormatError
    if len(data) > settings.report_max_export_bytes:
        raise ExportProcessingError("The exported report exceeded the configured size limit.")
    return RenderedExport(data=data, content_type=content_type, extension=extension)


__all__ = ["RenderedExport", "render_export"]
