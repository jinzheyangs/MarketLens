"""从服务端工作区/项目角色推导文档访问范围（ACL）。"""

from __future__ import annotations

from uuid import UUID

from app.auth.principal import Principal
from app.core.errors import ForbiddenError
from app.rag.types import DocumentAccessScope


def build_document_access_scope(
    principal: Principal,
    *,
    organization_id: UUID,
    workspace_id: UUID,
    project_id: UUID | None = None,
) -> DocumentAccessScope:
    workspace_access = (
        principal.is_tenant_admin
        or organization_id in principal.organization_roles
        or workspace_id in principal.workspace_roles
    )
    direct_project_ids = {
        candidate
        for candidate, parent_workspace in principal.project_workspace_ids.items()
        if parent_workspace == workspace_id
    }

    if project_id is not None:
        if not workspace_access and project_id not in direct_project_ids:
            raise ForbiddenError
        return DocumentAccessScope(
            workspace_id=workspace_id,
            allowed_project_ids=frozenset({project_id}),
            include_workspace_documents=workspace_access,
            selected_project_id=project_id,
        )

    if workspace_access:
        return DocumentAccessScope(
            workspace_id=workspace_id,
            allowed_project_ids=None,
            include_workspace_documents=True,
            selected_project_id=None,
        )

    if direct_project_ids:
        return DocumentAccessScope(
            workspace_id=workspace_id,
            allowed_project_ids=frozenset(direct_project_ids),
            include_workspace_documents=False,
            selected_project_id=None,
        )

    raise ForbiddenError
