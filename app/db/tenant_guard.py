"""ORM 写入侧的 fail-closed 租户守卫。

这是隔离边界的基础层，而非完整隔离：完整隔离还依赖服务端派生的租户
上下文、SQL/RLS 策略与查询级强制。这里强制的规则：

* 插入租户范围的行必须存在已鉴权租户上下文；
* 新行缺失 ``tenant_id`` 时从上下文绑定；
* 为其他租户创建/更新/删除行会被拒绝。
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
    """按当前租户绑定或校验 ``instance.tenant_id``。"""
    # 新建 ORM 实例在 flush 前可能还没有值，类型注解无法表达该前置状态
    current: UUID | None = getattr(instance, "tenant_id", None)
    if is_new and current is None:
        instance.tenant_id = tenant_id
        return
    if current != tenant_id:
        raise TenantIsolationError


def _before_flush(session: Session, flush_context: Any, instances: object) -> None:
    """flush 前拦截新增/变更/删除的租户范围对象并做校验绑定。"""
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
    """每进程安装一次 flush 守卫（幂等）。"""
    if not event.contains(Session, "before_flush", _before_flush):
        event.listen(Session, "before_flush", _before_flush)
