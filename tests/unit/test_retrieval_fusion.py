from __future__ import annotations

from uuid import uuid4

from app.rag.retrieval import _reciprocal_rank_fusion


def test_rrf_promotes_items_found_by_both_retrievers() -> None:
    shared = uuid4()
    vector_only = uuid4()
    keyword_only = uuid4()
    scores = _reciprocal_rank_fusion(
        [shared, vector_only],
        [shared, keyword_only],
    )
    assert scores[shared] > scores[vector_only]
    assert scores[shared] > scores[keyword_only]
    assert set(scores) == {shared, vector_only, keyword_only}


def test_rrf_preserves_deterministic_ordering_inputs() -> None:
    first = uuid4()
    second = uuid4()
    scores = _reciprocal_rank_fusion([first, second], [])
    assert scores[first] > scores[second]
