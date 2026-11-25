"""文档上传、索引、生命周期与混合检索接口。"""

from __future__ import annotations

from dataclasses import asdict
from typing import NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.auth.authorization import AuthorizationService
from app.auth.dependencies import get_authorization, get_principal, get_settings_dep
from app.auth.enums import Permission
from app.auth.principal import Principal
from app.core.config import Settings
from app.core.errors import (
    AppError,
    ErrorCode,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.db.models import Document, DocumentVersion, Organization, Project, Workspace
from app.rag.access import build_document_access_scope
from app.rag.errors import (
    DocumentTooLargeError,
    EmbeddingError,
    ObjectStorageError,
    RagError,
    RagUnavailableError,
    UnsupportedDocumentError,
    VectorStoreError,
)
from app.rag.parsing import sanitize_title, validate_upload
from app.rag.types import DocumentAccessScope
from app.schemas.documents import (
    DocumentDetail,
    DocumentRead,
    DocumentSearchRequest,
    DocumentSearchResponse,
    DocumentSearchResult,
    DocumentVersionRead,
)
from app.services.documents import get_document, list_documents, list_versions
from app.services.rag import RagServices
from app.services.tenancy import (
    get_project_bundle,
    get_workspace_bundle,
    project_context,
    workspace_context,
)

router = APIRouter(tags=["documents"])


def _rag_services(request: Request) -> RagServices:
    services = getattr(request.app.state, "rag_services", None)
    if services is None:
        raise ServiceUnavailableError("Document retrieval is not configured.")
    return cast(RagServices, services)


def _map_rag_error(exc: RagError) -> NoReturn:
    if isinstance(exc, UnsupportedDocumentError):
        raise AppError(
            code=ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            message=exc.safe_message,
            status_code=415,
        )
    if isinstance(exc, DocumentTooLargeError):
        raise AppError(
            code=ErrorCode.PAYLOAD_TOO_LARGE,
            message=exc.safe_message,
            status_code=413,
        )
    if isinstance(
        exc,
        (ObjectStorageError, VectorStoreError, EmbeddingError, RagUnavailableError),
    ):
        raise ServiceUnavailableError(exc.safe_message)
    raise AppError(
        code=ErrorCode.INTERNAL_ERROR,
        message=exc.safe_message,
        status_code=500,
    )


async def _read_upload(file: UploadFile, settings: Settings) -> bytes:
    data = await file.read(settings.document_max_bytes + 1)
    if len(data) > settings.document_max_bytes:
        raise DocumentTooLargeError("upload exceeds DOCUMENT_MAX_BYTES")
    if not data:
        raise UnsupportedDocumentError("upload is empty")
    return data


async def _require_workspace(
    session: AsyncSession,
    workspace_id: UUID,
) -> tuple[Workspace, Organization]:
    bundle = await get_workspace_bundle(session, workspace_id)
    if bundle is None:
        raise NotFoundError("The workspace was not found.")
    return bundle


async def _require_project(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    project_id: UUID,
) -> tuple[Project, Workspace, Organization]:
    bundle = await get_project_bundle(session, project_id)
    if bundle is None or bundle[1].id != workspace_id:
        raise NotFoundError("The project was not found.")
    return bundle


async def _document_manage_context(
    session: AsyncSession,
    document: Document,
) -> tuple[Workspace, Organization, Project | None]:
    workspace, organization = await _require_workspace(session, document.workspace_id)
    project: Project | None = None
    if document.project_id is not None:
        project, project_workspace, _ = await _require_project(
            session,
            workspace_id=workspace.id,
            project_id=document.project_id,
        )
        if project_workspace.id != workspace.id:
            raise NotFoundError("The document scope is invalid.")
    return workspace, organization, project


async def _require_document_manage(
    *,
    authz: AuthorizationService,
    workspace: Workspace,
    organization: Organization,
    project: Project | None,
) -> None:
    if project is not None:
        await authz.require(
            Permission.PROJECT_UPDATE,
            project_context(project, workspace, organization),
        )
    else:
        await authz.require(
            Permission.WORKSPACE_UPDATE,
            workspace_context(workspace, organization),
        )


async def _require_document_delete(
    *,
    authz: AuthorizationService,
    workspace: Workspace,
    organization: Organization,
    project: Project | None,
) -> None:
    if project is not None:
        await authz.require(
            Permission.PROJECT_MANAGE_STATUS,
            project_context(project, workspace, organization),
        )
    else:
        await authz.require(
            Permission.WORKSPACE_MANAGE_STATUS,
            workspace_context(workspace, organization),
        )


async def _read_scope(
    *,
    principal: Principal,
    workspace: Workspace,
    organization: Organization,
    project_id: UUID | None,
) -> DocumentAccessScope:
    try:
        return build_document_access_scope(
            principal,
            organization_id=organization.id,
            workspace_id=workspace.id,
            project_id=project_id,
        )
    except ForbiddenError as exc:
        raise NotFoundError("Documents were not found.") from exc


def _detail(
    document: Document,
    versions: list[DocumentVersion],
) -> DocumentDetail:
    base = DocumentRead.model_validate(document)
    return DocumentDetail(
        **base.model_dump(),
        versions=[DocumentVersionRead.model_validate(version) for version in versions],
    )


@router.post(
    "/workspaces/{workspace_id}/documents",
    response_model=DocumentDetail,
    status_code=201,
    summary="Upload a document to a workspace or project",
)
async def upload_document(
    workspace_id: UUID,
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    project_id: UUID | None = Form(default=None),
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> DocumentDetail:
    workspace, organization = await _require_workspace(session, workspace_id)

    project: Project | None = None
    if project_id is not None:
        project, _, _ = await _require_project(
            session, workspace_id=workspace_id, project_id=project_id
        )
        await authz.require(
            Permission.PROJECT_UPDATE,
            project_context(project, workspace, organization),
        )
    else:
        await authz.require(Permission.WORKSPACE_UPDATE, workspace_context(workspace, organization))

    services = _rag_services(request)
    try:
        data = await _read_upload(file, settings)
        upload = validate_upload(
            filename=file.filename or "",
            content_type=file.content_type,
            data=data,
            settings=settings,
        )
        document, version = await services.create_document(
            principal=principal,
            workspace_id=workspace.id,
            project_id=project.id if project is not None else None,
            title=sanitize_title(title, upload.filename),
            upload=upload,
            data=data,
        )
    except RagError as exc:
        _map_rag_error(exc)
    return _detail(document, [version])


@router.post(
    "/documents/{document_id}/versions",
    response_model=DocumentVersionRead,
    status_code=201,
    summary="Upload a new document version",
)
async def upload_document_version(
    document_id: UUID,
    request: Request,
    file: UploadFile = File(...),
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> DocumentVersionRead:
    document = await get_document(session, document_id)
    if document is None:
        raise NotFoundError("The document was not found.")
    workspace, organization, project = await _document_manage_context(session, document)
    await _require_document_manage(
        authz=authz,
        workspace=workspace,
        organization=organization,
        project=project,
    )

    services = _rag_services(request)
    try:
        data = await _read_upload(file, settings)
        upload = validate_upload(
            filename=file.filename or "",
            content_type=file.content_type,
            data=data,
            settings=settings,
        )
        version = await services.add_version(
            principal=principal,
            document_id=document.id,
            upload=upload,
            data=data,
        )
    except RagError as exc:
        _map_rag_error(exc)
    return DocumentVersionRead.model_validate(version)


@router.post(
    "/documents/{document_id}/versions/{version_id}/index",
    response_model=DocumentVersionRead,
    summary="Parse, chunk, embed, and index a document version",
)
async def index_document_version(
    document_id: UUID,
    version_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> DocumentVersionRead:
    document = await get_document(session, document_id)
    if document is None:
        raise NotFoundError("The document was not found.")
    workspace, organization, project = await _document_manage_context(session, document)
    await _require_document_manage(
        authz=authz,
        workspace=workspace,
        organization=organization,
        project=project,
    )

    services = _rag_services(request)
    version = await services.index_version(
        principal=principal,
        document_id=document.id,
        version_id=version_id,
    )
    return DocumentVersionRead.model_validate(version)


@router.delete(
    "/documents/{document_id}",
    status_code=204,
    summary="Delete a document and propagate deletion to storage and vectors",
)
async def delete_document(
    document_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> Response:
    document = await get_document(session, document_id, active_only=False)
    if document is None:
        raise NotFoundError("The document was not found.")
    workspace, organization, project = await _document_manage_context(session, document)
    await _require_document_delete(
        authz=authz,
        workspace=workspace,
        organization=organization,
        project=project,
    )
    services = _rag_services(request)
    await services.delete_document(principal=principal, document_id=document.id)
    return Response(status_code=204)


@router.get(
    "/documents/{document_id}",
    response_model=DocumentDetail,
    summary="Get document metadata and versions",
)
async def get_single_document(
    document_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> DocumentDetail:
    document = await get_document(session, document_id)
    if document is None:
        raise NotFoundError("The document was not found.")
    workspace, organization = await _require_workspace(session, document.workspace_id)
    await authz.require(Permission.WORKSPACE_READ, workspace_context(workspace, organization))
    scope = await _read_scope(
        principal=principal,
        workspace=workspace,
        organization=organization,
        project_id=document.project_id,
    )
    if not scope.allows(document.project_id):
        raise NotFoundError("The document was not found.")
    versions = await list_versions(session, document_id=document.id)
    return _detail(document, versions)


@router.get(
    "/workspaces/{workspace_id}/documents",
    response_model=list[DocumentRead],
    summary="List accessible documents in a workspace",
)
async def list_workspace_documents(
    workspace_id: UUID,
    project_id: UUID | None = Query(default=None),
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[DocumentRead]:
    workspace, organization = await _require_workspace(session, workspace_id)
    await authz.require(Permission.WORKSPACE_READ, workspace_context(workspace, organization))
    if project_id is not None:
        project, _, _ = await _require_project(
            session, workspace_id=workspace_id, project_id=project_id
        )
        await authz.require(
            Permission.PROJECT_READ,
            project_context(project, workspace, organization),
        )
    scope = await _read_scope(
        principal=principal,
        workspace=workspace,
        organization=organization,
        project_id=project_id,
    )
    documents = await list_documents(
        session,
        scope=scope,
        workspace_id=workspace.id,
        limit=limit,
        offset=offset,
    )
    return [DocumentRead.model_validate(document) for document in documents]


@router.post(
    "/workspaces/{workspace_id}/search",
    response_model=DocumentSearchResponse,
    summary="Hybrid vector + keyword search with ACL filtering",
)
async def search_workspace_documents(
    workspace_id: UUID,
    payload: DocumentSearchRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> DocumentSearchResponse:
    workspace, organization = await _require_workspace(session, workspace_id)
    await authz.require(Permission.WORKSPACE_READ, workspace_context(workspace, organization))
    if payload.project_id is not None:
        project, _, _ = await _require_project(
            session, workspace_id=workspace_id, project_id=payload.project_id
        )
        await authz.require(
            Permission.PROJECT_READ,
            project_context(project, workspace, organization),
        )
    scope = await _read_scope(
        principal=principal,
        workspace=workspace,
        organization=organization,
        project_id=payload.project_id,
    )
    services = _rag_services(request)
    limit = min(
        payload.limit or settings.search_default_limit,
        settings.search_max_limit,
    )
    results = await services.search(scope=scope, query=payload.query, limit=limit)
    return DocumentSearchResponse(
        workspace_id=workspace.id,
        query=payload.query,
        results=[DocumentSearchResult(**asdict(result)) for result in results],
    )
