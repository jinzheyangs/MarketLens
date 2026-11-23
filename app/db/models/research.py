"""Tenant-scoped research runs, checkpoints, findings, and tool traces."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.agent.enums import (
    SPECIALIST_ROLES,
    FindingSourceKind,
    ResearchPhase,
    ResearchRunKind,
    ResearchRunStatus,
    ResearchToolName,
    ToolCallStatus,
)
from app.db.base import Base
from app.db.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

_RUN_STATUSES = ", ".join(f"'{status}'" for status in ResearchRunStatus)
_TOOL_NAMES = ", ".join(f"'{name}'" for name in ResearchToolName)
_TOOL_STATUSES = ", ".join(f"'{status}'" for status in ToolCallStatus)
_SOURCE_KINDS = ", ".join(f"'{kind}'" for kind in FindingSourceKind)
_AGENT_ROLES = ", ".join(f"'{role}'" for role in SPECIALIST_ROLES)


class ResearchRun(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "research_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_research_runs_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "request_key",
            name="uq_research_runs_tenant_request_key",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_research_runs_tenant_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_research_runs_tenant_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "parent_run_id"],
            ["research_runs.tenant_id", "research_runs.id"],
            name="fk_research_runs_tenant_parent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "root_run_id"],
            ["research_runs.tenant_id", "research_runs.id"],
            name="fk_research_runs_tenant_root",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"status IN ({_RUN_STATUSES})", name="status_valid"),
        CheckConstraint(
            f"run_kind IN ('{ResearchRunKind.PRIMARY}', '{ResearchRunKind.FOLLOWUP}')",
            name="run_kind_valid",
        ),
        CheckConstraint("round >= 0", name="round_non_negative"),
        CheckConstraint("followup_round >= 0", name="followup_round_non_negative"),
        CheckConstraint("max_rounds >= 1", name="max_rounds_positive"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("finding_count >= 0", name="finding_count_non_negative"),
        CheckConstraint("checkpoint_count >= 0", name="checkpoint_count_non_negative"),
        CheckConstraint("error_count >= 0", name="error_count_non_negative"),
        Index(
            "ix_research_runs_tenant_workspace_status",
            "tenant_id",
            "workspace_id",
            "status",
        ),
        Index(
            "ix_research_runs_tenant_status_next",
            "tenant_id",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_research_runs_tenant_root",
            "tenant_id",
            "root_run_id",
        ),
        Index(
            "uq_research_runs_root_followup_round",
            "tenant_id",
            "root_run_id",
            "followup_round",
            unique=True,
            postgresql_where=text("run_kind = 'followup'"),
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(nullable=True)
    parent_run_id: Mapped[UUID | None] = mapped_column(nullable=True)
    root_run_id: Mapped[UUID | None] = mapped_column(nullable=True)
    run_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ResearchRunKind.PRIMARY.value,
        server_default=text(f"'{ResearchRunKind.PRIMARY}'"),
    )
    followup_round: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    requested_by: Mapped[UUID | None] = mapped_column(nullable=True)
    request_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    model_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    graph_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ResearchRunStatus.PENDING.value,
        server_default=text(f"'{ResearchRunStatus.PENDING}'"),
    )
    phase: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ResearchPhase.INITIALIZED.value,
        server_default=text(f"'{ResearchPhase.INITIALIZED}'"),
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    max_rounds: Mapped[int] = mapped_column(Integer, nullable=False)
    budget: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    usage: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    plan: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    pause_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    last_checkpoint_id: Mapped[UUID | None] = mapped_column(nullable=True)
    finding_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    checkpoint_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    error_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResearchCheckpoint(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "research_checkpoints"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_research_checkpoints_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "sequence",
            name="uq_research_checkpoints_tenant_run_sequence",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["research_runs.tenant_id", "research_runs.id"],
            name="fk_research_checkpoints_tenant_run",
            ondelete="CASCADE",
        ),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("round >= 0", name="round_non_negative"),
        Index(
            "ix_research_checkpoints_tenant_run",
            "tenant_id",
            "run_id",
            "sequence",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    node: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ResearchFinding(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "research_findings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_research_findings_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "finding_key",
            name="uq_research_findings_tenant_run_key",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["research_runs.tenant_id", "research_runs.id"],
            name="fk_research_findings_tenant_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_research_findings_tenant_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_research_findings_tenant_project",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"agent_role IN ({_AGENT_ROLES})", name="agent_role_valid"),
        CheckConstraint(f"source_kind IN ({_SOURCE_KINDS})", name="source_kind_valid"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        Index(
            "ix_research_findings_tenant_run",
            "tenant_id",
            "run_id",
            "created_at",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(nullable=True)
    task_key: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_role: Mapped[str] = mapped_column(String(32), nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID | None] = mapped_column(nullable=True)
    document_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    chunk_id: Mapped[UUID | None] = mapped_column(nullable=True)
    source_snapshot_id: Mapped[UUID | None] = mapped_column(nullable=True)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paragraph_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scope: Mapped[str] = mapped_column(String(500), nullable=False)
    limitations: Mapped[str] = mapped_column(String(500), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    injection_suspected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    evidence_untrusted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    finding_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ResearchToolCall(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "research_tool_calls"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["research_runs.tenant_id", "research_runs.id"],
            name="fk_research_tool_calls_tenant_run",
            ondelete="CASCADE",
        ),
        CheckConstraint(f"agent_role IN ({_AGENT_ROLES})", name="agent_role_valid"),
        CheckConstraint(f"tool_name IN ({_TOOL_NAMES})", name="tool_name_valid"),
        CheckConstraint(f"status IN ({_TOOL_STATUSES})", name="status_valid"),
        CheckConstraint("result_count >= 0", name="result_count_non_negative"),
        CheckConstraint("latency_ms >= 0", name="latency_ms_non_negative"),
        Index(
            "ix_research_tool_calls_tenant_run",
            "tenant_id",
            "run_id",
            "created_at",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(nullable=False)
    agent_role: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    result_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
