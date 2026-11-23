"""Evidence value objects and untrusted-content validation helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.agent.enums import FindingSourceKind

_WHITESPACE = re.compile(r"\s+")
_INJECTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?",
        r"disregard\s+(all\s+)?(previous|prior|above)\s+instructions?",
        r"reveal\s+(the\s+)?(system|developer)\s+(prompt|message)",
        r"you\s+are\s+now\s+(a|an|the)\s+",
        r"<\|?\s*(system|assistant|developer)\s*\|?>",
        r"call\s+(the\s+)?tool\s+",
        r"delete\s+(all\s+)?(documents|sources|data)",
    )
)


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One authorized, bounded evidence passage supplied to a specialist."""

    evidence_id: str
    source_kind: FindingSourceKind
    source_id: UUID
    title: str
    text: str
    observed_at: datetime
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    chunk_id: UUID | None = None
    source_snapshot_id: UUID | None = None
    source_url: str | None = None
    page_number: int | None = None
    paragraph_number: int | None = None
    start_offset: int | None = None
    end_offset: int | None = None

    @property
    def injection_suspected(self) -> bool:
        return contains_prompt_injection(self.title) or contains_prompt_injection(self.text)


@dataclass(frozen=True, slots=True)
class SearchLead:
    """A search result that is not itself citable evidence."""

    url: str
    title: str | None
    snippet: str | None


@dataclass(slots=True)
class ToolCollection:
    evidence: list[EvidenceItem] = field(default_factory=list)
    leads: list[SearchLead] = field(default_factory=list)
    tool_calls: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)

    def deduplicate(self, *, max_evidence: int, max_leads: int) -> None:
        seen_evidence: set[str] = set()
        unique_evidence: list[EvidenceItem] = []
        for item in self.evidence:
            if item.evidence_id in seen_evidence:
                continue
            seen_evidence.add(item.evidence_id)
            unique_evidence.append(item)
            if len(unique_evidence) >= max_evidence:
                break
        self.evidence = unique_evidence

        seen_urls: set[str] = set()
        unique_leads: list[SearchLead] = []
        for lead in self.leads:
            if lead.url in seen_urls:
                continue
            seen_urls.add(lead.url)
            unique_leads.append(lead)
            if len(unique_leads) >= max_leads:
                break
        self.leads = unique_leads


def normalize_text(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def quote_is_supported(quote: str, evidence_text: str) -> bool:
    """Require a model-supplied quote to occur in the authorized evidence.

    Normalizing whitespace makes the check robust to line wrapping while still
    refusing paraphrased or fabricated citations.
    """
    normalized_quote = normalize_text(quote)
    normalized_evidence = normalize_text(evidence_text)
    if not normalized_quote or not normalized_evidence:
        return False
    minimum = min(8, len(normalized_evidence))
    if len(normalized_quote) < minimum:
        return False
    return normalized_quote in normalized_evidence


def contains_prompt_injection(value: str) -> bool:
    """Heuristically flag likely injected instructions without trusting them."""
    return any(pattern.search(value) for pattern in _INJECTION_PATTERNS)


__all__ = [
    "EvidenceItem",
    "SearchLead",
    "ToolCollection",
    "contains_prompt_injection",
    "normalize_text",
    "quote_is_supported",
]
