"""Server-side RBAC with attribute-based restrictions.

Roles are loaded from PostgreSQL memberships. JWT role/permission claims are
never consulted. Parent roles inherit downward (organization -> workspace ->
project); descendant access does not grant parent write access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.auth.enums import (
    OrganizationStatus,
    Permission,
    ProjectClassification,
    ProjectStatus,
    ResourceType,
    ScopeRole,
    WorkspaceStatus,
)
from app.auth.principal import Principal
from app.core.errors import ForbiddenError
from app.core.logging import get_logger

logger = get_logger(__name__)

_ROLE_RANK: dict[ScopeRole, int] = {
    ScopeRole.VIEWER: 1,
    ScopeRole.EDITOR: 2,
    ScopeRole.ADMIN: 3,
}

_REQUIRED_ROLE: dict[Permission, ScopeRole] = {
    Permission.ORGANIZATION_READ: ScopeRole.VIEWER,
    Permission.ORGANIZATION_UPDATE: ScopeRole.ADMIN,
    Permission.ORGANIZATION_MANAGE_MEMBERS: ScopeRole.ADMIN,
    Permission.WORKSPACE_READ: ScopeRole.VIEWER,
    Permission.WORKSPACE_CONTENT_READ: ScopeRole.VIEWER,
    Permission.WORKSPACE_CREATE: ScopeRole.EDITOR,
    Permission.WORKSPACE_UPDATE: ScopeRole.EDITOR,
    Permission.WORKSPACE_MANAGE_STATUS: ScopeRole.ADMIN,
    Permission.WORKSPACE_MANAGE_MEMBERS: ScopeRole.ADMIN,
    Permission.PROJECT_READ: ScopeRole.VIEWER,
    Permission.PROJECT_CREATE: ScopeRole.EDITOR,
    Permission.PROJECT_UPDATE: ScopeRole.EDITOR,
    Permission.PROJECT_MANAGE_CLASSIFICATION: ScopeRole.ADMIN,
    Permission.PROJECT_MANAGE_STATUS: ScopeRole.ADMIN,
    Permission.PROJECT_MANAGE_MEMBERS: ScopeRole.ADMIN,
}


@dataclass(frozen=True, slots=True)
class ResourceContext:
    resource_type: ResourceType
    organization_id: UUID
    organization_status: str
    workspace_id: UUID | None = None
    workspace_status: str | None = None
    project_id: UUID | None = None
    project_status: str | None = None
    project_classification: str | None = None

    @property
    def resource_id(self) -> UUID:
        return self.project_id or self.workspace_id or self.organization_id


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reason: str
    effective_role: ScopeRole | None


class DenialAuditSink(Protocol):
    async def record_denial(
        self,
        *,
        principal: Principal,
        permission: Permission,
        resource_type: ResourceType,
        resource_id: UUID,
        reason: str,
    ) -> None: ...


def _rank(role: ScopeRole) -> int:
    return _ROLE_RANK[role]


def _highest(roles: list[ScopeRole]) -> ScopeRole | None:
    if not roles:
        return None
    return max(roles, key=_rank)


def _is_read(permission: Permission) -> bool:
    return permission.value.endswith(":read") or permission.value.endswith("_read")


class AuthorizationService:
    def __init__(self, *, principal: Principal, denial_audit: DenialAuditSink) -> None:
        self._principal = principal
        self._denial_audit = denial_audit

    def evaluate(
        self,
        permission: Permission,
        resource: ResourceContext,
    ) -> AuthorizationDecision:
        if self._principal.is_tenant_admin:
            effective_role: ScopeRole | None = ScopeRole.ADMIN
        else:
            effective_role = self._effective_role(permission, resource)

        if effective_role is None:
            return AuthorizationDecision(False, "no_membership", None)

        required = _REQUIRED_ROLE.get(permission)
        if required is None:
            return AuthorizationDecision(False, "unsupported_permission", effective_role)

        if _rank(effective_role) < _rank(required):
            return AuthorizationDecision(False, "insufficient_role", effective_role)

        abac_reason = self._abac_denial(permission, resource, effective_role)
        if abac_reason is not None:
            return AuthorizationDecision(False, abac_reason, effective_role)

        return AuthorizationDecision(True, "allowed", effective_role)

    async def require(self, permission: Permission, resource: ResourceContext) -> None:
        decision = self.evaluate(permission, resource)
        if decision.allowed:
            return
        await self._record_denial(
            permission=permission,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
            reason=decision.reason,
        )
        raise ForbiddenError

    async def require_tenant_admin(self, permission: Permission) -> None:
        if self._principal.is_tenant_admin:
            return
        await self._record_denial(
            permission=permission,
            resource_type=ResourceType.TENANT,
            resource_id=self._principal.tenant_id,
            reason="tenant_admin_required",
        )
        raise ForbiddenError

    def _effective_role(
        self,
        permission: Permission,
        resource: ResourceContext,
    ) -> ScopeRole | None:
        candidates: list[ScopeRole] = []
        org_role = self._principal.organization_roles.get(resource.organization_id)
        if org_role is not None:
            candidates.append(org_role)
        if resource.workspace_id is not None:
            workspace_role = self._principal.workspace_roles.get(resource.workspace_id)
            if workspace_role is not None:
                candidates.append(workspace_role)
        if resource.project_id is not None:
            project_role = self._principal.project_roles.get(resource.project_id)
            if project_role is not None:
                candidates.append(project_role)

        effective = _highest(candidates)
        if effective is not None:
            return effective

        # A descendant membership permits navigation/read of its ancestors but
        # does not grant any write capability there. Workspace-wide content
        # (external source library) deliberately requires a real workspace role.
        if (
            _is_read(permission)
            and permission != Permission.WORKSPACE_CONTENT_READ
            and self._can_read_ancestor(resource)
        ):
            return ScopeRole.VIEWER
        return None

    def _can_read_ancestor(self, resource: ResourceContext) -> bool:
        if resource.resource_type == ResourceType.ORGANIZATION:
            return resource.organization_id in self._principal.organization_ids
        if resource.resource_type == ResourceType.WORKSPACE:
            return (
                resource.workspace_id is not None
                and resource.workspace_id in self._principal.workspace_ids
            )
        if resource.resource_type == ResourceType.PROJECT:
            return (
                resource.project_id is not None
                and resource.project_id in self._principal.project_ids
            )
        return False

    def _abac_denial(
        self,
        permission: Permission,
        resource: ResourceContext,
        effective_role: ScopeRole,
    ) -> str | None:
        if _is_read(permission) or effective_role == ScopeRole.ADMIN:
            return None
        if resource.organization_status == OrganizationStatus.SUSPENDED:
            return "organization_suspended"
        if resource.workspace_status == WorkspaceStatus.ARCHIVED:
            return "workspace_archived"
        if resource.project_status == ProjectStatus.ARCHIVED:
            return "project_archived"
        if resource.project_classification == ProjectClassification.CONFIDENTIAL:
            return "confidential_requires_admin"
        return None

    async def _record_denial(
        self,
        *,
        permission: Permission,
        resource_type: ResourceType,
        resource_id: UUID,
        reason: str,
    ) -> None:
        logger.warning(
            "authorization_denied",
            extra={
                "context": {
                    "permission": str(permission),
                    "resource_type": str(resource_type),
                    "reason": reason,
                    "user_id": str(self._principal.user_id),
                }
            },
        )
        await self._denial_audit.record_denial(
            principal=self._principal,
            permission=permission,
            resource_type=resource_type,
            resource_id=resource_id,
            reason=reason,
        )
