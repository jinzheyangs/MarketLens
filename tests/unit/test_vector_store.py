from __future__ import annotations

from uuid import uuid4

import pytest

from app.rag.errors import VectorStoreError
from app.rag.types import VectorChunk, VectorFilter
from app.rag.vector_store import QdrantVectorStore
from tests.conftest import make_settings


def _settings():
    return make_settings(
        rag_enabled=True,
        qdrant_local_mode=True,
        embedding_dimension=8,
        qdrant_collection=f"test_chunks_{uuid4().hex[:8]}",
    )


def _payload(
    tenant_id,
    workspace_id,
    *,
    project_id: str = "none",
    document_id,
    version_id,
    embedding_model: str = "hash-v1",
    embedding_version: str = "1",
) -> dict[str, object]:
    return {
        "tenant_id": str(tenant_id),
        "workspace_id": str(workspace_id),
        "project_id": project_id,
        "document_id": str(document_id),
        "version_id": str(version_id),
        "embedding_model": embedding_model,
        "embedding_version": embedding_version,
    }


async def test_vector_filters_apply_tenant_and_project_scope() -> None:
    store = QdrantVectorStore(_settings())
    await store.ensure_collection(8)
    tenant_a, tenant_b = uuid4(), uuid4()
    workspace_a, workspace_b = uuid4(), uuid4()
    project = uuid4()
    workspace_doc, project_doc = uuid4(), uuid4()
    workspace_version, project_version = uuid4(), uuid4()

    await store.upsert(
        [
            VectorChunk(
                id=uuid4(),
                vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                payload=_payload(
                    tenant_a,
                    workspace_a,
                    document_id=workspace_doc,
                    version_id=workspace_version,
                ),
            ),
            VectorChunk(
                id=uuid4(),
                vector=[0.9, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                payload=_payload(
                    tenant_a,
                    workspace_a,
                    project_id=str(project),
                    document_id=project_doc,
                    version_id=project_version,
                ),
            ),
            VectorChunk(
                id=uuid4(),
                vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                payload=_payload(
                    tenant_b,
                    workspace_b,
                    document_id=uuid4(),
                    version_id=uuid4(),
                ),
            ),
        ]
    )
    vector = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    all_workspace = await store.search(
        vector=vector,
        query_filter=VectorFilter(
            tenant_id=tenant_a,
            workspace_id=workspace_a,
            allowed_project_ids=None,
            include_workspace_documents=True,
            selected_project_id=None,
        ),
        limit=10,
    )
    assert {hit.payload["document_id"] for hit in all_workspace} == {
        str(workspace_doc),
        str(project_doc),
    }

    project_only = await store.search(
        vector=vector,
        query_filter=VectorFilter(
            tenant_id=tenant_a,
            workspace_id=workspace_a,
            allowed_project_ids=frozenset({project}),
            include_workspace_documents=False,
            selected_project_id=None,
        ),
        limit=10,
    )
    assert [hit.payload["document_id"] for hit in project_only] == [str(project_doc)]

    workspace_and_selected = await store.search(
        vector=vector,
        query_filter=VectorFilter(
            tenant_id=tenant_a,
            workspace_id=workspace_a,
            allowed_project_ids=frozenset({project}),
            include_workspace_documents=True,
            selected_project_id=project,
        ),
        limit=10,
    )
    assert len(workspace_and_selected) == 2

    other_tenant = await store.search(
        vector=vector,
        query_filter=VectorFilter(
            tenant_id=tenant_b,
            workspace_id=workspace_b,
            allowed_project_ids=None,
            include_workspace_documents=True,
            selected_project_id=None,
        ),
        limit=10,
    )
    assert len(other_tenant) == 1

    await store.delete_version(
        tenant_id=tenant_a,
        document_id=workspace_doc,
        version_id=workspace_version,
    )
    remaining = await store.search(
        vector=vector,
        query_filter=VectorFilter(
            tenant_id=tenant_a,
            workspace_id=workspace_a,
            allowed_project_ids=None,
            include_workspace_documents=True,
            selected_project_id=None,
        ),
        limit=10,
    )
    assert [hit.payload["document_id"] for hit in remaining] == [str(project_doc)]

    await store.delete_document(tenant_id=tenant_a, document_id=project_doc)
    assert (
        await store.search(
            vector=vector,
            query_filter=VectorFilter(
                tenant_id=tenant_a,
                workspace_id=workspace_a,
                allowed_project_ids=None,
                include_workspace_documents=True,
                selected_project_id=None,
            ),
            limit=10,
        )
        == []
    )
    await store.aclose()


async def test_embedding_version_filter_excludes_stale_vectors() -> None:
    store = QdrantVectorStore(_settings())
    await store.ensure_collection(8)
    tenant_id, workspace_id = uuid4(), uuid4()
    current_document, stale_document = uuid4(), uuid4()
    current_version, stale_version = uuid4(), uuid4()
    vector = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    await store.upsert(
        [
            VectorChunk(
                id=uuid4(),
                vector=vector,
                payload=_payload(
                    tenant_id,
                    workspace_id,
                    document_id=current_document,
                    version_id=current_version,
                ),
            ),
            VectorChunk(
                id=uuid4(),
                vector=vector,
                payload=_payload(
                    tenant_id,
                    workspace_id,
                    document_id=stale_document,
                    version_id=stale_version,
                    embedding_model="old-model",
                ),
            ),
        ]
    )
    hits = await store.search(
        vector=vector,
        query_filter=VectorFilter(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            allowed_project_ids=None,
            include_workspace_documents=True,
            selected_project_id=None,
            embedding_model="hash-v1",
            embedding_version="1",
        ),
        limit=10,
    )
    assert [hit.payload["document_id"] for hit in hits] == [str(current_document)]
    await store.aclose()


async def test_collection_dimension_mismatch_is_rejected() -> None:
    store = QdrantVectorStore(_settings())
    await store.ensure_collection(8)
    with pytest.raises(VectorStoreError):
        await store.ensure_collection(4)
    await store.aclose()
