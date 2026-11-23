"""Value objects shared by ingestion, indexing, and retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ParsedPage:
    page_number: int
    text: str


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    pages: tuple[ParsedPage, ...]
    page_count: int


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    chunk_index: int
    content: str
    content_hash: str
    page_number: int | None
    paragraph_number: int | None
    start_offset: int
    end_offset: int
    char_count: int


@dataclass(frozen=True, slots=True)
class DocumentAccessScope:
    """Derived document ACL for one workspace search/read operation."""

    workspace_id: UUID
    allowed_project_ids: frozenset[UUID] | None
    include_workspace_documents: bool
    selected_project_id: UUID | None

    def allows(self, project_id: UUID | None) -> bool:
        if project_id is None:
            return self.include_workspace_documents
        if self.selected_project_id is not None and project_id != self.selected_project_id:
            return False
        if self.allowed_project_ids is None:
            return True
        return project_id in self.allowed_project_ids

    @property
    def has_any_scope(self) -> bool:
        return (
            self.include_workspace_documents
            or self.allowed_project_ids is None
            or bool(self.allowed_project_ids)
        )

    def to_vector_filter(
        self,
        tenant_id: UUID,
        *,
        embedding_model: str | None = None,
        embedding_version: str | None = None,
    ) -> VectorFilter:
        return VectorFilter(
            tenant_id=tenant_id,
            workspace_id=self.workspace_id,
            allowed_project_ids=self.allowed_project_ids,
            include_workspace_documents=self.include_workspace_documents,
            selected_project_id=self.selected_project_id,
            embedding_model=embedding_model,
            embedding_version=embedding_version,
        )


@dataclass(frozen=True, slots=True)
class VectorFilter:
    tenant_id: UUID
    workspace_id: UUID
    allowed_project_ids: frozenset[UUID] | None
    include_workspace_documents: bool
    selected_project_id: UUID | None
    embedding_model: str | None = None
    embedding_version: str | None = None


@dataclass(frozen=True, slots=True)
class VectorChunk:
    id: UUID
    vector: list[float]
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class VectorHit:
    id: UUID
    score: float
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SearchResult:
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    version_number: int
    document_title: str
    content: str
    page_number: int | None
    paragraph_number: int | None
    start_offset: int
    end_offset: int
    score: float
    vector_rank: int | None
    keyword_rank: int | None
    observed_at: datetime | None = None
