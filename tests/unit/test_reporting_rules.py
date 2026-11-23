from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.agent.evidence import quote_is_supported
from app.db.models import ReportVersion
from app.reporting.enums import ExportFormat
from app.reporting.exports import render_export
from app.reporting.rules import claim_conflicts, is_stale, polarity, support_score
from tests.conftest import make_settings


def test_support_score_requires_claim_evidence_overlap() -> None:
    assert (
        support_score(
            "Enterprise demand increased strongly",
            "Enterprise demand increased strongly in 2026",
            "Market report",
        )
        >= 0.5
    )
    assert (
        support_score(
            "Enterprise demand increased strongly",
            "A completely unrelated weather report",
        )
        == 0.0
    )


def test_conflicting_claims_are_flagged() -> None:
    conflicts = claim_conflicts(
        [
            ("a", "Enterprise demand increased"),
            ("b", "Enterprise demand declined"),
        ],
        min_shared_tokens=2,
    )
    assert "a" in conflicts
    assert "b" in conflicts
    assert polarity("Demand is not growing") < 0


def test_stale_and_quote_rules() -> None:
    now = datetime(2026, 11, 23, tzinfo=UTC)
    assert is_stale(now - timedelta(days=800), now=now, max_age_days=730)
    assert not is_stale(now - timedelta(days=20), now=now, max_age_days=730)
    assert quote_is_supported("Revenue grew 20 percent", "Revenue grew 20 percent in 2025.")
    assert not quote_is_supported("A fabricated quote", "Revenue grew 20 percent in 2025.")


def _version() -> ReportVersion:
    structured = {
        "title": "Research Report: test",
        "root_run_id": str(uuid4()),
        "graph_version": "stage5-v1",
        "model_provider": "deterministic",
        "model_name": "test",
        "review_rules_version": "stage6-v1",
        "generated_at": "2026-11-23T00:00:00+00:00",
        "followup_round": 0,
        "executive_summary": [{"label": "C1", "status": "supported", "claim": "Demand is growing"}],
        "methodology": {"source_count": 1, "evidence_count": 1},
        "claims": [
            {
                "label": "C1",
                "status": "supported",
                "claim": "Demand is growing",
                "agent_role": "industry",
                "scope": "global",
                "limitations": "single source",
                "review_summary": "Supported by verifiable evidence.",
                "citations": [
                    {
                        "evidence_label": "E1",
                        "source_label": "S1",
                        "relation": "supports",
                        "support_score": 0.8,
                    }
                ],
            }
        ],
        "limitations": ["Evidence is untrusted."],
        "evidence": [
            {
                "label": "E1",
                "source_label": "S1",
                "quote": "Demand is growing",
                "source_id": str(uuid4()),
                "observed_at": "2026-11-01T00:00:00+00:00",
                "injection_suspected": False,
            }
        ],
        "sources": [
            {
                "label": "S1",
                "title": "Market report",
                "url": "https://example.test/report",
                "source_kind": "external_source",
            }
        ],
        "review_counts": {"claims": 1, "supported": 1},
    }
    return ReportVersion(
        id=uuid4(),
        tenant_id=uuid4(),
        report_id=uuid4(),
        version_number=1,
        status="approved",
        title="Research Report: test",
        content_markdown="# Research Report: test\n\nDemand is growing.\n",
        structured=structured,
        source_list=[],
        citation_index=[],
        checksum_sha256="0" * 64,
    )


def test_markdown_and_docx_exports() -> None:
    settings = make_settings(report_max_export_bytes=1_000_000)
    markdown = render_export(
        _version(),
        export_format=ExportFormat.MARKDOWN,
        settings=settings,
    )
    assert markdown.content_type.startswith("text/markdown")
    assert b"Demand is growing" in markdown.data

    docx = render_export(
        _version(),
        export_format=ExportFormat.DOCX,
        settings=settings,
    )
    assert docx.content_type.endswith("wordprocessingml.document")
    assert docx.data.startswith(b"PK")
    with zipfile.ZipFile(io.BytesIO(docx.data)) as archive:
        document_xml = archive.read("word/document.xml")
    assert b"Demand is growing" in document_xml
