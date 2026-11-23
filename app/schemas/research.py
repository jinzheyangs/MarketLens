"""Research run, finding, checkpoint, and tool trace API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.agent.enums import (
    AgentRole,
    FindingSourceKind,
    ResearchRunStatus,
    ResearchToolName,
    ToolCallStatus,
)

ObjectiveString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000),
]
RequestKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]


class ResearchRunCreate(BaseModel):
    objective: ObjectiveString
    project_id: UUID | None = None
    request_key: RequestKey | None = None


class ResearchRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    workspace_id: UUID
    project_id: UUID | None
    requested_by: UUID | None
    request_key: str | None
    objective: str
    model_provider: str
    model_name: str
    graph_version: str
    status: ResearchRunStatus
    phase: str
    round: int
    max_rounds: int
    budget: dict[str, Any]
    usage: dict[str, Any]
    plan: list[dict[str, Any]]
    stop_reason: str | None
    error_code: str | None
    error_message: str | None
    pause_requested: bool
    cancel_requested: bool
    attempts: int
    max_attempts: int
    finding_count: int
    checkpoint_count: int
    error_count: int
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ResearchFindingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    task_key: str
    agent_role: AgentRole
    claim: str
    evidence: str
    source_kind: FindingSourceKind
    source_id: UUID
    document_id: UUID | None
    document_version_id: UUID | None
    chunk_id: UUID | None
    source_snapshot_id: UUID | None
    page_number: int | None
    paragraph_number: int | None
    start_offset: int | None
    end_offset: int | None
    source_title: str | None
    source_url: str | None
    observed_at: datetime
    scope: str
    limitations: str
    confidence: float
    injection_suspected: bool
    evidence_untrusted: bool
    created_at: datetime
    untrusted: bool = True


class ResearchCheckpointRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sequence: int
    node: str
    phase: str
    round: int
    state: dict[str, Any]
    created_at: datetime
    untrusted: bool = True


class ResearchToolCallRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agent_role: AgentRole
    tool_name: ResearchToolName
    status: ToolCallStatus
    arguments: dict[str, Any]
    result_count: int
    error_code: str | None
    latency_ms: float
    created_at: datetime
