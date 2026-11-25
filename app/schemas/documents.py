"""文档入库与检索接口 Schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.rag.enums import DocumentSourceType, DocumentStatus, DocumentVersionStatus

SearchQuery = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    workspace_id: UUID
    project_id: UUID | None
    title: str
    source_type: DocumentSourceType
    status: DocumentStatus
    active_version_id: UUID | None
    created_at: datetime
    updated_at: datetime


class DocumentVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    version_number: int
    status: DocumentVersionStatus
    filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    page_count: int | None
    embedding_model: str | None
    embedding_version: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    indexed_at: datetime | None


class DocumentDetail(DocumentRead):
    versions: list[DocumentVersionRead]


class DocumentSearchRequest(BaseModel):
    query: SearchQuery
    limit: int | None = Field(default=None, ge=1, le=200)
    project_id: UUID | None = None

    @field_validator("query")
    @classmethod
    def _reject_control_characters(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("query contains invalid characters")
        return value


class DocumentSearchResult(BaseModel):
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
    untrusted: bool = True


class DocumentSearchResponse(BaseModel):
    workspace_id: UUID
    query: str
    results: list[DocumentSearchResult]
