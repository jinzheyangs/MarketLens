from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.oidc import JWTVerifier
from app.core.context import tenant_context
from app.db.models import Document, DocumentChunk, DocumentVersion
from app.factory import create_app
from app.rag.errors import EmbeddingError, ObjectStorageError, VectorStoreError
from app.rag.types import VectorFilter
from app.services.rag import RagServices
from tests.conftest import make_settings, open_client
from tests.integration.test_api_stage2 import (
    AUDIENCE,
    ISSUER,
    Seed,
    _auth,
    _seed_database,
)

pytestmark = pytest.mark.integration


class FailingEmbedding:
    dimension = 8
    model = "failing"
    version = "1"

    async def embed_texts(self, texts):
        raise EmbeddingError("forced embedding failure")

    async def embed_query(self, text):
        raise EmbeddingError("forced embedding failure")

    async def aclose(self) -> None:
        return None


class FailingVectorStore:
    async def delete_document(self, **kwargs) -> None:
        raise VectorStoreError("forced vector deletion failure")


@dataclass
class RagHarness:
    client: AsyncClient
    seed: Seed
    app: FastAPI
    rag: RagServices


@pytest.fixture
async def seed(
    session_factory: async_sessionmaker[AsyncSession],
    jwt_private_key: rsa.RSAPrivateKey,
) -> Seed:
    return await _seed_database(session_factory, jwt_private_key)


@pytest.fixture
async def rag_api(
    seed: Seed,
    test_database_url: str,
    jwt_public_key: rsa.RSAPublicKey,
    tmp_path: Path,
) -> RagHarness:
    settings = make_settings(
        database_url=test_database_url,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        log_level="WARNING",
        rag_enabled=True,
        qdrant_local_mode=True,
        qdrant_collection=f"rag_test_{uuid4().hex[:8]}",
        object_storage_backend="local",
        object_storage_local_path=str(tmp_path / "objects"),
        embedding_provider="hash",
        embedding_dimension=8,
        embedding_model="hash-v1",
        embedding_version="1",
        chunk_max_chars=200,
        chunk_overlap_chars=20,
        search_candidate_limit=20,
        document_max_bytes=1_000_000,
    )
    application = create_app(settings)
    application.state.token_verifier = JWTVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithms=("RS256",),
        key_resolver=lambda _raw_token: jwt_public_key,
        leeway_seconds=0,
    )
    async with open_client(application) as client:
        services = application.state.rag_services
        assert isinstance(services, RagServices)
        yield RagHarness(
            client=client,
            seed=seed,
            app=application,
            rag=services,
        )


async def _upload(
    api: RagHarness,
    token: str,
    *,
    text: str,
    project_id: UUID | None = None,
    filename: str = "notes.txt",
    title: str = "Document",
) -> dict:
    data = {"title": title}
    if project_id is not None:
        data["project_id"] = str(project_id)
    response = await api.client.post(
        f"/api/v1/workspaces/{api.seed.workspace_a}/documents",
        headers=_auth(token),
        files={"file": (filename, text.encode("utf-8"), "text/plain")},
        data=data,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _index(api: RagHarness, token: str, document: dict) -> dict:
    version = document["versions"][0]
    response = await api.client.post(
        f"/api/v1/documents/{document['id']}/versions/{version['id']}/index",
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _search(
    api: RagHarness,
    token: str,
    query: str,
    *,
    project_id: UUID | None = None,
) -> list[dict]:
    payload: dict[str, object] = {"query": query, "limit": 10}
    if project_id is not None:
        payload["project_id"] = str(project_id)
    response = await api.client.post(
        f"/api/v1/workspaces/{api.seed.workspace_a}/search",
        headers=_auth(token),
        json=payload,
    )
    assert response.status_code == 200, response.text
    return response.json()["results"]


async def test_upload_index_search_returns_citation_locator(rag_api: RagHarness) -> None:
    token = rag_api.seed.tokens["workspace_editor_a"]
    document = await _upload(
        rag_api,
        token,
        text="Alpha beta gamma.\n\nSecond paragraph with delta.",
        title="Research Notes",
    )
    version = await _index(rag_api, token, document)
    assert version["status"] == "indexed"
    assert version["page_count"] == 1

    results = await _search(rag_api, token, "alpha")
    assert results
    first = results[0]
    assert first["document_id"] == document["id"]
    assert first["version_number"] == 1
    assert "Alpha" in first["content"]
    assert first["page_number"] == 1
    assert first["paragraph_number"] == 1
    assert first["start_offset"] == 0
    assert first["end_offset"] > first["start_offset"]
    assert first["untrusted"] is True
    assert first["score"] > 0

    detail = await rag_api.client.get(f"/api/v1/documents/{document['id']}", headers=_auth(token))
    assert detail.status_code == 200
    body = detail.json()
    assert body["active_version_id"] == version["id"]
    assert body["versions"][0]["status"] == "indexed"


async def test_project_acl_filters_workspace_and_project_documents(rag_api: RagHarness) -> None:
    workspace_editor = rag_api.seed.tokens["workspace_editor_a"]
    project_editor = rag_api.seed.tokens["project_editor_a"]

    workspace_document = await _upload(
        rag_api,
        workspace_editor,
        text="shared token from the workspace document",
        title="Workspace document",
    )
    await _index(rag_api, workspace_editor, workspace_document)

    project_document = await _upload(
        rag_api,
        project_editor,
        text="shared token from the project document",
        project_id=rag_api.seed.project_a,
        title="Project document",
    )
    await _index(rag_api, project_editor, project_document)

    project_results = await _search(rag_api, project_editor, "token")
    assert {result["document_id"] for result in project_results} == {project_document["id"]}

    workspace_results = await _search(rag_api, workspace_editor, "token")
    assert {result["document_id"] for result in workspace_results} == {
        workspace_document["id"],
        project_document["id"],
    }

    hidden = await rag_api.client.get(
        f"/api/v1/documents/{workspace_document['id']}",
        headers=_auth(project_editor),
    )
    assert hidden.status_code == 404

    listing = await rag_api.client.get(
        f"/api/v1/workspaces/{rag_api.seed.workspace_a}/documents",
        headers=_auth(project_editor),
    )
    assert listing.status_code == 200
    assert {item["id"] for item in listing.json()} == {project_document["id"]}


async def test_cross_tenant_search_and_document_access(rag_api: RagHarness) -> None:
    token_a = rag_api.seed.tokens["workspace_editor_a"]
    token_b = rag_api.seed.tokens["tenant_admin_b"]

    document = await _upload(
        rag_api,
        token_a,
        text="cross tenant isolation token",
        title="Tenant A document",
    )
    await _index(rag_api, token_a, document)

    results_a = await _search(rag_api, token_a, "isolation")
    assert {result["document_id"] for result in results_a} == {document["id"]}

    cross_search = await rag_api.client.post(
        f"/api/v1/workspaces/{rag_api.seed.workspace_b}/search",
        headers=_auth(token_b),
        json={"query": "isolation", "limit": 10},
    )
    assert cross_search.status_code == 200
    assert cross_search.json()["results"] == []

    cross_get = await rag_api.client.get(
        f"/api/v1/documents/{document['id']}", headers=_auth(token_b)
    )
    assert cross_get.status_code == 404

    cross_delete = await rag_api.client.delete(
        f"/api/v1/documents/{document['id']}", headers=_auth(token_b)
    )
    assert cross_delete.status_code == 404


async def test_version_switch_and_idempotent_reindex(rag_api: RagHarness) -> None:
    token = rag_api.seed.tokens["workspace_editor_a"]
    document = await _upload(
        rag_api,
        token,
        text="apple orchard content",
        title="Versioned document",
    )
    first_version = await _index(rag_api, token, document)
    assert first_version["version_number"] == 1

    second_upload = await rag_api.client.post(
        f"/api/v1/documents/{document['id']}/versions",
        headers=_auth(token),
        files={
            "file": (
                "notes-v2.txt",
                b"banana plantation content",
                "text/plain",
            )
        },
    )
    assert second_upload.status_code == 201, second_upload.text
    second_version = second_upload.json()
    assert second_version["version_number"] == 2
    indexed_second = await rag_api.client.post(
        f"/api/v1/documents/{document['id']}/versions/{second_version['id']}/index",
        headers=_auth(token),
    )
    assert indexed_second.status_code == 200
    assert indexed_second.json()["status"] == "indexed"

    apple_results = await _search(rag_api, token, "apple")
    assert all(result["version_number"] == 2 for result in apple_results)
    assert all("apple" not in result["content"].lower() for result in apple_results)

    banana_results = await _search(rag_api, token, "banana")
    assert banana_results
    assert all(result["version_number"] == 2 for result in banana_results)

    detail = await rag_api.client.get(f"/api/v1/documents/{document['id']}", headers=_auth(token))
    body = detail.json()
    assert body["active_version_id"] == second_version["id"]
    statuses = {version["version_number"]: version["status"] for version in body["versions"]}
    assert statuses == {1: "superseded", 2: "indexed"}

    async def chunk_count() -> int:
        with tenant_context(rag_api.seed.tenant_a):
            async with rag_api.seed.session_factory() as session:
                return int(
                    await session.scalar(
                        select(func.count())
                        .select_from(DocumentChunk)
                        .where(
                            DocumentChunk.tenant_id == rag_api.seed.tenant_a,
                            DocumentChunk.version_id == UUID(second_version["id"]),
                        )
                    )
                    or 0
                )

    before = await chunk_count()
    for _ in range(2):
        repeated = await rag_api.client.post(
            f"/api/v1/documents/{document['id']}/versions/{second_version['id']}/index",
            headers=_auth(token),
        )
        assert repeated.status_code == 200
        assert repeated.json()["status"] == "indexed"
    assert await chunk_count() == before


async def test_delete_propagates_to_storage_chunks_and_vectors(rag_api: RagHarness) -> None:
    uploader = rag_api.seed.tokens["workspace_editor_a"]
    deleter = rag_api.seed.tokens["workspace_admin_a"]
    document = await _upload(
        rag_api,
        uploader,
        text="deletion propagation token",
        title="Deletable document",
    )
    version = await _index(rag_api, uploader, document)

    with tenant_context(rag_api.seed.tenant_a):
        async with rag_api.seed.session_factory() as session:
            stored = await session.scalar(
                select(DocumentVersion).where(
                    DocumentVersion.tenant_id == rag_api.seed.tenant_a,
                    DocumentVersion.id == UUID(version["id"]),
                )
            )
    assert stored is not None
    object_key = stored.object_key
    assert await rag_api.rag.storage.get(object_key) == b"deletion propagation token"

    deleted = await rag_api.client.delete(
        f"/api/v1/documents/{document['id']}", headers=_auth(deleter)
    )
    assert deleted.status_code == 204

    assert await _search(rag_api, uploader, "deletion") == []
    with tenant_context(rag_api.seed.tenant_a):
        async with rag_api.seed.session_factory() as session:
            document_row = await session.scalar(
                select(Document).where(
                    Document.tenant_id == rag_api.seed.tenant_a,
                    Document.id == UUID(document["id"]),
                )
            )
            chunks = await session.scalar(
                select(func.count())
                .select_from(DocumentChunk)
                .where(
                    DocumentChunk.tenant_id == rag_api.seed.tenant_a,
                    DocumentChunk.document_id == UUID(document["id"]),
                )
            )
    assert document_row is not None
    assert document_row.status == "deleted"
    assert chunks == 0

    with pytest.raises(ObjectStorageError):
        await rag_api.rag.storage.get(object_key)

    vector = await rag_api.rag.embedding.embed_query("deletion")
    hits = await rag_api.rag.vector_store.search(
        vector=vector,
        query_filter=VectorFilter(
            tenant_id=rag_api.seed.tenant_a,
            workspace_id=rag_api.seed.workspace_a,
            allowed_project_ids=None,
            include_workspace_documents=True,
            selected_project_id=None,
        ),
        limit=10,
    )
    assert hits == []

    audit = await rag_api.client.get(
        "/api/v1/audit-events",
        headers=_auth(rag_api.seed.tokens["tenant_admin_a"]),
        params={"limit": 100},
    )
    assert audit.status_code == 200
    actions = {event["action"] for event in audit.json()}
    assert {"document.uploaded", "document.indexed", "document.deleted"} <= actions


async def test_delete_failure_is_retryable(rag_api: RagHarness) -> None:
    uploader = rag_api.seed.tokens["workspace_editor_a"]
    deleter = rag_api.seed.tokens["workspace_admin_a"]
    document = await _upload(
        rag_api,
        uploader,
        text="delete retry token",
        title="Delete retry document",
    )
    await _index(rag_api, uploader, document)

    original_vector_store = rag_api.rag.vector_store
    rag_api.rag.vector_store = FailingVectorStore()  # type: ignore[assignment]
    try:
        failed = await rag_api.client.delete(
            f"/api/v1/documents/{document['id']}", headers=_auth(deleter)
        )
    finally:
        rag_api.rag.vector_store = original_vector_store
    assert failed.status_code == 503

    with tenant_context(rag_api.seed.tenant_a):
        async with rag_api.seed.session_factory() as session:
            row = await session.scalar(
                select(Document).where(
                    Document.tenant_id == rag_api.seed.tenant_a,
                    Document.id == UUID(document["id"]),
                )
            )
    assert row is not None
    assert row.status == "delete_failed"

    retried = await rag_api.client.delete(
        f"/api/v1/documents/{document['id']}", headers=_auth(deleter)
    )
    assert retried.status_code == 204
    assert await _search(rag_api, uploader, "retry") == []


async def test_index_failure_is_persisted_and_can_be_retried(rag_api: RagHarness) -> None:
    token = rag_api.seed.tokens["workspace_editor_a"]
    document = await _upload(
        rag_api,
        token,
        text="retry token content",
        title="Retry document",
    )
    original_embedding = rag_api.rag.embedding
    rag_api.rag.embedding = FailingEmbedding()
    try:
        failed = await _index(rag_api, token, document)
    finally:
        rag_api.rag.embedding = original_embedding

    assert failed["status"] == "failed"
    assert failed["error_code"] == "embedding_failed"

    retried = await _index(rag_api, token, document)
    assert retried["status"] == "indexed"
    results = await _search(rag_api, token, "retry")
    assert results
    assert "retry" in results[0]["content"].lower()


async def test_cjk_keyword_fallback(rag_api: RagHarness) -> None:
    token = rag_api.seed.tokens["workspace_editor_a"]
    document = await _upload(
        rag_api,
        token,
        text="苹果香蕉市场报告。\n\n第二段内容。",
        title="中文资料",
    )
    await _index(rag_api, token, document)
    results = await _search(rag_api, token, "香蕉")
    assert results
    assert any("香蕉" in result["content"] for result in results)


async def test_rag_disabled_fails_closed(
    seed: Seed,
    test_database_url: str,
    jwt_public_key: rsa.RSAPublicKey,
) -> None:
    settings = make_settings(
        database_url=test_database_url,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        log_level="WARNING",
        rag_enabled=False,
    )
    application = create_app(settings)
    application.state.token_verifier = JWTVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithms=("RS256",),
        key_resolver=lambda _raw_token: jwt_public_key,
        leeway_seconds=0,
    )
    async with open_client(application) as client:
        response = await client.post(
            f"/api/v1/workspaces/{seed.workspace_a}/search",
            headers=_auth(seed.tokens["workspace_editor_a"]),
            json={"query": "anything"},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


async def test_upload_permissions_and_type_validation(rag_api: RagHarness) -> None:
    viewer = rag_api.seed.tokens["workspace_viewer_a"]
    editor = rag_api.seed.tokens["workspace_editor_a"]

    denied = await rag_api.client.post(
        f"/api/v1/workspaces/{rag_api.seed.workspace_a}/documents",
        headers=_auth(viewer),
        files={"file": ("notes.txt", b"viewer upload", "text/plain")},
        data={"title": "Denied"},
    )
    assert denied.status_code == 403

    unsupported = await rag_api.client.post(
        f"/api/v1/workspaces/{rag_api.seed.workspace_a}/documents",
        headers=_auth(editor),
        files={"file": ("payload.exe", b"MZ", "application/octet-stream")},
        data={"title": "Unsupported"},
    )
    assert unsupported.status_code == 415
    assert unsupported.json()["error"]["code"] == "unsupported_media_type"
