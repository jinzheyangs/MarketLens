"""查询辅助：缺少已鉴权租户上下文时快速失败（fail-closed）。"""

from __future__ import annotations

from sqlalchemy import Select, select

from app.core.context import require_tenant_id
from app.db.mixins import TenantScopedMixin


def apply_tenant_scope[ModelT: TenantScopedMixin](
    statement: Select[tuple[ModelT]], model: type[ModelT]
) -> Select[tuple[ModelT]]:
    """给查询语句追加 ``tenant_id = 当前租户`` 过滤条件。"""
    return statement.where(model.tenant_id == require_tenant_id())


def tenant_scoped_select[ModelT: TenantScopedMixin](
    model: type[ModelT],
) -> Select[tuple[ModelT]]:
    """构造一个天然限定当前租户的 SELECT 语句。"""
    return apply_tenant_scope(select(model), model)
