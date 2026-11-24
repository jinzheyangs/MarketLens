"""Per-tenant collection domain policies.

Revision ID: 20261123_0010
Revises: 20261123_0009
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261123_0010"
down_revision: str | None = "20261123_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "tenant_domain_policies"


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
        _TABLE,
        sa.Column(
            "allowed_domains",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "blocked_domains",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_domain_policies_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_domain_policies")),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_tenant_domain_policies_tenant_id_id",
        ),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_domain_policies_tenant"),
    )
    op.create_index(
        op.f("ix_tenant_domain_policies_tenant_id"),
        _TABLE,
        ["tenant_id"],
        unique=False,
    )
    _enable_tenant_rls(_TABLE)


def downgrade() -> None:
    op.drop_index(
        op.f("ix_tenant_domain_policies_tenant_id"),
        table_name=_TABLE,
    )
    op.drop_table(_TABLE)
