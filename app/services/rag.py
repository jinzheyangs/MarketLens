"""Document ingestion, indexing, deletion propagation, and hybrid search."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.enums import AuditAction, AuditOutcome, ResourceType
from app.auth.principal import Principal
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ServiceUnavailableError
from app.db.models import Document, DocumentChunk, DocumentVersion
from app.db.tenant_scope import tenant_scoped_select
from app.rag.chunking import chunk_document
from app.rag.embedding import (
    EmbeddingProvider,
    build_embedding_provider,
)
from app.rag.enums import DocumentStatus, DocumentVersionStatus
from app.rag.errors import (
    DocumentParseError,
    DocumentTooLargeError,
    EmbeddingError,
    RagError,
    VectorStoreError,
)
from app.rag.parsing import UploadInfo, parse_document
from app.rag.retrieval import HybridRetriever
from app.rag.storage import ObjectStorage, build_object_storage
from app.rag.types import ChunkDraft, DocumentAccessScope, SearchResult, VectorChunk
from app.rag.vector_store import QdrantVectorStore, build_vector_store
from app.schemas.health import ComponentHealth
from app.services.audit import record_event

logger = logging.getLogger(__name__)

_VECTOR_UPSERT_BATCH = 256
_REINDEXABLE_STATUSES = {
    DocumentVersionStatus.UPLOADED,
    DocumentVersionStatus.FAILED,
    DocumentVersionStatus.INDEXED,
    DocumentVersionStatus.SUPERSEDED,
}


def _lease_expired(updated_at: datetime | None, lease_seconds: int) -> bool:
    if updated_at is None:
        return True
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - updated_at > timedelta(seconds=lease_seconds)


def _object_key(
    *,
    tenant_id: UUID,
    workspace_id: UUID,
    document_id: UUID,
    version_id: UUID,
) -> str:
    return (
        f"tenants/{tenant_id}/workspaces/{workspace_id}/"
        f"documents/{document_id}/versions/{version_id}/original"
    )


def _chunk_id(*, tenant_id: UUID, version_id: UUID, chunk_index: int) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"marketscope:{tenant_id}:{version_id}:{chunk_index}",
    )


def _safe_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, RagError):
        return exc.code, exc.safe_message
    return "internal_error", "Document processing failed."


@dataclass(slots=True)
class RagServices:
    settings: Settings
    storage: ObjectStorage
    embedding: EmbeddingProvider
    vector_store: QdrantVectorStore
    session_factory: async_sessionmaker[AsyncSession]
    retriever: HybridRetriever

    async def ensure_ready(self) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 11):
            try:
                await self.storage.ensure_ready()
                await self.vector_store.ensure_collection(self.embedding.dimension)
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "rag_dependency_not_ready",
                    extra={
                        "context": {
                            "attempt": attempt,
                            "error": type(exc).__name__,
                        }
                    },
                )
                if attempt < 10:
                    await asyncio.sleep(1)
        if last_error is not None:
            raise last_error

    async def health(self) -> dict[str, ComponentHealth]:
        components: dict[str, ComponentHealth] = {}
        checks = (
            ("object_storage", self.storage.health),
            ("vector_store", self.vector_store.health),
        )
        for name, check in checks:
            started = time.perf_counter()
            try:
                async with asyncio.timeout(self.settings.health_check_timeout_seconds):
                    await check()
                components[name] = ComponentHealth(
                    status="ok",
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            except Exception as exc:
                logger.warning(
                    "rag_component_unhealthy",
                    extra={"context": {"component": name, "error": type(exc).__name__}},
                )
                components[name] = ComponentHealth(
                    status="error",
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                )
        return components

    async def aclose(self) -> None:
        for close in (
            self.vector_store.aclose,
            self.embedding.aclose,
            self.storage.aclose,
        ):
            try:
                await close()
            except Exception:
                logger.exception("rag_resource_close_failed")

    async def create_document(
        self,
        *,
        principal: Principal,
        workspace_id: UUID,
        project_id: UUID | None,
        title: str,
        upload: UploadInfo,
        data: bytes,
    ) -> tuple[Document, DocumentVersion]:
        tenant_id = principal.tenant_id
        document_id = uuid4()
        version_id = uuid4()
        object_key = _object_key(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            document_id=document_id,
            version_id=version_id,
        )
        checksum = hashlib.sha256(data).hexdigest()
        await self.storage.put(object_key, data, upload.content_type)

        document = Document(
            id=document_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            project_id=project_id,
            title=title,
            created_by=principal.user_id,
        )
        version = DocumentVersion(
            id=version_id,
            tenant_id=tenant_id,
            document_id=document_id,
            version_number=1,
            status=DocumentVersionStatus.UPLOADED.value,
            object_key=object_key,
            filename=upload.filename,
            content_type=upload.content_type,
            size_bytes=len(data),
            checksum_sha256=checksum,
            created_by=principal.user_id,
        )
        try:
            async with self.session_factory() as session, session.begin():
                session.add_all([document, version])
                try:
                    await session.flush()
                except IntegrityError as exc:
                    raise ConflictError("The document could not be created.") from exc
                record_event(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=principal.user_id,
                    action=AuditAction.DOCUMENT_UPLOADED,
                    resource_type=ResourceType.DOCUMENT,
                    resource_id=document.id,
                    details={
                        "content_type": upload.content_type,
                        "size_bytes": len(data),
                        "version_number": 1,
                    },
                )
                await session.flush()
        except Exception:
            await self._best_effort_delete_object(object_key)
            raise
        return document, version

    async def add_version(
        self,
        *,
        principal: Principal,
        document_id: UUID,
        upload: UploadInfo,
        data: bytes,
    ) -> DocumentVersion:
        tenant_id = principal.tenant_id
        version_id = uuid4()
        checksum = hashlib.sha256(data).hexdigest()

        async with self.session_factory() as session:
            document = cast(
                Document | None,
                await session.scalar(
                    tenant_scoped_select(Document).where(
                        Document.id == document_id,
                        Document.status == DocumentStatus.ACTIVE,
                    )
                ),
            )
            if document is None:
                raise NotFoundError("The document was not found.")
            object_key = _object_key(
                tenant_id=tenant_id,
                workspace_id=document.workspace_id,
                document_id=document_id,
                version_id=version_id,
            )

        await self.storage.put(object_key, data, upload.content_type)
        try:
            async with self.session_factory() as session, session.begin():
                document = cast(
                    Document | None,
                    await session.scalar(
                        tenant_scoped_select(Document)
                        .where(
                            Document.id == document_id,
                            Document.status == DocumentStatus.ACTIVE,
                        )
                        .with_for_update()
                    ),
                )
                if document is None:
                    raise NotFoundError("The document was not found.")
                maximum = await session.scalar(
                    select(func.max(DocumentVersion.version_number)).where(
                        DocumentVersion.tenant_id == tenant_id,
                        DocumentVersion.document_id == document_id,
                    )
                )
                version_number = int(maximum or 0) + 1
                version = DocumentVersion(
                    id=version_id,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    version_number=version_number,
                    status=DocumentVersionStatus.UPLOADED.value,
                    object_key=object_key,
                    filename=upload.filename,
                    content_type=upload.content_type,
                    size_bytes=len(data),
                    checksum_sha256=checksum,
                    created_by=principal.user_id,
                )
                session.add(version)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    raise ConflictError("The document version could not be created.") from exc
                record_event(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=principal.user_id,
                    action=AuditAction.DOCUMENT_UPLOADED,
                    resource_type=ResourceType.DOCUMENT_VERSION,
                    resource_id=version.id,
                    details={
                        "document_id": str(document_id),
                        "content_type": upload.content_type,
                        "size_bytes": len(data),
                        "version_number": version_number,
                    },
                )
                await session.flush()
        except Exception:
            await self._best_effort_delete_object(object_key)
            raise
        return version

    async def index_version(
        self,
        *,
        principal: Principal,
        document_id: UUID,
        version_id: UUID,
    ) -> DocumentVersion:
        tenant_id = principal.tenant_id
        async with self.session_factory() as session, session.begin():
            version = cast(
                DocumentVersion | None,
                await session.scalar(
                    tenant_scoped_select(DocumentVersion)
                    .where(
                        DocumentVersion.id == version_id,
                        DocumentVersion.document_id == document_id,
                    )
                    .with_for_update()
                ),
            )
            document = cast(
                Document | None,
                await session.scalar(
                    tenant_scoped_select(Document).where(Document.id == document_id)
                ),
            )
            if version is None or document is None:
                raise NotFoundError("The document version was not found.")
            if document.status != DocumentStatus.ACTIVE:
                raise ConflictError("The document is not active.")
            if version.status == DocumentVersionStatus.PROCESSING.value:
                if not _lease_expired(
                    version.updated_at,
                    self.settings.document_state_lease_seconds,
                ):
                    raise ConflictError("The document version is already being indexed.")
            elif version.status not in _REINDEXABLE_STATUSES:
                raise ConflictError("The document version is not in an indexable state.")
            version.status = DocumentVersionStatus.PROCESSING.value
            version.error_code = None
            version.error_message = None

            object_key = version.object_key
            filename = version.filename
            workspace_id = document.workspace_id
            project_id = document.project_id

        try:
            data = await self.storage.get(object_key)
            if len(data) > self.settings.document_max_bytes:
                raise DocumentTooLargeError("stored object exceeds DOCUMENT_MAX_BYTES")
            extension = Path(filename).suffix.lower()
            parsed = await asyncio.to_thread(
                parse_document,
                data,
                extension=extension,
                settings=self.settings,
            )
            drafts = await asyncio.to_thread(
                chunk_document,
                parsed,
                max_chars=self.settings.chunk_max_chars,
                overlap_chars=self.settings.chunk_overlap_chars,
                max_chunks=self.settings.document_max_chunks,
            )
            if not drafts:
                raise DocumentParseError("document produced no chunks")

            vectors = await self.embedding.embed_texts([draft.content for draft in drafts])
            if len(vectors) != len(drafts):
                raise EmbeddingError("embedding count does not match chunk count")

            vector_chunks: list[VectorChunk] = []
            for draft, vector in zip(drafts, vectors, strict=True):
                vector_chunks.append(
                    VectorChunk(
                        id=_chunk_id(
                            tenant_id=tenant_id,
                            version_id=version_id,
                            chunk_index=draft.chunk_index,
                        ),
                        vector=vector,
                        payload={
                            "tenant_id": str(tenant_id),
                            "workspace_id": str(workspace_id),
                            "project_id": str(project_id) if project_id else "none",
                            "document_id": str(document_id),
                            "version_id": str(version_id),
                            "chunk_index": draft.chunk_index,
                            "page_number": draft.page_number or -1,
                            "paragraph_number": draft.paragraph_number or -1,
                            "content_hash": draft.content_hash,
                            "embedding_model": self.embedding.model,
                            "embedding_version": self.embedding.version,
                        },
                    )
                )

            await self.vector_store.delete_version(
                tenant_id=tenant_id,
                document_id=document_id,
                version_id=version_id,
            )
            for offset in range(0, len(vector_chunks), _VECTOR_UPSERT_BATCH):
                await self.vector_store.upsert(
                    vector_chunks[offset : offset + _VECTOR_UPSERT_BATCH]
                )

            previous_version_ids = await self._commit_index(
                document_id=document_id,
                version_id=version_id,
                parsed_page_count=parsed.page_count,
                drafts=drafts,
                tenant_id=tenant_id,
                principal=principal,
            )
            for previous_version_id in previous_version_ids:
                try:
                    await self.vector_store.delete_version(
                        tenant_id=tenant_id,
                        document_id=document_id,
                        version_id=previous_version_id,
                    )
                except VectorStoreError:
                    logger.exception(
                        "stale_vector_cleanup_failed",
                        extra={
                            "context": {
                                "tenant_id": str(tenant_id),
                                "document_id": str(document_id),
                                "version_id": str(previous_version_id),
                            }
                        },
                    )
        except Exception as exc:
            await self._mark_index_failed(
                tenant_id=tenant_id,
                document_id=document_id,
                version_id=version_id,
                exc=exc,
                principal=principal,
            )
            return await self._load_version(version_id)

        return await self._load_version(version_id)

    async def _commit_index(
        self,
        *,
        document_id: UUID,
        version_id: UUID,
        parsed_page_count: int,
        drafts: Sequence[ChunkDraft],
        tenant_id: UUID,
        principal: Principal,
    ) -> list[UUID]:
        async with self.session_factory() as session, session.begin():
            document = cast(
                Document | None,
                await session.scalar(
                    tenant_scoped_select(Document)
                    .where(Document.id == document_id)
                    .with_for_update()
                ),
            )
            version = cast(
                DocumentVersion | None,
                await session.scalar(
                    tenant_scoped_select(DocumentVersion)
                    .where(DocumentVersion.id == version_id)
                    .with_for_update()
                ),
            )
            if document is None or version is None:
                raise ConflictError("The document was deleted during indexing.")
            if document.status != DocumentStatus.ACTIVE:
                raise ConflictError("The document was deleted during indexing.")
            if version.status != DocumentVersionStatus.PROCESSING.value:
                raise ConflictError("The document version is no longer processing.")

            previous_version_ids = list(
                (
                    await session.scalars(
                        select(DocumentVersion.id).where(
                            DocumentVersion.tenant_id == tenant_id,
                            DocumentVersion.document_id == document_id,
                            DocumentVersion.id != version_id,
                            DocumentVersion.status == DocumentVersionStatus.INDEXED,
                        )
                    )
                ).all()
            )
            await session.execute(
                delete(DocumentChunk).where(
                    DocumentChunk.tenant_id == tenant_id,
                    DocumentChunk.version_id == version_id,
                )
            )
            if previous_version_ids:
                await session.execute(
                    update(DocumentVersion)
                    .where(
                        DocumentVersion.tenant_id == tenant_id,
                        DocumentVersion.id.in_(previous_version_ids),
                    )
                    .values(status=DocumentVersionStatus.SUPERSEDED.value)
                )

            for draft in drafts:
                session.add(
                    DocumentChunk(
                        id=_chunk_id(
                            tenant_id=tenant_id,
                            version_id=version_id,
                            chunk_index=draft.chunk_index,
                        ),
                        tenant_id=tenant_id,
                        document_id=document_id,
                        version_id=version_id,
                        chunk_index=draft.chunk_index,
                        content=draft.content,
                        content_hash=draft.content_hash,
                        page_number=draft.page_number,
                        paragraph_number=draft.paragraph_number,
                        start_offset=draft.start_offset,
                        end_offset=draft.end_offset,
                        char_count=draft.char_count,
                        embedding_model=self.embedding.model,
                        embedding_version=self.embedding.version,
                    )
                )

            version.status = DocumentVersionStatus.INDEXED.value
            version.page_count = parsed_page_count
            version.embedding_model = self.embedding.model
            version.embedding_version = self.embedding.version
            version.error_code = None
            version.error_message = None
            version.indexed_at = datetime.now(UTC)
            document.active_version_id = version_id

            try:
                await session.flush()
            except IntegrityError as exc:
                raise ConflictError("The document index could not be committed.") from exc

            record_event(
                session,
                tenant_id=tenant_id,
                actor_user_id=principal.user_id,
                action=AuditAction.DOCUMENT_INDEXED,
                resource_type=ResourceType.DOCUMENT_VERSION,
                resource_id=version_id,
                details={
                    "document_id": str(document_id),
                    "version_number": version.version_number,
                    "chunk_count": len(drafts),
                    "page_count": parsed_page_count,
                },
            )
            await session.flush()
        return previous_version_ids

    async def _mark_index_failed(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        version_id: UUID,
        exc: Exception,
        principal: Principal,
    ) -> None:
        code, safe_message = _safe_error(exc)
        logger.error(
            "document_index_failed",
            extra={
                "context": {
                    "tenant_id": str(tenant_id),
                    "document_id": str(document_id),
                    "version_id": str(version_id),
                    "error": type(exc).__name__,
                    "code": code,
                }
            },
            exc_info=exc,
        )
        try:
            async with self.session_factory() as session, session.begin():
                version = cast(
                    DocumentVersion | None,
                    await session.scalar(
                        tenant_scoped_select(DocumentVersion)
                        .where(DocumentVersion.id == version_id)
                        .with_for_update()
                    ),
                )
                if version is None:
                    return
                if version.status != DocumentVersionStatus.PROCESSING.value:
                    return
                version.status = DocumentVersionStatus.FAILED.value
                version.error_code = code
                version.error_message = safe_message
                record_event(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=principal.user_id,
                    action=AuditAction.DOCUMENT_INDEX_FAILED,
                    resource_type=ResourceType.DOCUMENT_VERSION,
                    resource_id=version_id,
                    outcome=AuditOutcome.DENIED,
                    details={
                        "document_id": str(document_id),
                        "version_number": version.version_number,
                        "error_code": code,
                    },
                )
                await session.flush()
        except Exception:
            logger.exception(
                "document_index_failure_state_write_failed",
                extra={"context": {"version_id": str(version_id)}},
            )

    async def _load_version(self, version_id: UUID) -> DocumentVersion:
        async with self.session_factory() as session:
            version = cast(
                DocumentVersion | None,
                await session.scalar(
                    tenant_scoped_select(DocumentVersion).where(DocumentVersion.id == version_id)
                ),
            )
        if version is None:
            raise NotFoundError("The document version was not found.")
        return version

    async def delete_document(
        self,
        *,
        principal: Principal,
        document_id: UUID,
    ) -> None:
        tenant_id = principal.tenant_id
        async with self.session_factory() as session, session.begin():
            document = cast(
                Document | None,
                await session.scalar(
                    tenant_scoped_select(Document)
                    .where(Document.id == document_id)
                    .with_for_update()
                ),
            )
            if document is None:
                raise NotFoundError("The document was not found.")
            if document.status == DocumentStatus.DELETED:
                return
            if document.status == DocumentStatus.DELETING and not _lease_expired(
                document.updated_at,
                self.settings.document_state_lease_seconds,
            ):
                raise ConflictError("The document is already being deleted.")
            document.status = DocumentStatus.DELETING.value
            object_keys = list(
                (
                    await session.scalars(
                        select(DocumentVersion.object_key).where(
                            DocumentVersion.tenant_id == tenant_id,
                            DocumentVersion.document_id == document_id,
                        )
                    )
                ).all()
            )

        try:
            await self.vector_store.delete_document(
                tenant_id=tenant_id,
                document_id=document_id,
            )
            for object_key in object_keys:
                await self.storage.delete(object_key)
        except Exception as exc:
            await self._mark_delete_failed(
                tenant_id=tenant_id,
                document_id=document_id,
                principal=principal,
                exc=exc,
            )
            raise ServiceUnavailableError("Document deletion could not be completed.") from exc

        try:
            async with self.session_factory() as session, session.begin():
                document = cast(
                    Document | None,
                    await session.scalar(
                        tenant_scoped_select(Document)
                        .where(Document.id == document_id)
                        .with_for_update()
                    ),
                )
                if document is None:
                    return
                await session.execute(
                    delete(DocumentChunk).where(
                        DocumentChunk.tenant_id == tenant_id,
                        DocumentChunk.document_id == document_id,
                    )
                )
                await session.execute(
                    update(DocumentVersion)
                    .where(
                        DocumentVersion.tenant_id == tenant_id,
                        DocumentVersion.document_id == document_id,
                    )
                    .values(
                        status=DocumentVersionStatus.DELETED.value,
                        error_code=None,
                        error_message=None,
                    )
                )
                document.status = DocumentStatus.DELETED.value
                document.active_version_id = None
                record_event(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=principal.user_id,
                    action=AuditAction.DOCUMENT_DELETED,
                    resource_type=ResourceType.DOCUMENT,
                    resource_id=document_id,
                    details={"versions_deleted": len(object_keys)},
                )
                await session.flush()
        except Exception as exc:
            await self._mark_delete_failed(
                tenant_id=tenant_id,
                document_id=document_id,
                principal=principal,
                exc=exc,
            )
            raise ServiceUnavailableError("Document deletion could not be completed.") from exc

    async def _mark_delete_failed(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        principal: Principal,
        exc: Exception,
    ) -> None:
        code, _ = _safe_error(exc)
        logger.error(
            "document_delete_failed",
            extra={
                "context": {
                    "tenant_id": str(tenant_id),
                    "document_id": str(document_id),
                    "error": type(exc).__name__,
                    "code": code,
                }
            },
            exc_info=exc,
        )
        try:
            async with self.session_factory() as session, session.begin():
                document = cast(
                    Document | None,
                    await session.scalar(
                        tenant_scoped_select(Document)
                        .where(Document.id == document_id)
                        .with_for_update()
                    ),
                )
                if document is None:
                    return
                if document.status != DocumentStatus.DELETING.value:
                    return
                document.status = DocumentStatus.DELETE_FAILED.value
                record_event(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=principal.user_id,
                    action=AuditAction.DOCUMENT_DELETE_FAILED,
                    resource_type=ResourceType.DOCUMENT,
                    resource_id=document_id,
                    outcome=AuditOutcome.DENIED,
                    details={"error_code": code},
                )
                await session.flush()
        except Exception:
            logger.exception(
                "document_delete_failure_state_write_failed",
                extra={"context": {"document_id": str(document_id)}},
            )

    async def search(
        self,
        *,
        scope: DocumentAccessScope,
        query: str,
        limit: int,
    ) -> list[SearchResult]:
        return await self.retriever.search(
            self.session_factory,
            scope=scope,
            query=query,
            limit=limit,
        )

    async def _best_effort_delete_object(self, object_key: str) -> None:
        try:
            await self.storage.delete(object_key)
        except Exception:
            logger.exception(
                "orphan_object_cleanup_failed",
                extra={"context": {"object_key": object_key}},
            )


def build_rag_services(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> RagServices:
    storage = build_object_storage(settings)
    embedding = build_embedding_provider(settings)
    vector_store = build_vector_store(settings)
    retriever = HybridRetriever(
        settings=settings,
        embedding=embedding,
        vector_store=vector_store,
    )
    return RagServices(
        settings=settings,
        storage=storage,
        embedding=embedding,
        vector_store=vector_store,
        session_factory=session_factory,
        retriever=retriever,
    )


__all__ = ["RagServices", "build_rag_services"]
