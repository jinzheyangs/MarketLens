"""Hybrid retrieval: PostgreSQL full text/keyword + Qdrant vectors + RRF rerank.

Candidate filtering happens in both stores using tenant/workspace/project scope.
Every returned candidate is re-read from PostgreSQL with the same ACL and the
active document version, so stale/orphaned vectors can never leak content.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Select, false, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.context import require_tenant_id
from app.db.models import Document, DocumentChunk, DocumentVersion
from app.rag.embedding import EmbeddingProvider
from app.rag.enums import DocumentStatus, DocumentVersionStatus
from app.rag.errors import EmbeddingError, VectorStoreError
from app.rag.types import DocumentAccessScope, SearchResult
from app.rag.vector_store import QdrantVectorStore

logger = logging.getLogger(__name__)

_RRF_K = 60
_UNTRUSTED_RECORD_STATUSES = {
    DocumentVersionStatus.DELETING,
    DocumentVersionStatus.DELETED,
    DocumentVersionStatus.DELETE_FAILED,
}


def _scoped_chunk_select(scope: DocumentAccessScope) -> Select[tuple[UUID]]:
    tenant_id = require_tenant_id()
    statement = (
        select(DocumentChunk.id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.version_id)
        .where(
            DocumentChunk.tenant_id == tenant_id,
            Document.tenant_id == tenant_id,
            DocumentVersion.tenant_id == tenant_id,
            Document.workspace_id == scope.workspace_id,
            Document.status == DocumentStatus.ACTIVE,
            Document.active_version_id == DocumentChunk.version_id,
        )
    )

    condition = _project_scope_condition(scope)
    if condition is not None:
        statement = statement.where(condition)
    return statement


def _project_scope_condition(scope: DocumentAccessScope) -> Any:
    if scope.allowed_project_ids is None:
        if scope.selected_project_id is None:
            return None
        if scope.include_workspace_documents:
            return or_(
                Document.project_id.is_(None),
                Document.project_id == scope.selected_project_id,
            )
        return Document.project_id == scope.selected_project_id

    project_conditions = []
    if scope.include_workspace_documents:
        project_conditions.append(Document.project_id.is_(None))
    if scope.allowed_project_ids:
        project_conditions.append(Document.project_id.in_(scope.allowed_project_ids))
    if not project_conditions:
        return false()
    return or_(*project_conditions)


async def keyword_candidates(
    session: AsyncSession,
    *,
    scope: DocumentAccessScope,
    query: str,
    limit: int,
    embedding_model: str,
    embedding_version: str,
) -> list[UUID]:
    base = _scoped_chunk_select(scope).where(
        DocumentChunk.embedding_model == embedding_model,
        DocumentChunk.embedding_version == embedding_version,
    )
    tsquery = func.websearch_to_tsquery("simple", query)
    rank = func.ts_rank_cd(DocumentChunk.search_vector, tsquery)
    fts_statement = (
        base.add_columns(rank.label("score"))
        .where(DocumentChunk.search_vector.op("@@")(tsquery))
        .order_by(rank.desc(), DocumentChunk.id)
        .limit(limit)
    )
    rows = (await session.execute(fts_statement)).all()

    ordered: list[UUID] = []
    seen: set[UUID] = set()
    for row in rows:
        chunk_id = row[0]
        if chunk_id not in seen:
            seen.add(chunk_id)
            ordered.append(chunk_id)

    if len(ordered) < limit:
        fallback_statement = (
            base.add_columns(literal(0.5).label("score"))
            .where(DocumentChunk.content.icontains(query, autoescape=True))
            .order_by(DocumentChunk.id)
            .limit(limit - len(ordered))
        )
        fallback_rows = (await session.execute(fallback_statement)).all()
        for row in fallback_rows:
            chunk_id = row[0]
            if chunk_id not in seen:
                seen.add(chunk_id)
                ordered.append(chunk_id)

    return ordered


async def vector_candidates(
    *,
    embedding: EmbeddingProvider,
    vector_store: QdrantVectorStore,
    scope: DocumentAccessScope,
    query: str,
    limit: int,
) -> list[UUID]:
    tenant_id = require_tenant_id()
    query_vector = await embedding.embed_query(query)
    hits = await vector_store.search(
        vector=query_vector,
        query_filter=scope.to_vector_filter(
            tenant_id,
            embedding_model=embedding.model,
            embedding_version=embedding.version,
        ),
        limit=limit,
    )
    return [hit.id for hit in hits]


class HybridRetriever:
    def __init__(
        self,
        *,
        settings: Settings,
        embedding: EmbeddingProvider,
        vector_store: QdrantVectorStore,
    ) -> None:
        self._settings = settings
        self._embedding = embedding
        self._vector_store = vector_store

    async def search(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        scope: DocumentAccessScope,
        query: str,
        limit: int,
    ) -> list[SearchResult]:
        candidate_limit = max(self._settings.search_candidate_limit, limit * 3)
        async with session_factory() as session:
            keyword_ids = await keyword_candidates(
                session,
                scope=scope,
                query=query,
                limit=candidate_limit,
                embedding_model=self._embedding.model,
                embedding_version=self._embedding.version,
            )

        try:
            vector_ids = await vector_candidates(
                embedding=self._embedding,
                vector_store=self._vector_store,
                scope=scope,
                query=query,
                limit=candidate_limit,
            )
        except (EmbeddingError, VectorStoreError) as exc:
            logger.warning(
                "vector_retrieval_degraded",
                extra={"context": {"error": type(exc).__name__}},
            )
            vector_ids = []

        fused = _reciprocal_rank_fusion(vector_ids, keyword_ids)
        if not fused:
            return []

        async with session_factory() as session:
            rows = await _fetch_authorized_candidates(
                session,
                scope=scope,
                chunk_ids=list(fused),
                embedding_model=self._embedding.model,
                embedding_version=self._embedding.version,
            )

        results: list[SearchResult] = []
        query_lower = query.lower()
        for chunk_id, rrf_score in fused.items():
            record = rows.get(chunk_id)
            if record is None:
                continue
            chunk, document, version = record
            final_score = rrf_score
            if query_lower in chunk.content.lower():
                final_score += 0.05
            if query_lower in document.title.lower():
                final_score += 0.02
            results.append(
                SearchResult(
                    chunk_id=chunk.id,
                    document_id=document.id,
                    version_id=version.id,
                    version_number=version.version_number,
                    document_title=document.title,
                    content=chunk.content,
                    page_number=chunk.page_number,
                    paragraph_number=chunk.paragraph_number,
                    start_offset=chunk.start_offset,
                    end_offset=chunk.end_offset,
                    score=round(final_score, 6),
                    vector_rank=_rank_of(chunk.id, vector_ids),
                    keyword_rank=_rank_of(chunk.id, keyword_ids),
                    observed_at=version.indexed_at or version.created_at,
                )
            )

        results.sort(key=lambda item: (-item.score, str(item.chunk_id)))
        return results[:limit]


def _reciprocal_rank_fusion(
    vector_ids: Sequence[UUID],
    keyword_ids: Sequence[UUID],
) -> dict[UUID, float]:
    scores: dict[UUID, float] = {}
    for rank, chunk_id in enumerate(vector_ids, start=1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
    for rank, chunk_id in enumerate(keyword_ids, start=1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
    return scores


def _rank_of(chunk_id: UUID, ordered: Sequence[UUID]) -> int | None:
    try:
        return list(ordered).index(chunk_id) + 1
    except ValueError:
        return None


async def _fetch_authorized_candidates(
    session: AsyncSession,
    *,
    scope: DocumentAccessScope,
    chunk_ids: list[UUID],
    embedding_model: str,
    embedding_version: str,
) -> dict[UUID, tuple[DocumentChunk, Document, DocumentVersion]]:
    tenant_id = require_tenant_id()
    statement = (
        select(DocumentChunk, Document, DocumentVersion)
        .join(Document, Document.id == DocumentChunk.document_id)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.version_id)
        .where(
            DocumentChunk.tenant_id == tenant_id,
            Document.tenant_id == tenant_id,
            DocumentVersion.tenant_id == tenant_id,
            DocumentChunk.id.in_(chunk_ids),
            DocumentChunk.embedding_model == embedding_model,
            DocumentChunk.embedding_version == embedding_version,
            Document.workspace_id == scope.workspace_id,
            Document.status == DocumentStatus.ACTIVE,
            Document.active_version_id == DocumentChunk.version_id,
        )
    )
    condition = _project_scope_condition(scope)
    if condition is not None:
        statement = statement.where(condition)

    rows = (await session.execute(statement)).all()
    records: dict[UUID, tuple[DocumentChunk, Document, DocumentVersion]] = {}
    for chunk, document, version in rows:
        if not scope.allows(document.project_id):
            continue
        if version.status in _UNTRUSTED_RECORD_STATUSES:
            continue
        records[chunk.id] = (chunk, document, version)
    return records
