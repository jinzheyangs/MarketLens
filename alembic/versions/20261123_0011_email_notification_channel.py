"""Allow encrypted email notification channels.

Revision ID: 20261123_0011
Revises: 20261123_0010
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261123_0011"
down_revision: str | None = "20261123_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_notification_channels_kind_valid"


def upgrade() -> None:
    op.execute(f"ALTER TABLE notification_channels DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE notification_channels ADD CONSTRAINT {_CONSTRAINT} "
        "CHECK (kind IN ('webhook', 'slack', 'email', 'log'))"
    )


def downgrade() -> None:
    # Preserve encrypted recipient lists but disable email channels so an older
    # worker never treats them as an unsupported/externally visible target.
    op.execute(
        "UPDATE notification_channels SET kind = 'log', enabled = false "
        "WHERE kind = 'email'"
    )
    op.execute(f"ALTER TABLE notification_channels DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE notification_channels ADD CONSTRAINT {_CONSTRAINT} "
        "CHECK (kind IN ('webhook', 'slack', 'log'))"
    )
