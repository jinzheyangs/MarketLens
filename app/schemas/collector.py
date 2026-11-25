"""外部搜索与采集接口 Schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.collector.enums import CollectionTaskStatus, CollectionTaskType, SourceStatus

UrlString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2048),
]
SearchQuery = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class CollectionTaskCreate(BaseModel):
    urls: list[UrlString] = Field(min_length=1, max_length=100)
    project_id: UUID | None = None

    @field_validator("urls")
    @classmethod
    def _deduplicate_urls(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))


class CollectionTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    workspace_id: UUID
    project_id: UUID | None
    requested_by: UUID | None
    task_type: CollectionTaskType
    url: str
    domain: str
    status: CollectionTaskStatus
    attempts: int
    max_attempts: int
    next_attempt_at: datetime
    lease_expires_at: datetime | None
    source_id: UUID | None
    last_error_code: str | None
    last_error_message: str | None
    created_at: datetime
    updated_at: datetime


class WebSearchRequest(BaseModel):
    query: SearchQuery
    limit: int = Field(default=10, ge=1, le=50)


class WebSearchHit(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    url: str
    title: str | None
    snippet: str | None
    untrusted: bool = True


class WebSearchResponse(BaseModel):
    provider: str
    results: list[WebSearchHit]
    untrusted: bool = True


class SourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    workspace_id: UUID
    url: str
    domain: str
    title: str | None
    status: SourceStatus
    content_sha256: str | None
    latest_snapshot_id: UUID | None
    first_seen_at: datetime
    last_fetched_at: datetime | None
    untrusted: bool = True


class SourceSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_id: UUID
    content_type: str
    size_bytes: int
    content_sha256: str
    final_url: str
    http_status: int
    extracted_char_count: int | None
    fetched_at: datetime
    created_at: datetime
    untrusted: bool = True


class SourceDetail(SourceRead):
    latest_text: str | None = None
    snapshots: list[SourceSnapshotRead] = Field(default_factory=list)
