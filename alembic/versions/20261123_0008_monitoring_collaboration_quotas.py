"""Monitoring, collaboration, and tenant quotas.

Revision ID: 20261123_0008
Revises: 20261123_0007
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261123_0008"
down_revision: str | None = "20261123_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_TABLES = (
    "notification_channels",
    "monitor_subscriptions",
    "monitor_alert_rules",
    "monitor_runs",
    "monitor_seen_sources",
    "monitor_changes",
    "notification_deliveries",
    "project_comments",
    "approval_requests",
    "tenant_quotas",
)


def _enable_tenant_rls(table_name: str) -> None:
    predicate = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY tenant_isolation ON "{table_name}" '
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def upgrade() -> None:
    op.create_table(
        "notification_channels",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("encrypted_config", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning tenant. Every read/write must be scoped to it.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('webhook', 'log')",
            name=op.f("ck_notification_channels_kind_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_notification_channels_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_notification_channels_tenant_workspace",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_channels")),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_notification_channels_tenant_id_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "name",
            name="uq_notification_channels_tenant_workspace_name",
        ),
    )
    op.create_index(
        op.f("ix_notification_channels_tenant_id"),
        "notification_channels",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_notification_channels_tenant_workspace",
        "notification_channels",
        ["tenant_id", "workspace_id"],
        unique=False,
    )

    op.create_table(
        "monitor_subscriptions",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("query", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'active'"), nullable=False),
        sa.Column("cadence_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning tenant. Every read/write must be scoped to it.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cadence_minutes >= 1",
            name=op.f("ck_monitor_subscriptions_cadence_positive"),
        ),
        sa.CheckConstraint(
            "kind IN ('keyword', 'competitor')",
            name=op.f("ck_monitor_subscriptions_kind_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'deleted')",
            name=op.f("ck_monitor_subscriptions_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_monitor_subscriptions_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_monitor_subscriptions_tenant_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_monitor_subscriptions_tenant_workspace",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_subscriptions")),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_monitor_subscriptions_tenant_id_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "name",
            name="uq_monitor_subscriptions_tenant_workspace_name",
        ),
    )
    op.create_index(
        op.f("ix_monitor_subscriptions_tenant_id"),
        "monitor_subscriptions",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_monitor_subscriptions_tenant_status_next",
        "monitor_subscriptions",
        ["tenant_id", "status", "next_run_at"],
        unique=False,
    )
    op.create_index(
        "ix_monitor_subscriptions_tenant_workspace",
        "monitor_subscriptions",
        ["tenant_id", "workspace_id"],
        unique=False,
    )

    op.create_table(
        "monitor_alert_rules",
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column(
            "min_severity",
            sa.String(length=16),
            server_default=sa.text("'medium'"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning tenant. Every read/write must be scoped to it.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "min_severity IN ('low', 'medium', 'high', 'critical')",
            name=op.f("ck_monitor_alert_rules_severity_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_monitor_alert_rules_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "channel_id"],
            ["notification_channels.tenant_id", "notification_channels.id"],
            name="fk_monitor_alert_rules_tenant_channel",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "subscription_id"],
            ["monitor_subscriptions.tenant_id", "monitor_subscriptions.id"],
            name="fk_monitor_alert_rules_tenant_subscription",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_alert_rules")),
        sa.UniqueConstraint(
            "tenant_id",
            "subscription_id",
            "channel_id",
            name="uq_monitor_alert_rules_tenant_subscription_channel",
        ),
    )
    op.create_index(
        op.f("ix_monitor_alert_rules_tenant_id"),
        "monitor_alert_rules",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_monitor_alert_rules_tenant_subscription",
        "monitor_alert_rules",
        ["tenant_id", "subscription_id"],
        unique=False,
    )

    op.create_table(
        "monitor_runs",
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column(
            "triggered_by",
            sa.String(length=16),
            server_default=sa.text("'schedule'"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'pending'"), nullable=False),
        sa.Column(
            "task_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("change_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning tenant. Every read/write must be scoped to it.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_monitor_runs_attempts_non_negative"),
        ),
        sa.CheckConstraint(
            "change_count >= 0",
            name=op.f("ck_monitor_runs_change_count_non_negative"),
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name=op.f("ck_monitor_runs_max_attempts_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'collecting', 'completed', 'failed', 'canceled')",
            name=op.f("ck_monitor_runs_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_monitor_runs_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "subscription_id"],
            ["monitor_subscriptions.tenant_id", "monitor_subscriptions.id"],
            name="fk_monitor_runs_tenant_subscription",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_runs")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_monitor_runs_tenant_id_id"),
    )
    op.create_index(
        op.f("ix_monitor_runs_tenant_id"),
        "monitor_runs",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_monitor_runs_tenant_status_next",
        "monitor_runs",
        ["tenant_id", "status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_monitor_runs_tenant_subscription",
        "monitor_runs",
        ["tenant_id", "subscription_id"],
        unique=False,
    )

    op.create_table(
        "monitor_seen_sources",
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("last_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("last_content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning tenant. Every read/write must be scoped to it.",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_monitor_seen_sources_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_id"],
            ["web_sources.tenant_id", "web_sources.id"],
            name="fk_monitor_seen_sources_tenant_source",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "subscription_id"],
            ["monitor_subscriptions.tenant_id", "monitor_subscriptions.id"],
            name="fk_monitor_seen_sources_tenant_subscription",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_seen_sources")),
        sa.UniqueConstraint(
            "tenant_id",
            "subscription_id",
            "source_id",
            name="uq_monitor_seen_sources_tenant_subscription_source",
        ),
    )
    op.create_index(
        op.f("ix_monitor_seen_sources_tenant_id"),
        "monitor_seen_sources",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_monitor_seen_sources_tenant_subscription",
        "monitor_seen_sources",
        ["tenant_id", "subscription_id"],
        unique=False,
    )

    op.create_table(
        "monitor_changes",
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("previous_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("current_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("change_type", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.String(length=1000), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning tenant. Every read/write must be scoped to it.",
        ),
        sa.CheckConstraint(
            "change_type IN ('new_source', 'content_changed')",
            name=op.f("ck_monitor_changes_change_type_valid"),
        ),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name=op.f("ck_monitor_changes_severity_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_monitor_changes_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["monitor_runs.tenant_id", "monitor_runs.id"],
            name="fk_monitor_changes_tenant_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_id"],
            ["web_sources.tenant_id", "web_sources.id"],
            name="fk_monitor_changes_tenant_source",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "subscription_id"],
            ["monitor_subscriptions.tenant_id", "monitor_subscriptions.id"],
            name="fk_monitor_changes_tenant_subscription",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_changes")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_monitor_changes_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "source_id",
            "change_type",
            name="uq_monitor_changes_tenant_run_source_type",
        ),
    )
    op.create_index(
        op.f("ix_monitor_changes_tenant_id"),
        "monitor_changes",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_monitor_changes_tenant_subscription_detected",
        "monitor_changes",
        ["tenant_id", "subscription_id", "detected_at"],
        unique=False,
    )

    op.create_table(
        "notification_deliveries",
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("change_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning tenant. Every read/write must be scoped to it.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_notification_deliveries_attempts_non_negative"),
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name=op.f("ck_notification_deliveries_max_attempts_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'sent', 'failed')",
            name=op.f("ck_notification_deliveries_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_notification_deliveries_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "change_id"],
            ["monitor_changes.tenant_id", "monitor_changes.id"],
            name="fk_notification_deliveries_tenant_change",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "channel_id"],
            ["notification_channels.tenant_id", "notification_channels.id"],
            name="fk_notification_deliveries_tenant_channel",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "subscription_id"],
            ["monitor_subscriptions.tenant_id", "monitor_subscriptions.id"],
            name="fk_notification_deliveries_tenant_subscription",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_deliveries")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_notification_deliveries_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "delivery_key",
            name="uq_notification_deliveries_tenant_delivery_key",
        ),
    )
    op.create_index(
        op.f("ix_notification_deliveries_tenant_id"),
        "notification_deliveries",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_notification_deliveries_tenant_status_next",
        "notification_deliveries",
        ["tenant_id", "status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_notification_deliveries_tenant_channel",
        "notification_deliveries",
        ["tenant_id", "channel_id"],
        unique=False,
    )

    op.create_table(
        "project_comments",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("author_user_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning tenant. Every read/write must be scoped to it.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'deleted')",
            name=op.f("ck_project_comments_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_project_comments_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "parent_id"],
            ["project_comments.tenant_id", "project_comments.id"],
            name="fk_project_comments_tenant_parent",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_project_comments_tenant_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_project_comments_tenant_workspace",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_comments")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_project_comments_tenant_id_id"),
    )
    op.create_index(
        op.f("ix_project_comments_tenant_id"),
        "project_comments",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_project_comments_tenant_project_created",
        "project_comments",
        ["tenant_id", "project_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "approval_requests",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("resource_type", sa.String(length=50), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("assignee_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("request_note", sa.String(length=1000), nullable=True),
        sa.Column("decision_note", sa.String(length=1000), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning tenant. Every read/write must be scoped to it.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "resource_type IN ('report_version')",
            name=op.f("ck_approval_requests_resource_type_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'canceled')",
            name=op.f("ck_approval_requests_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_approval_requests_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_approval_requests_tenant_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_approval_requests_tenant_workspace",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_requests")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_approval_requests_tenant_id_id"),
    )
    op.create_index(
        op.f("ix_approval_requests_tenant_id"),
        "approval_requests",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_approval_requests_tenant_assignee_status",
        "approval_requests",
        ["tenant_id", "assignee_user_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_approval_requests_tenant_project",
        "approval_requests",
        ["tenant_id", "project_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "tenant_quotas",
        sa.Column("max_active_research_runs", sa.Integer(), nullable=False),
        sa.Column("max_pending_collection_tasks", sa.Integer(), nullable=False),
        sa.Column("max_monitor_subscriptions", sa.Integer(), nullable=False),
        sa.Column("max_concurrent_monitor_runs", sa.Integer(), nullable=False),
        sa.Column("max_notification_deliveries_per_day", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning tenant. Every read/write must be scoped to it.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "max_active_research_runs >= 1",
            name=op.f("ck_tenant_quotas_research_runs_positive"),
        ),
        sa.CheckConstraint(
            "max_pending_collection_tasks >= 1",
            name=op.f("ck_tenant_quotas_collection_tasks_positive"),
        ),
        sa.CheckConstraint(
            "max_monitor_subscriptions >= 1",
            name=op.f("ck_tenant_quotas_subscriptions_positive"),
        ),
        sa.CheckConstraint(
            "max_concurrent_monitor_runs >= 1",
            name=op.f("ck_tenant_quotas_monitor_runs_positive"),
        ),
        sa.CheckConstraint(
            "max_notification_deliveries_per_day >= 1",
            name=op.f("ck_tenant_quotas_notifications_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_quotas_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_quotas")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tenant_quotas_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_quotas_tenant"),
    )
    op.create_index(
        op.f("ix_tenant_quotas_tenant_id"),
        "tenant_quotas",
        ["tenant_id"],
        unique=False,
    )

    for table_name in _TENANT_TABLES:
        _enable_tenant_rls(table_name)


def downgrade() -> None:
    op.drop_index(op.f("ix_tenant_quotas_tenant_id"), table_name="tenant_quotas")
    op.drop_table("tenant_quotas")
    op.drop_index(
        "ix_approval_requests_tenant_project",
        table_name="approval_requests",
    )
    op.drop_index(
        "ix_approval_requests_tenant_assignee_status",
        table_name="approval_requests",
    )
    op.drop_index(
        op.f("ix_approval_requests_tenant_id"),
        table_name="approval_requests",
    )
    op.drop_table("approval_requests")
    op.drop_index(
        "ix_project_comments_tenant_project_created",
        table_name="project_comments",
    )
    op.drop_index(
        op.f("ix_project_comments_tenant_id"),
        table_name="project_comments",
    )
    op.drop_table("project_comments")
    op.drop_index(
        "ix_notification_deliveries_tenant_channel",
        table_name="notification_deliveries",
    )
    op.drop_index(
        "ix_notification_deliveries_tenant_status_next",
        table_name="notification_deliveries",
    )
    op.drop_index(
        op.f("ix_notification_deliveries_tenant_id"),
        table_name="notification_deliveries",
    )
    op.drop_table("notification_deliveries")
    op.drop_index(
        "ix_monitor_changes_tenant_subscription_detected",
        table_name="monitor_changes",
    )
    op.drop_index(
        op.f("ix_monitor_changes_tenant_id"),
        table_name="monitor_changes",
    )
    op.drop_table("monitor_changes")
    op.drop_index(
        "ix_monitor_seen_sources_tenant_subscription",
        table_name="monitor_seen_sources",
    )
    op.drop_index(
        op.f("ix_monitor_seen_sources_tenant_id"),
        table_name="monitor_seen_sources",
    )
    op.drop_table("monitor_seen_sources")
    op.drop_index(
        "ix_monitor_runs_tenant_subscription",
        table_name="monitor_runs",
    )
    op.drop_index(
        "ix_monitor_runs_tenant_status_next",
        table_name="monitor_runs",
    )
    op.drop_index(
        op.f("ix_monitor_runs_tenant_id"),
        table_name="monitor_runs",
    )
    op.drop_table("monitor_runs")
    op.drop_index(
        "ix_monitor_alert_rules_tenant_subscription",
        table_name="monitor_alert_rules",
    )
    op.drop_index(
        op.f("ix_monitor_alert_rules_tenant_id"),
        table_name="monitor_alert_rules",
    )
    op.drop_table("monitor_alert_rules")
    op.drop_index(
        "ix_monitor_subscriptions_tenant_workspace",
        table_name="monitor_subscriptions",
    )
    op.drop_index(
        "ix_monitor_subscriptions_tenant_status_next",
        table_name="monitor_subscriptions",
    )
    op.drop_index(
        op.f("ix_monitor_subscriptions_tenant_id"),
        table_name="monitor_subscriptions",
    )
    op.drop_table("monitor_subscriptions")
    op.drop_index(
        "ix_notification_channels_tenant_workspace",
        table_name="notification_channels",
    )
    op.drop_index(
        op.f("ix_notification_channels_tenant_id"),
        table_name="notification_channels",
    )
    op.drop_table("notification_channels")
