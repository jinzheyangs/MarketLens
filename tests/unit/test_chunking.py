from __future__ import annotations

import pytest

from app.rag.chunking import chunk_document
from app.rag.errors import TooManyChunksError
from app.rag.types import ParsedDocument, ParsedPage


def _document(text: str) -> ParsedDocument:
    return ParsedDocument(pages=(ParsedPage(page_number=1, text=text),), page_count=1)


def test_paragraph_chunks_keep_offsets_and_citation_metadata() -> None:
    text = "First paragraph.\n\nSecond paragraph is longer."
    chunks = chunk_document(
        _document(text),
        max_chars=30,
        overlap_chars=5,
        max_chunks=10,
    )
    assert len(chunks) == 2
    first, second = chunks
    assert first.chunk_index == 0
    assert first.page_number == 1
    assert first.paragraph_number == 1
    assert text[first.start_offset : first.end_offset] == first.content
    assert second.paragraph_number == 2
    assert text[second.start_offset : second.end_offset] == second.content
    assert first.content_hash != second.content_hash
    assert first.char_count == len(first.content)


def test_chunking_is_deterministic() -> None:
    text = "Alpha beta gamma.\n\nDelta epsilon zeta.\n\nTheta."
    first = chunk_document(_document(text), max_chars=30, overlap_chars=5, max_chunks=20)
    second = chunk_document(_document(text), max_chars=30, overlap_chars=5, max_chunks=20)
    assert first == second


def test_long_paragraph_is_split_with_overlap() -> None:
    text = "a" * 100
    chunks = chunk_document(_document(text), max_chars=40, overlap_chars=10, max_chunks=20)
    assert len(chunks) == 3
    assert all(len(chunk.content) <= 40 for chunk in chunks)
    assert all(chunk.paragraph_number == 1 for chunk in chunks)
    for chunk in chunks:
        assert text[chunk.start_offset : chunk.end_offset] == chunk.content


def test_page_numbers_are_preserved() -> None:
    document = ParsedDocument(
        pages=(
            ParsedPage(page_number=1, text="Page one text."),
            ParsedPage(page_number=2, text="Page two text."),
        ),
        page_count=2,
    )
    chunks = chunk_document(document, max_chars=100, overlap_chars=10, max_chunks=10)
    assert [chunk.page_number for chunk in chunks] == [1, 2]


def test_chunk_limit_is_enforced() -> None:
    with pytest.raises(TooManyChunksError):
        chunk_document(
            _document("One paragraph.\n\nTwo paragraph."),
            max_chars=15,
            overlap_chars=0,
            max_chunks=1,
        )
