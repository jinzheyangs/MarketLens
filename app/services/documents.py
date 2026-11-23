"""Document read projections and ACL-filtered listing."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import Select, false, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentVersion
from app.db.tenant_scope import tenant_scoped_select
from app.rag.enums import DocumentStatus
from app.rag.types import DocumentAccessScope


def _project_condition(scope: DocumentAccessScope) -> Any:
    if scope.allowed_project_ids is None:
        if scope.selected_project_id is None:
            return None
        if scope.include_workspace_documents:
            return or_(
                Document.project_id.is_(None),
                Document.project_id == scope.selected_project_id,
            )
        return Document.project_id == scope.selected_project_id

    conditions = []
    if scope.include_workspace_documents:
        conditions.append(Document.project_id.is_(None))
    if scope.allowed_project_ids:
        conditions.append(Document.project_id.in_(scope.allowed_project_ids))
    if not conditions:
        return false()
    return or_(*conditions)


def scoped_documents(
    scope: DocumentAccessScope,
    *,
    workspace_id: UUID,
) -> Select[tuple[Document]]:
    statement = tenant_scoped_select(Document).where(
        Document.workspace_id == workspace_id,
        Document.status == DocumentStatus.ACTIVE,
    )
    condition = _project_condition(scope)
    if condition is not None:
        statement = statement.where(condition)
    return statement


async def get_document(
    session: AsyncSession,
    document_id: UUID,
    *,
    active_only: bool = True,
) -> Document | None:
    statement = tenant_scoped_select(Document).where(Document.id == document_id)
    if active_only:
        statement = statement.where(Document.status == DocumentStatus.ACTIVE)
    return cast(Document | None, await session.scalar(statement))


async def list_documents(
    session: AsyncSession,
    *,
    scope: DocumentAccessScope,
    workspace_id: UUID,
    limit: int,
    offset: int,
) -> list[Document]:
    statement = (
        scoped_documents(scope, workspace_id=workspace_id)
        .order_by(Document.created_at.desc(), Document.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.scalars(statement)).all())


async def list_versions(
    session: AsyncSession,
    *,
    document_id: UUID,
) -> list[DocumentVersion]:
    statement = (
        tenant_scoped_select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_number.desc())
    )
    return list((await session.scalars(statement)).all())
