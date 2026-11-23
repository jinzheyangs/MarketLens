"""Deterministic evidence-review rules.

These rules are intentionally independent of any model narrative. A model may
never turn a missing/unavailable citation into a supported claim; semantic
conflict detection is a conservative lexical heuristic in this stage.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from app.agent.evidence import normalize_text

_TOKEN_PATTERN = re.compile(r"[^\W_]{2,}", re.UNICODE)
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "will",
        "can",
        "could",
        "should",
        "would",
        "about",
        "into",
        "than",
        "then",
        "there",
        "their",
        "they",
        "these",
        "those",
        "over",
        "under",
        "more",
        "most",
        "less",
        "least",
        "not",
        "but",
        "its",
        "also",
        "which",
        "when",
        "where",
        "what",
        "who",
        "how",
        "why",
    }
)
_POSITIVE_TERMS = frozenset(
    {
        "increase",
        "increased",
        "increasing",
        "grow",
        "grew",
        "growing",
        "rise",
        "rose",
        "rising",
        "strong",
        "stronger",
        "improve",
        "improved",
        "improving",
        "adoption",
        "adopt",
        "opportunity",
        "opportunities",
        "expand",
        "expanded",
        "leading",
        "leader",
        "gain",
        "gained",
        "win",
        "winning",
        "likely",
        "positive",
        "accelerate",
        "accelerated",
    }
)
_NEGATIVE_TERMS = frozenset(
    {
        "decrease",
        "decreased",
        "decreasing",
        "decline",
        "declined",
        "declining",
        "fall",
        "fell",
        "falling",
        "drop",
        "dropped",
        "weak",
        "weaker",
        "worse",
        "worsen",
        "failed",
        "failing",
        "failure",
        "risk",
        "risks",
        "barrier",
        "barriers",
        "slow",
        "slower",
        "contraction",
        "shrink",
        "shrunk",
        "lose",
        "lost",
        "loss",
        "unlikely",
        "negative",
        "churn",
        "delay",
        "delayed",
    }
)


def significant_tokens(value: str) -> frozenset[str]:
    return frozenset(
        token.casefold()
        for token in _TOKEN_PATTERN.findall(value)
        if token.casefold() not in _STOPWORDS
    )


def support_score(claim: str, evidence_text: str, source_title: str = "") -> float:
    claim_tokens = significant_tokens(claim)
    if not claim_tokens:
        return 0.0
    evidence_tokens = significant_tokens(f"{source_title} {evidence_text}")
    if not evidence_tokens:
        return 0.0
    return round(len(claim_tokens & evidence_tokens) / len(claim_tokens), 4)


def is_stale(observed_at: datetime, *, now: datetime, max_age_days: int) -> bool:
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    return now - observed_at > timedelta(days=max_age_days)


def polarity(value: str) -> int:
    tokens = significant_tokens(value)
    positive = len(tokens & _POSITIVE_TERMS)
    negative = len(tokens & _NEGATIVE_TERMS)
    if positive == negative:
        return 0
    result = 1 if positive > negative else -1
    if re.search(r"\b(not|no|never|cannot|can't|isn't|aren't)\b", value, re.IGNORECASE):
        return -result
    return result


def claim_conflicts(
    claims: Sequence[tuple[str, str]],
    *,
    min_shared_tokens: int,
) -> dict[str, str]:
    """Return claim_id -> conflict reason for opposite-polarity, overlapping claims.

    This is a deliberately conservative lexical heuristic, not a semantic NLI
    model. It flags for human/report review; it never upgrades evidence.
    """
    conflicts: dict[str, str] = {}
    tokens = {claim_id: significant_tokens(text) for claim_id, text in claims}
    polarities = {claim_id: polarity(text) for claim_id, text in claims}
    for index, (left_id, _left_text) in enumerate(claims):
        for right_id, _right_text in claims[index + 1 :]:
            shared = tokens[left_id] & tokens[right_id]
            if len(shared) < min_shared_tokens:
                continue
            if polarities[left_id] == 0 or polarities[right_id] == 0:
                continue
            if polarities[left_id] == polarities[right_id]:
                continue
            reason = (
                "Opposite-polarity claims share significant terms: " + ", ".join(sorted(shared)[:8])
            )[:500]
            conflicts[left_id] = reason
            conflicts[right_id] = reason
    return conflicts


def sanitize_text(value: str, *, max_chars: int = 20_000) -> str:
    candidate = _CONTROL_PATTERN.sub("", value)
    candidate = normalize_text(candidate)
    return candidate[:max_chars]


def stable_key(*parts: str, length: int = 32) -> str:
    raw = "\x1f".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


__all__ = [
    "claim_conflicts",
    "is_stale",
    "polarity",
    "sanitize_text",
    "significant_tokens",
    "stable_key",
    "support_score",
    "truncate",
]
