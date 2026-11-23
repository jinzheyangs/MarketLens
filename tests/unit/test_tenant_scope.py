from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.context import (
    current_tenant_id,
    require_tenant_id,
    tenant_context,
)
from app.core.errors import TenantContextError, TenantIsolationError
from app.db.tenant_guard import apply_tenant_scope


def test_require_tenant_id_fails_closed() -> None:
    assert current_tenant_id() is None
    with pytest.raises(TenantContextError):
        require_tenant_id()


def test_tenant_context_is_scoped_and_reset() -> None:
    tenant_id = uuid4()
    with tenant_context(tenant_id):
        assert current_tenant_id() == tenant_id
        assert require_tenant_id() == tenant_id
    assert current_tenant_id() is None


def test_new_row_without_tenant_is_bound_to_context() -> None:
    tenant_id = uuid4()
    row = SimpleNamespace(tenant_id=None)
    apply_tenant_scope(row, tenant_id, is_new=True)
    assert row.tenant_id == tenant_id


def test_new_row_for_another_tenant_is_rejected() -> None:
    row = SimpleNamespace(tenant_id=uuid4())
    with pytest.raises(TenantIsolationError):
        apply_tenant_scope(row, uuid4(), is_new=True)


def test_existing_row_for_another_tenant_is_rejected() -> None:
    row = SimpleNamespace(tenant_id=uuid4())
    with pytest.raises(TenantIsolationError):
        apply_tenant_scope(row, uuid4(), is_new=False)


def test_existing_row_for_active_tenant_is_allowed() -> None:
    tenant_id = uuid4()
    row = SimpleNamespace(tenant_id=tenant_id)
    apply_tenant_scope(row, tenant_id, is_new=False)
    assert row.tenant_id == tenant_id
