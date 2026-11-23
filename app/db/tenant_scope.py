"""Query helpers that fail closed without an authenticated tenant context."""

from __future__ import annotations

from sqlalchemy import Select, select

from app.core.context import require_tenant_id
from app.db.mixins import TenantScopedMixin


def apply_tenant_scope[ModelT: TenantScopedMixin](
    statement: Select[tuple[ModelT]], model: type[ModelT]
) -> Select[tuple[ModelT]]:
    return statement.where(model.tenant_id == require_tenant_id())


def tenant_scoped_select[ModelT: TenantScopedMixin](
    model: type[ModelT],
) -> Select[tuple[ModelT]]:
    return apply_tenant_scope(select(model), model)
