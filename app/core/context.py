"""Request-scoped context propagated through logs, traces, and database access.

Tenant context is deliberately *not* derived from a client-supplied header. The
identity layer (stage 2) binds it after authentication/authorization succeeds.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from uuid import UUID

from app.core.errors import TenantContextError

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_tenant_id: ContextVar[UUID | None] = ContextVar("tenant_id", default=None)


def is_safe_request_id(value: str) -> bool:
    """Inbound request IDs are untrusted; only allow a bounded safe alphabet."""
    return bool(_REQUEST_ID_PATTERN.fullmatch(value))


def current_request_id() -> str | None:
    return _request_id.get()


def set_request_id(value: str) -> Token[str | None]:
    return _request_id.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)


def current_tenant_id() -> UUID | None:
    return _tenant_id.get()


def set_tenant_id(tenant_id: UUID) -> Token[UUID | None]:
    """Bind the authenticated tenant for the current task.

    Only trusted server-side code (authentication/authorization middleware) may
    call this. A request header such as ``X-Tenant-ID`` must never be forwarded
    here without authorization checks.
    """
    return _tenant_id.set(tenant_id)


def reset_tenant_id(token: Token[UUID | None]) -> None:
    _tenant_id.reset(token)


@contextmanager
def tenant_context(tenant_id: UUID) -> Iterator[None]:
    """Bind and reliably reset tenant context around a unit of work."""
    token = set_tenant_id(tenant_id)
    try:
        yield
    finally:
        reset_tenant_id(token)


def require_tenant_id() -> UUID:
    """Return the current tenant or fail closed.

    Missing context is a server-side integration error, not an authorization
    decision, so it is surfaced as an internal error and logged.
    """
    tenant_id = current_tenant_id()
    if tenant_id is None:
        raise TenantContextError
    return tenant_id
