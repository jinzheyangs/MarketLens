"""Fail-closed ORM write guard for tenant-scoped rows.

This is a foundation, not the complete isolation boundary. Stage 2 adds
server-derived tenant context, SQL/RLS policies, and query-level enforcement.
Rules enforced here:

* a tenant-scoped INSERT requires an authenticated tenant context;
* a missing ``tenant_id`` on a new row is bound from that context;
* creating/updating/deleting a row for another tenant is rejected.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.context import current_tenant_id
from app.core.errors import TenantContextError, TenantIsolationError
from app.db.mixins import TenantScopedMixin


def apply_tenant_scope(instance: TenantScopedMixin, tenant_id: UUID, *, is_new: bool) -> None:
    """Bind or validate ``instance.tenant_id`` against the active tenant."""
    # A new ORM instance may not have a value yet; the annotation alone cannot
    # express that pre-flush state.
    current: UUID | None = getattr(instance, "tenant_id", None)
    if is_new and current is None:
        instance.tenant_id = tenant_id
        return
    if current != tenant_id:
        raise TenantIsolationError


def _before_flush(session: Session, flush_context: Any, instances: object) -> None:
    tenant_id = current_tenant_id()

    scoped_new = [obj for obj in session.new if isinstance(obj, TenantScopedMixin)]
    scoped_dirty = [obj for obj in session.dirty if isinstance(obj, TenantScopedMixin)]
    scoped_deleted = [obj for obj in session.deleted if isinstance(obj, TenantScopedMixin)]

    if not (scoped_new or scoped_dirty or scoped_deleted):
        return
    if tenant_id is None:
        raise TenantContextError

    for obj in scoped_new:
        apply_tenant_scope(obj, tenant_id, is_new=True)
    for obj in (*scoped_dirty, *scoped_deleted):
        apply_tenant_scope(obj, tenant_id, is_new=False)


def install_tenant_guard() -> None:
    """Install the flush guard once per process."""
    if not event.contains(Session, "before_flush", _before_flush):
        event.listen(Session, "before_flush", _before_flush)
