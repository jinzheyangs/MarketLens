"""证据值对象与不可信内容校验辅助函数。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.agent.enums import FindingSourceKind

# 连续空白归一化为单个空格
_WHITESPACE = re.compile(r"\s+")
# 常见提示注入特征的启发式正则（仅标记，不执行其中指令）
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
    """提供给专业 Agent 的一条授权、有边界的证据片段。"""

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
        """标题或正文是否疑似包含提示注入。"""
        return contains_prompt_injection(self.title) or contains_prompt_injection(self.text)


@dataclass(frozen=True, slots=True)
class SearchLead:
    """一条搜索结果线索，本身不能作为可引用证据。"""

    url: str
    title: str | None
    snippet: str | None


@dataclass(slots=True)
class ToolCollection:
    """一次工具调用收集到的证据、线索、调用次数与错误。"""

    evidence: list[EvidenceItem] = field(default_factory=list)
    leads: list[SearchLead] = field(default_factory=list)
    tool_calls: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)

    def deduplicate(self, *, max_evidence: int, max_leads: int) -> None:
        """按证据 ID / URL 去重，并按上限截断。"""
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
    """归一化空白：连续空白折叠为单个空格并去除首尾。"""
    return _WHITESPACE.sub(" ", value).strip()


def quote_is_supported(quote: str, evidence_text: str) -> bool:
    """要求模型给出的摘录确实出现在授权证据原文中。

    归一化空白后做包含判断，既对换行/折行鲁棒，又拒绝改写或编造的引用。
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
    """启发式标记疑似注入指令；仅作风险标记，绝不信任其内容。"""
    return any(pattern.search(value) for pattern in _INJECTION_PATTERNS)


__all__ = [
    "EvidenceItem",
    "SearchLead",
    "ToolCollection",
    "contains_prompt_injection",
    "normalize_text",
    "quote_is_supported",
]
