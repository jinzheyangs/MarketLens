"""Create the root tenant registry.

Revision ID: 20261123_0001
Revises:
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261123_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "slug",
            sa.String(length=64),
            nullable=False,
            comment="Stable human-readable key. Never used as an authorization decision.",
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
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
        # Alembic applies the metadata naming convention to these names, so only
        # the constraint-specific suffix is supplied here.
        sa.CheckConstraint(
            "status IN ('active', 'suspended')",
            name="status_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
        comment=(
            "Root tenant registry. Organizations, workspaces, projects, and "
            "memberships arrive in stage 2."
        ),
    )


def downgrade() -> None:
    op.drop_table("tenants")
