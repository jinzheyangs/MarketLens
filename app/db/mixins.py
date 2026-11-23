"""Reusable ORM mixins.

Every future business table must inherit ``TenantScopedMixin``. The ``tenant_id``
column is non-nullable and indexed; ORM writes are additionally guarded by
``app.db.tenant_guard``. Full SQL/RLS enforcement is introduced with the identity
layer in stage 2.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPrimaryKeyMixin:
    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TenantScopedMixin:
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        index=True,
        comment="Owning tenant. Every read/write must be scoped to it.",
    )
