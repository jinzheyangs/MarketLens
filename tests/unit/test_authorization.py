from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from app.auth.authorization import AuthorizationService, ResourceContext
from app.auth.enums import (
    OrganizationStatus,
    Permission,
    ProjectClassification,
    ProjectStatus,
    ResourceType,
    ScopeRole,
    TenantRole,
    TenantStatus,
    WorkspaceStatus,
)
from app.auth.principal import Principal
from app.core.errors import ForbiddenError


class RecordingAudit:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def record_denial(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _principal(
    *,
    tenant_admin: bool = False,
    organization_roles: dict[UUID, ScopeRole] | None = None,
    workspace_roles: dict[UUID, ScopeRole] | None = None,
    project_roles: dict[UUID, ScopeRole] | None = None,
    organization_ids: set[UUID] | None = None,
    workspace_ids: set[UUID] | None = None,
    project_ids: set[UUID] | None = None,
) -> Principal:
    org_roles = organization_roles or {}
    ws_roles = workspace_roles or {}
    proj_roles = project_roles or {}
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
        organization_roles=org_roles,
        workspace_roles=ws_roles,
        project_roles=proj_roles,
        organization_ids=frozenset(organization_ids if organization_ids is not None else org_roles),
        workspace_ids=frozenset(workspace_ids if workspace_ids is not None else ws_roles),
        project_ids=frozenset(project_ids if project_ids is not None else proj_roles),
    )


def _org(org_id: UUID, status: str = OrganizationStatus.ACTIVE) -> ResourceContext:
    return ResourceContext(
        resource_type=ResourceType.ORGANIZATION,
        organization_id=org_id,
        organization_status=status,
    )


def _workspace(
    org_id: UUID,
    workspace_id: UUID,
    *,
    org_status: str = OrganizationStatus.ACTIVE,
    workspace_status: str = WorkspaceStatus.ACTIVE,
) -> ResourceContext:
    return ResourceContext(
        resource_type=ResourceType.WORKSPACE,
        organization_id=org_id,
        organization_status=org_status,
        workspace_id=workspace_id,
        workspace_status=workspace_status,
    )


def _project(
    org_id: UUID,
    workspace_id: UUID,
    project_id: UUID,
    *,
    org_status: str = OrganizationStatus.ACTIVE,
    workspace_status: str = WorkspaceStatus.ACTIVE,
    project_status: str = ProjectStatus.ACTIVE,
    classification: str = ProjectClassification.STANDARD,
) -> ResourceContext:
    return ResourceContext(
        resource_type=ResourceType.PROJECT,
        organization_id=org_id,
        organization_status=org_status,
        workspace_id=workspace_id,
        workspace_status=workspace_status,
        project_id=project_id,
        project_status=project_status,
        project_classification=classification,
    )


def test_tenant_admin_has_full_access() -> None:
    org_id, ws_id, project_id = uuid4(), uuid4(), uuid4()
    service = AuthorizationService(
        principal=_principal(tenant_admin=True),
        denial_audit=RecordingAudit(),
    )
    for permission, resource in (
        (Permission.ORGANIZATION_UPDATE, _org(org_id)),
        (Permission.WORKSPACE_CREATE, _org(org_id)),
        (Permission.PROJECT_UPDATE, _project(org_id, ws_id, project_id)),
        (
            Permission.PROJECT_UPDATE,
            _project(
                org_id,
                ws_id,
                project_id,
                classification=ProjectClassification.CONFIDENTIAL,
            ),
        ),
    ):
        assert service.evaluate(permission, resource).allowed


def test_viewer_can_read_but_not_write() -> None:
    org_id, ws_id, project_id = uuid4(), uuid4(), uuid4()
    service = AuthorizationService(
        principal=_principal(organization_roles={org_id: ScopeRole.VIEWER}),
        denial_audit=RecordingAudit(),
    )
    assert service.evaluate(Permission.ORGANIZATION_READ, _org(org_id)).allowed
    assert service.evaluate(Permission.WORKSPACE_READ, _workspace(org_id, ws_id)).allowed
    assert service.evaluate(Permission.PROJECT_READ, _project(org_id, ws_id, project_id)).allowed
    decision = service.evaluate(Permission.WORKSPACE_CREATE, _org(org_id))
    assert not decision.allowed
    assert decision.reason == "insufficient_role"


def test_editor_can_create_child_resources_but_not_manage_organization() -> None:
    org_id, ws_id = uuid4(), uuid4()
    service = AuthorizationService(
        principal=_principal(organization_roles={org_id: ScopeRole.EDITOR}),
        denial_audit=RecordingAudit(),
    )
    assert service.evaluate(Permission.WORKSPACE_CREATE, _org(org_id)).allowed
    assert service.evaluate(Permission.WORKSPACE_UPDATE, _workspace(org_id, ws_id)).allowed
    denied = service.evaluate(Permission.ORGANIZATION_UPDATE, _org(org_id))
    assert not denied.allowed
    assert denied.reason == "insufficient_role"


def test_workspace_editor_can_create_projects() -> None:
    org_id, ws_id = uuid4(), uuid4()
    service = AuthorizationService(
        principal=_principal(workspace_roles={ws_id: ScopeRole.EDITOR}),
        denial_audit=RecordingAudit(),
    )
    assert service.evaluate(Permission.PROJECT_CREATE, _workspace(org_id, ws_id)).allowed


def test_descendant_membership_can_read_ancestors_but_not_write() -> None:
    org_id, ws_id, project_id = uuid4(), uuid4(), uuid4()
    service = AuthorizationService(
        principal=_principal(
            project_roles={project_id: ScopeRole.EDITOR},
            organization_ids={org_id},
            workspace_ids={ws_id},
        ),
        denial_audit=RecordingAudit(),
    )
    assert service.evaluate(Permission.ORGANIZATION_READ, _org(org_id)).allowed
    assert service.evaluate(Permission.WORKSPACE_READ, _workspace(org_id, ws_id)).allowed
    denied = service.evaluate(Permission.WORKSPACE_UPDATE, _workspace(org_id, ws_id))
    assert not denied.allowed
    assert denied.reason == "no_membership"


def test_no_membership_is_denied() -> None:
    service = AuthorizationService(principal=_principal(), denial_audit=RecordingAudit())
    decision = service.evaluate(Permission.ORGANIZATION_READ, _org(uuid4()))
    assert not decision.allowed
    assert decision.reason == "no_membership"


def test_confidential_project_requires_admin_for_writes() -> None:
    org_id, ws_id, project_id = uuid4(), uuid4(), uuid4()
    editor = AuthorizationService(
        principal=_principal(project_roles={project_id: ScopeRole.EDITOR}),
        denial_audit=RecordingAudit(),
    )
    resource = _project(
        org_id,
        ws_id,
        project_id,
        classification=ProjectClassification.CONFIDENTIAL,
    )
    assert editor.evaluate(Permission.PROJECT_READ, resource).allowed
    denied = editor.evaluate(Permission.PROJECT_UPDATE, resource)
    assert not denied.allowed
    assert denied.reason == "confidential_requires_admin"

    admin = AuthorizationService(
        principal=_principal(project_roles={project_id: ScopeRole.ADMIN}),
        denial_audit=RecordingAudit(),
    )
    assert admin.evaluate(Permission.PROJECT_UPDATE, resource).allowed


def test_status_and_classification_changes_require_admin() -> None:
    org_id, ws_id, project_id = uuid4(), uuid4(), uuid4()
    editor = AuthorizationService(
        principal=_principal(organization_roles={org_id: ScopeRole.EDITOR}),
        denial_audit=RecordingAudit(),
    )
    admin = AuthorizationService(
        principal=_principal(organization_roles={org_id: ScopeRole.ADMIN}),
        denial_audit=RecordingAudit(),
    )
    project = _project(org_id, ws_id, project_id)
    workspace = _workspace(org_id, ws_id)

    assert editor.evaluate(Permission.PROJECT_UPDATE, project).allowed
    assert not editor.evaluate(Permission.PROJECT_MANAGE_CLASSIFICATION, project).allowed
    assert not editor.evaluate(Permission.PROJECT_MANAGE_STATUS, project).allowed
    assert not editor.evaluate(Permission.WORKSPACE_MANAGE_STATUS, workspace).allowed
    assert admin.evaluate(Permission.PROJECT_MANAGE_CLASSIFICATION, project).allowed
    assert admin.evaluate(Permission.PROJECT_MANAGE_STATUS, project).allowed
    assert admin.evaluate(Permission.WORKSPACE_MANAGE_STATUS, workspace).allowed


def test_archived_and_suspended_resources_block_editor_writes() -> None:
    org_id, ws_id, project_id = uuid4(), uuid4(), uuid4()
    service = AuthorizationService(
        principal=_principal(
            organization_roles={org_id: ScopeRole.EDITOR},
            workspace_roles={ws_id: ScopeRole.EDITOR},
            project_roles={project_id: ScopeRole.EDITOR},
        ),
        denial_audit=RecordingAudit(),
    )
    suspended = service.evaluate(
        Permission.WORKSPACE_CREATE,
        _org(org_id, OrganizationStatus.SUSPENDED),
    )
    assert suspended.reason == "organization_suspended"
    archived = service.evaluate(
        Permission.PROJECT_CREATE,
        _workspace(org_id, ws_id, workspace_status=WorkspaceStatus.ARCHIVED),
    )
    assert archived.reason == "workspace_archived"
    project_archived = service.evaluate(
        Permission.PROJECT_UPDATE,
        _project(org_id, ws_id, project_id, project_status=ProjectStatus.ARCHIVED),
    )
    assert project_archived.reason == "project_archived"


async def test_denial_is_recorded_and_raises_forbidden() -> None:
    audit = RecordingAudit()
    org_id = uuid4()
    service = AuthorizationService(
        principal=_principal(organization_roles={org_id: ScopeRole.VIEWER}),
        denial_audit=audit,
    )
    with pytest.raises(ForbiddenError):
        await service.require(Permission.WORKSPACE_CREATE, _org(org_id))
    assert audit.calls
    assert audit.calls[-1]["reason"] == "insufficient_role"
    assert audit.calls[-1]["permission"] == Permission.WORKSPACE_CREATE


async def test_tenant_admin_requirement_is_audited() -> None:
    audit = RecordingAudit()
    service = AuthorizationService(
        principal=_principal(organization_roles={uuid4(): ScopeRole.ADMIN}),
        denial_audit=audit,
    )
    with pytest.raises(ForbiddenError):
        await service.require_tenant_admin(Permission.USER_MANAGE)
    assert audit.calls[-1]["reason"] == "tenant_admin_required"
    assert audit.calls[-1]["resource_type"] == ResourceType.TENANT
