"""Propagate the authenticated tenant into PostgreSQL row-level security.

Policies compare ``tenant_id`` with ``app.tenant_id``. The setting is
transaction-local; pooled connections never retain another tenant's value.
If no tenant is bound the policy sees NULL and returns no rows / rejects writes.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.core.context import current_tenant_id


def _set_tenant_guc(session: Session, transaction: Any, connection: Any) -> None:
    tenant_id = current_tenant_id()
    if tenant_id is None:
        return
    connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


def install_rls_context() -> None:
    if not event.contains(Session, "after_begin", _set_tenant_guc):
        event.listen(Session, "after_begin", _set_tenant_guc)
