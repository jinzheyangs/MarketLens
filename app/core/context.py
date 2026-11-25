"""请求级上下文：在日志、追踪与数据库访问之间透传请求 ID 与租户身份。

租户上下文**绝不**从客户端请求头读取；它由身份层在鉴权成功后绑定。
这里基于 ``ContextVar`` 实现协程/线程隔离，避免不同请求之间串上下文。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from uuid import UUID

from app.core.errors import TenantContextError

# 请求 ID 只允许有限的安全字符集，长度受限，避免不可信字符进入日志/响应
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)    #存请求 ID，就是咱们最开始那个 RequestContextMiddleware 里的 request-id
_tenant_id: ContextVar[UUID | None] = ContextVar("tenant_id", default=None)     #租户 ID，UUID 类型，多租户系统用（比如 SaaS，每个公司一个租户）


def is_safe_request_id(value: str) -> bool:
    """校验入站请求 ID 是否符合安全字符集（入站请求默认不可信）。"""
    return bool(_REQUEST_ID_PATTERN.fullmatch(value))


def current_request_id() -> str | None:
    """读取当前请求的请求 ID。"""
    return _request_id.get()


def set_request_id(value: str) -> Token[str | None]:
    """写入请求 ID，返回用于后续重置的令牌。"""
    return _request_id.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    """按令牌还原请求 ID 上下文。"""
    _request_id.reset(token)


def current_tenant_id() -> UUID | None:
    """读取当前已鉴权租户的 ID。"""
    return _tenant_id.get()


def set_tenant_id(tenant_id: UUID) -> Token[UUID | None]:
    """为当前任务绑定已鉴权的租户身份。

    只有可信的服务端代码（鉴权中间件/授权层）可以调用本方法；
    严禁未经授权校验直接把 ``X-Tenant-ID`` 之类的请求头透传进来。
    """
    return _tenant_id.set(tenant_id)


def reset_tenant_id(token: Token[UUID | None]) -> None:
    """按令牌还原租户上下文。"""
    _tenant_id.reset(token)


@contextmanager
def tenant_context(tenant_id: UUID) -> Iterator[None]:
    """在一段工作单元内绑定租户上下文，并在退出时可靠重置。"""
    token = set_tenant_id(tenant_id)
    try:
        yield
    finally:
        reset_tenant_id(token)


def require_tenant_id() -> UUID:
    """返回当前租户 ID；上下文缺失时快速失败（fail-closed）。

    缺少上下文属于服务端集成错误，而非授权决策结果，因此以内部错误形式抛出并记录。
    """
    tenant_id = current_tenant_id()
    if tenant_id is None:
        raise TenantContextError
    return tenant_id
