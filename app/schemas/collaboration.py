"""Project comments and approval-node API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.collaboration.enums import ApprovalResourceType, ApprovalStatus, CommentStatus

CommentBody = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=5_000),
]


class CommentCreate(BaseModel):
    body: CommentBody
    parent_id: UUID | None = None


class CommentUpdate(BaseModel):
    body: CommentBody


class CommentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    project_id: UUID
    author_user_id: UUID
    parent_id: UUID | None
    body: str
    status: CommentStatus
    created_at: datetime
    updated_at: datetime


class ApprovalCreate(BaseModel):
    resource_type: ApprovalResourceType = ApprovalResourceType.REPORT_VERSION
    resource_id: UUID
    assignee_user_id: UUID
    request_note: str | None = Field(default=None, max_length=1_000)
    due_at: datetime | None = None


class ApprovalDecision(BaseModel):
    note: str | None = Field(default=None, max_length=1_000)


class ApprovalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    project_id: UUID
    resource_type: ApprovalResourceType
    resource_id: UUID
    requested_by: UUID
    assignee_user_id: UUID
    status: ApprovalStatus
    request_note: str | None
    decision_note: str | None
    due_at: datetime | None
    decided_by: UUID | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime
