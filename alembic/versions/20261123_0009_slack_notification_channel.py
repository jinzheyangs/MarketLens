"""Allow native Slack incoming-webhook notification channels.

Revision ID: 20261123_0009
Revises: 20261123_0008
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261123_0009"
down_revision: str | None = "20261123_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_notification_channels_kind_valid"


def upgrade() -> None:
    op.execute(f"ALTER TABLE notification_channels DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE notification_channels ADD CONSTRAINT {_CONSTRAINT} "
        "CHECK (kind IN ('webhook', 'slack', 'log'))"
    )


def downgrade() -> None:
    # Slack rows cannot satisfy the old constraint. Preserve their encrypted
    # configuration but disable them so a rolled-back worker never sends the
    # generic webhook payload to Slack. Operators must re-create/re-enable them.
    op.execute(
        "UPDATE notification_channels SET kind = 'webhook', enabled = false "
        "WHERE kind = 'slack'"
    )
    op.execute(f"ALTER TABLE notification_channels DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE notification_channels ADD CONSTRAINT {_CONSTRAINT} "
        "CHECK (kind IN ('webhook', 'log'))"
    )
