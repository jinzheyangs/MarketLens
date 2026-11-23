from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.auth.enums import ScopeRole, TenantRole, TenantStatus
from app.auth.principal import Principal
from app.core.errors import ForbiddenError
from app.rag.access import build_document_access_scope


def _principal(
    *,
    tenant_admin: bool = False,
    organization_roles: dict[UUID, ScopeRole] | None = None,
    workspace_roles: dict[UUID, ScopeRole] | None = None,
    project_workspace_ids: dict[UUID, UUID] | None = None,
) -> Principal:
    return Principal(
        user_id=uuid4(),
        tenant_id=uuid4(),
        tenant_slug="acme",
        tenant_name="Acme",
        tenant_status=TenantStatus.ACTIVE,
        issuer="https://idp.test",
        subject="user-1",
        email="user@example.test",
        display_name="User",
        tenant_role=TenantRole.TENANT_ADMIN if tenant_admin else TenantRole.MEMBER,
        organization_roles=organization_roles or {},
        workspace_roles=workspace_roles or {},
        project_roles={},
        organization_ids=frozenset((organization_roles or {}).keys()),
        workspace_ids=frozenset((workspace_roles or {}).keys()),
        project_ids=frozenset((project_workspace_ids or {}).keys()),
        project_workspace_ids=project_workspace_ids or {},
    )


def test_tenant_admin_and_workspace_roles_can_read_workspace_documents() -> None:
    org_id, workspace_id = uuid4(), uuid4()
    for principal in (
        _principal(tenant_admin=True),
        _principal(organization_roles={org_id: ScopeRole.VIEWER}),
        _principal(workspace_roles={workspace_id: ScopeRole.VIEWER}),
    ):
        scope = build_document_access_scope(
            principal,
            organization_id=org_id,
            workspace_id=workspace_id,
        )
        assert scope.include_workspace_documents is True
        assert scope.allowed_project_ids is None
        assert scope.allows(None) is True


def test_project_only_member_is_restricted_to_its_projects() -> None:
    org_id, workspace_id, project_id = uuid4(), uuid4(), uuid4()
    principal = _principal(
        project_workspace_ids={project_id: workspace_id},
    )
    scope = build_document_access_scope(
        principal,
        organization_id=org_id,
        workspace_id=workspace_id,
    )
    assert scope.include_workspace_documents is False
    assert scope.allowed_project_ids == frozenset({project_id})
    assert scope.allows(project_id) is True
    assert scope.allows(None) is False
    assert scope.allows(uuid4()) is False

    selected = build_document_access_scope(
        principal,
        organization_id=org_id,
        workspace_id=workspace_id,
        project_id=project_id,
    )
    assert selected.include_workspace_documents is False
    assert selected.allows(project_id) is True


def test_project_selection_requires_access() -> None:
    org_id, workspace_id = uuid4(), uuid4()
    with pytest.raises(ForbiddenError):
        build_document_access_scope(
            _principal(),
            organization_id=org_id,
            workspace_id=workspace_id,
            project_id=uuid4(),
        )


def test_no_scope_is_denied() -> None:
    with pytest.raises(ForbiddenError):
        build_document_access_scope(
            _principal(),
            organization_id=uuid4(),
            workspace_id=uuid4(),
        )
