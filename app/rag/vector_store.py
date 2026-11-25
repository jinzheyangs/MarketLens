"""Qdrant 向量库：候选阶段按租户/ACL 载荷过滤。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchAny,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from app.core.config import Settings
from app.rag.errors import VectorStoreError
from app.rag.types import VectorChunk, VectorFilter, VectorHit

_IMPOSSIBLE_PROJECT = "__no_accessible_scope__"


class QdrantVectorStore:
    def __init__(self, settings: Settings) -> None:
        self._collection = settings.qdrant_collection
        if settings.qdrant_local_mode:
            self._client = QdrantClient(location=":memory:")
        else:
            if not settings.qdrant_url:
                raise VectorStoreError("QDRANT_URL is not configured")
            self._client = QdrantClient(
                url=settings.qdrant_url,
                api_key=(
                    settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
                ),
                timeout=max(1, int(settings.qdrant_timeout_seconds)),
            )
        self._local = settings.qdrant_local_mode

    async def ensure_collection(self, dimension: int) -> None:
        await asyncio.to_thread(self._ensure_collection_sync, dimension)

    def _ensure_collection_sync(self, dimension: int) -> None:
        try:
            exists = self._client.collection_exists(self._collection)
            if exists:
                info = self._client.get_collection(self._collection)
                vectors = info.config.params.vectors
                current_size = getattr(vectors, "size", None)
                if current_size != dimension:
                    raise VectorStoreError(
                        "Qdrant collection dimension does not match EMBEDDING_DIMENSION"
                    )
            else:
                self._client.create_collection(
                    collection_name=self._collection,
                    vectors_config=VectorParams(size=dimension, distance=Distance.COSINE),
                )

            if not self._local:
                for field_name in (
                    "tenant_id",
                    "workspace_id",
                    "project_id",
                    "document_id",
                    "version_id",
                    "embedding_model",
                    "embedding_version",
                ):
                    self._client.create_payload_index(
                        collection_name=self._collection,
                        field_name=field_name,
                        field_schema=PayloadSchemaType.KEYWORD,
                    )
        except VectorStoreError:
            raise
        except Exception as exc:
            raise VectorStoreError("Qdrant collection initialization failed") from exc

    async def upsert(self, chunks: Sequence[VectorChunk]) -> None:
        if not chunks:
            return
        points = [
            PointStruct(id=str(chunk.id), vector=chunk.vector, payload=chunk.payload)
            for chunk in chunks
        ]
        try:
            await asyncio.to_thread(
                self._client.upsert,
                collection_name=self._collection,
                points=points,
                wait=True,
            )
        except Exception as exc:
            raise VectorStoreError("Qdrant upsert failed") from exc

    async def delete_version(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        version_id: UUID,
    ) -> None:
        query_filter = Filter(
            must=[
                FieldCondition(key="tenant_id", match=MatchValue(value=str(tenant_id))),
                FieldCondition(key="document_id", match=MatchValue(value=str(document_id))),
                FieldCondition(key="version_id", match=MatchValue(value=str(version_id))),
            ]
        )
        await self._delete_by_filter(query_filter)

    async def delete_document(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
    ) -> None:
        query_filter = Filter(
            must=[
                FieldCondition(key="tenant_id", match=MatchValue(value=str(tenant_id))),
                FieldCondition(key="document_id", match=MatchValue(value=str(document_id))),
            ]
        )
        await self._delete_by_filter(query_filter)

    async def _delete_by_filter(self, query_filter: Filter) -> None:
        try:
            await asyncio.to_thread(
                self._client.delete,
                collection_name=self._collection,
                points_selector=FilterSelector(filter=query_filter),
                wait=True,
            )
        except Exception as exc:
            raise VectorStoreError("Qdrant delete failed") from exc

    async def search(
        self,
        *,
        vector: Sequence[float],
        query_filter: VectorFilter,
        limit: int,
    ) -> list[VectorHit]:
        qdrant_filter = self._build_filter(query_filter)
        try:
            response = await asyncio.to_thread(
                self._client.query_points,
                collection_name=self._collection,
                query=list(vector),
                query_filter=qdrant_filter,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            raise VectorStoreError("Qdrant search failed") from exc

        hits: list[VectorHit] = []
        for point in response.points:
            try:
                point_id = UUID(str(point.id))
            except ValueError:
                continue
            hits.append(
                VectorHit(
                    id=point_id,
                    score=float(point.score),
                    payload=dict(point.payload or {}),
                )
            )
        return hits

    @staticmethod
    def _build_filter(query_filter: VectorFilter) -> Filter:
        must: list[Any] = [
            FieldCondition(key="tenant_id", match=MatchValue(value=str(query_filter.tenant_id))),
            FieldCondition(
                key="workspace_id",
                match=MatchValue(value=str(query_filter.workspace_id)),
            ),
        ]
        if query_filter.embedding_model is not None:
            must.append(
                FieldCondition(
                    key="embedding_model",
                    match=MatchValue(value=query_filter.embedding_model),
                )
            )
        if query_filter.embedding_version is not None:
            must.append(
                FieldCondition(
                    key="embedding_version",
                    match=MatchValue(value=query_filter.embedding_version),
                )
            )
        should: list[Any] = []

        if query_filter.allowed_project_ids is None:
            if not query_filter.include_workspace_documents:
                must.append(
                    FieldCondition(
                        key="project_id",
                        match=MatchValue(value=_IMPOSSIBLE_PROJECT),
                    )
                )
            elif query_filter.selected_project_id is not None:
                should = [
                    FieldCondition(key="project_id", match=MatchValue(value="none")),
                    FieldCondition(
                        key="project_id",
                        match=MatchValue(value=str(query_filter.selected_project_id)),
                    ),
                ]
        else:
            if query_filter.include_workspace_documents:
                should.append(FieldCondition(key="project_id", match=MatchValue(value="none")))
            if query_filter.allowed_project_ids:
                should.append(
                    FieldCondition(
                        key="project_id",
                        match=MatchAny(
                            any=[str(project_id) for project_id in query_filter.allowed_project_ids]
                        ),
                    )
                )
            if not should:
                must.append(
                    FieldCondition(
                        key="project_id",
                        match=MatchValue(value=_IMPOSSIBLE_PROJECT),
                    )
                )

        return Filter(must=must, should=should or None)

    async def health(self) -> None:
        try:
            exists = await asyncio.to_thread(self._client.collection_exists, self._collection)
        except Exception as exc:
            raise VectorStoreError("Qdrant is unavailable") from exc
        if not exists:
            raise VectorStoreError("Qdrant collection is missing")

    async def aclose(self) -> None:
        await asyncio.to_thread(self._client.close)


def build_vector_store(settings: Settings) -> QdrantVectorStore:
    return QdrantVectorStore(settings)
