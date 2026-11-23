"""Deterministic paragraph-aware chunking with citation offsets."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator

from app.rag.errors import TooManyChunksError
from app.rag.types import ChunkDraft, ParsedDocument

_PARAGRAPH_PATTERN = re.compile(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*")


def _paragraphs(text: str) -> Iterator[tuple[str, int, int]]:
    for match in _PARAGRAPH_PATTERN.finditer(text):
        raw = match.group()
        stripped = raw.strip()
        if not stripped:
            continue
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw) - len(raw.rstrip())
        yield stripped, match.start() + leading, match.end() - trailing


def _split_long(
    text: str,
    *,
    base_start: int,
    max_chars: int,
    overlap_chars: int,
) -> Iterator[tuple[str, int, int]]:
    step = max(1, max_chars - overlap_chars)
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        yield text[start:end], base_start + start, base_start + end
        if end == len(text):
            break
        start += step


def chunk_document(
    document: ParsedDocument,
    *,
    max_chars: int,
    overlap_chars: int,
    max_chunks: int,
) -> list[ChunkDraft]:
    chunks: list[ChunkDraft] = []

    def add_chunk(
        *,
        content: str,
        page_number: int,
        paragraph_number: int,
        start_offset: int,
        end_offset: int,
    ) -> None:
        content = content.strip()
        if not content:
            return
        if len(chunks) >= max_chunks:
            raise TooManyChunksError("document exceeds DOCUMENT_MAX_CHUNKS")
        chunks.append(
            ChunkDraft(
                chunk_index=len(chunks),
                content=content,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                page_number=page_number,
                paragraph_number=paragraph_number,
                start_offset=start_offset,
                end_offset=end_offset,
                char_count=len(content),
            )
        )

    def flush_buffered(
        buffered: list[str],
        *,
        page_number: int,
        paragraph_number: int,
        start_offset: int,
        end_offset: int,
    ) -> list[str]:
        if not buffered:
            return buffered
        add_chunk(
            content="\n\n".join(buffered),
            page_number=page_number,
            paragraph_number=paragraph_number,
            start_offset=start_offset,
            end_offset=end_offset,
        )
        return []

    for page in document.pages:
        paragraphs = list(_paragraphs(page.text))
        if not paragraphs:
            continue

        buffered: list[str] = []
        first_paragraph = 1
        start_offset = 0
        end_offset = 0

        for paragraph_number, (text, start, end) in enumerate(paragraphs, start=1):
            if len(text) > max_chars:
                buffered = flush_buffered(
                    buffered,
                    page_number=page.page_number,
                    paragraph_number=first_paragraph,
                    start_offset=start_offset,
                    end_offset=end_offset,
                )
                for piece, piece_start, piece_end in _split_long(
                    text,
                    base_start=start,
                    max_chars=max_chars,
                    overlap_chars=overlap_chars,
                ):
                    add_chunk(
                        content=piece,
                        page_number=page.page_number,
                        paragraph_number=paragraph_number,
                        start_offset=piece_start,
                        end_offset=piece_end,
                    )
                continue

            candidate_length = len("\n\n".join([*buffered, text])) if buffered else len(text)
            if buffered and candidate_length > max_chars:
                buffered = flush_buffered(
                    buffered,
                    page_number=page.page_number,
                    paragraph_number=first_paragraph,
                    start_offset=start_offset,
                    end_offset=end_offset,
                )
                candidate_length = len(text)

            if not buffered:
                first_paragraph = paragraph_number
                start_offset = start
            buffered.append(text)
            end_offset = end
            if candidate_length >= max_chars:
                buffered = flush_buffered(
                    buffered,
                    page_number=page.page_number,
                    paragraph_number=first_paragraph,
                    start_offset=start_offset,
                    end_offset=end_offset,
                )

        flush_buffered(
            buffered,
            page_number=page.page_number,
            paragraph_number=first_paragraph,
            start_offset=start_offset,
            end_offset=end_offset,
        )

    return chunks
