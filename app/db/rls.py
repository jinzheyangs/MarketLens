"""把已鉴权租户身份传入 PostgreSQL 行级安全（RLS）策略。

策略将 ``tenant_id`` 与数据库 GUC ``app.tenant_id`` 比较。该设置仅在当前
事务内有效；连接池中的连接不会残留其他租户的值。若未绑定租户，策略看到
NULL，返回空结果集 / 拒绝写入。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.core.context import current_tenant_id


def _set_tenant_guc(session: Session, transaction: Any, connection: Any) -> None:
    """事务开始时，把当前上下文租户 ID 写入会话级 GUC。"""
    tenant_id = current_tenant_id()
    if tenant_id is None:
        return
    connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


def install_rls_context() -> None:
    """注册 SQLAlchemy 会话 ``after_begin`` 事件监听（幂等，避免重复挂载）。"""
    if not event.contains(Session, "after_begin", _set_tenant_guc):
        event.listen(Session, "after_begin", _set_tenant_guc)
