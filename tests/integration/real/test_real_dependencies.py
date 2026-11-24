"""Optional real-service integration tests.

These tests are skipped unless explicit endpoints are provided:

- ``QDRANT_TEST_URL``
- ``S3_TEST_ENDPOINT_URL``, ``S3_TEST_ACCESS_KEY``, ``S3_TEST_SECRET_KEY``
- ``KAFKA_TEST_BOOTSTRAP_SERVERS``
- ``TEMPORAL_TEST_ADDRESS``

Run with:
    pytest -m real tests/integration/real -q

The tests never point at production data: they create random collections,
buckets, topics, workflow IDs, and tenants.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from aiokafka import AIOKafkaConsumer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from cryptography.hazmat.primitives.asymmetric import rsa
from qdrant_client import QdrantClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker
from tests.conftest import DEFAULT_TEST_DATABASE_URL, make_settings, open_client
from tests.integration.test_api_stage2 import AUDIENCE, ISSUER, Seed, _auth, _seed_database

from app.auth.oidc import JWTVerifier
from app.core.context import tenant_context
from app.db.models import DomainEvent, Tenant
from app.events.outbox import OutboxDispatcher
from app.events.publisher import KafkaEventPublisher
from app.events.schemas import DomainEventEnvelope
from app.factory import create_app
from app.rag.errors import ObjectStorageError
from app.rag.storage import S3ObjectStorage
from app.rag.types import VectorChunk, VectorFilter
from app.rag.vector_store import QdrantVectorStore
from app.workflow.workflows import ResearchLifecycleWorkflow

pytestmark = [pytest.mark.integration, pytest.mark.real]


def _qdrant_url() -> str:
    value = os.getenv("QDRANT_TEST_URL")
    if not value:
        pytest.skip("QDRANT_TEST_URL is not set")
    return value


def _kafka_servers() -> list[str]:
    value = os.getenv("KAFKA_TEST_BOOTSTRAP_SERVERS")
    if not value:
        pytest.skip("KAFKA_TEST_BOOTSTRAP_SERVERS is not set")
    return [item.strip() for item in value.split(",") if item.strip()]


async def test_real_qdrant_tenant_isolation() -> None:
    collection = f"marketscope_it_{uuid4().hex[:12]}"
    settings = make_settings(
        qdrant_url=_qdrant_url(),
        qdrant_local_mode=False,
        qdrant_collection=collection,
        embedding_dimension=8,
    )
    store = QdrantVectorStore(settings)
    await store.ensure_collection(8)
    tenant_a, tenant_b = uuid4(), uuid4()
    workspace_a, workspace_b = uuid4(), uuid4()
    project_a, project_b = uuid4(), uuid4()
    common = {"embedding_model": "real-it", "embedding_version": "v1"}
    await store.upsert(
        [
            VectorChunk(
                id=uuid4(),
                vector=[1.0, 0, 0, 0, 0, 0, 0, 0],
                payload={
                    **common,
                    "tenant_id": str(tenant_a),
                    "workspace_id": str(workspace_a),
                    "project_id": str(project_a),
                    "document_id": str(uuid4()),
                    "version_id": str(uuid4()),
                },
            ),
            VectorChunk(
                id=uuid4(),
                vector=[1.0, 0, 0, 0, 0, 0, 0, 0],
                payload={
                    **common,
                    "tenant_id": str(tenant_b),
                    "workspace_id": str(workspace_b),
                    "project_id": str(project_b),
                    "document_id": str(uuid4()),
                    "version_id": str(uuid4()),
                },
            ),
        ]
    )
    try:
        hits = await store.search(
            vector=[1.0, 0, 0, 0, 0, 0, 0, 0],
            query_filter=VectorFilter(
                tenant_id=tenant_a,
                workspace_id=workspace_a,
                allowed_project_ids=frozenset({project_a}),
                include_workspace_documents=False,
                selected_project_id=project_a,
                embedding_model="real-it",
                embedding_version="v1",
            ),
            limit=10,
        )
        assert len(hits) == 1
        assert hits[0].payload["tenant_id"] == str(tenant_a)
    finally:
        QdrantClient(url=_qdrant_url()).delete_collection(collection)


async def test_real_s3_object_storage() -> None:
    endpoint = os.getenv("S3_TEST_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("S3_TEST_ENDPOINT_URL is not set")
    settings = make_settings(
        object_storage_backend="s3",
        s3_endpoint_url=endpoint,
        s3_bucket=f"marketscope-it-{uuid4().hex[:10]}",
        s3_access_key=os.getenv("S3_TEST_ACCESS_KEY", "test-key"),
        s3_secret_key=os.getenv("S3_TEST_SECRET_KEY", "test-secret"),
        s3_use_ssl=False,
        s3_addressing_style="path",
        s3_auto_create_bucket=True,
    )
    storage = S3ObjectStorage(settings)
    await storage.ensure_ready()
    key = f"it/{uuid4()}.bin"
    payload = b"real-s3-object"
    try:
        await storage.put(key, payload, "application/octet-stream")
        assert await storage.get(key) == payload
        await storage.delete(key)
        with pytest.raises(ObjectStorageError):
            await storage.get(key)
    finally:
        await storage.aclose()


async def test_real_kafka_publish_and_consume() -> None:
    servers = _kafka_servers()
    topic = f"marketscope-it-{uuid4().hex[:10]}"
    admin = AIOKafkaAdminClient(bootstrap_servers=servers)
    await admin.start()
    await admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])
    await admin.close()
    settings = make_settings(
        event_publisher="kafka",
        kafka_bootstrap_servers=servers,
        event_topic=topic,
        kafka_security_protocol="PLAINTEXT",
    )
    envelope = DomainEventEnvelope(
        event_id=uuid4(),
        event_type="integration.test",
        event_version=1,
        tenant_id=uuid4(),
        aggregate_type="test",
        aggregate_id=uuid4(),
        occurred_at=datetime.now(UTC),
        partition_key="real-key",
        payload={"message": "real kafka"},
        headers={},
    )
    publisher = KafkaEventPublisher(settings)
    await publisher.start()
    await publisher.publish(envelope)
    await publisher.aclose()

    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=servers,
        group_id=f"it-{uuid4().hex[:8]}",
        auto_offset_reset="earliest",
    )
    await consumer.start()
    try:
        message = await asyncio.wait_for(consumer.getone(), timeout=20)
        parsed = DomainEventEnvelope.model_validate_json(message.value)
        assert parsed.event_id == envelope.event_id
        assert parsed.payload == envelope.payload
    finally:
        await consumer.stop()


async def test_real_outbox_dispatches_to_kafka(session_factory, test_database_url) -> None:
    servers = _kafka_servers()
    topic = f"marketscope-outbox-{uuid4().hex[:10]}"
    admin = AIOKafkaAdminClient(bootstrap_servers=servers)
    await admin.start()
    await admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])
    await admin.close()
    settings = make_settings(
        database_url=test_database_url or DEFAULT_TEST_DATABASE_URL,
        event_publisher="kafka",
        kafka_bootstrap_servers=servers,
        event_topic=topic,
        kafka_security_protocol="PLAINTEXT",
        events_outbox_enabled=True,
        event_outbox_batch_size=10,
        event_outbox_max_attempts=3,
    )
    tenant_id = uuid4()
    event_id = uuid4()
    async with session_factory() as session:
        session.add(Tenant(id=tenant_id, slug=f"real-{tenant_id.hex[:8]}", name="Real IT"))
        await session.commit()
    with tenant_context(tenant_id):
        async with session_factory() as session, session.begin():
            session.add(
                DomainEvent(
                    tenant_id=tenant_id,
                    event_id=event_id,
                    event_type="integration.outbox",
                    event_version=1,
                    aggregate_type="test",
                    aggregate_id=uuid4(),
                    payload={"outbox": True},
                    headers={},
                    partition_key="real-key",
                    idempotency_key=f"it:{event_id}",
                    status="pending",
                    attempts=0,
                    max_attempts=3,
                    occurred_at=datetime.now(UTC),
                )
            )
    publisher = KafkaEventPublisher(settings)
    await publisher.start()
    dispatcher = OutboxDispatcher(
        settings=settings,
        publisher=publisher,
        session_factory=session_factory,
    )
    processed = await dispatcher.dispatch_once(tenant_ids=[tenant_id])
    await publisher.aclose()
    assert processed == 1
    with tenant_context(tenant_id):
        async with session_factory() as session:
            event = await session.scalar(
                select(DomainEvent).where(DomainEvent.event_id == event_id)
            )
            assert event is not None and event.status == "published"
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=servers,
        group_id=f"it-{uuid4().hex[:8]}",
        auto_offset_reset="earliest",
    )
    await consumer.start()
    try:
        message = await asyncio.wait_for(consumer.getone(), timeout=20)
        assert b"integration.outbox" in message.value
    finally:
        await consumer.stop()


async def test_real_temporal_workflow() -> None:
    address = os.getenv("TEMPORAL_TEST_ADDRESS")
    if not address:
        pytest.skip("TEMPORAL_TEST_ADDRESS is not set")
    state = {"run": "pending", "review": None, "research": 0, "reviews": 0}

    @activity.defn(name="get_run_state")
    async def get_run_state(tenant_id: str, run_id: str) -> dict[str, object]:
        del tenant_id, run_id
        return {
            "run_status": state["run"],
            "review_status": state["review"],
            "pause_requested": False,
            "cancel_requested": False,
        }

    @activity.defn(name="run_research_batch")
    async def run_research_batch(tenant_id: str) -> int:
        del tenant_id
        state["research"] += 1
        if state["research"] >= 2:
            state["run"] = "completed"
            state["review"] = "pending"
        return 1

    @activity.defn(name="run_review_batch")
    async def run_review_batch(tenant_id: str) -> int:
        del tenant_id
        state["reviews"] += 1
        if state["run"] == "completed":
            state["review"] = "completed"
        return 1

    @activity.defn(name="run_export_batch")
    async def run_export_batch(tenant_id: str) -> int:
        del tenant_id
        return 0

    client = await Client.connect(address, namespace="default")
    queue = f"it-{uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[ResearchLifecycleWorkflow],
        activities=[
            get_run_state,
            run_research_batch,
            run_review_batch,
            run_export_batch,
        ],
    ):
        result = await client.execute_workflow(
            ResearchLifecycleWorkflow.run,
            {
                "tenant_id": str(uuid4()),
                "run_id": str(uuid4()),
                "poll_seconds": 0.5,
                "deadline_seconds": 60,
            },
            id=f"it-{uuid4()}",
            task_queue=queue,
            execution_timeout=timedelta(seconds=60),
        )
    assert result["workflow_result"] == "completed"
    assert state["research"] >= 2
    assert state["reviews"] >= 1


@pytest.fixture
async def real_seed(
    session_factory: async_sessionmaker[AsyncSession],
    jwt_private_key: rsa.RSAPrivateKey,
) -> Seed:
    return await _seed_database(session_factory, jwt_private_key)


async def test_real_rag_upload_index_search(
    real_seed: Seed,
    test_database_url: str,
    jwt_public_key: rsa.RSAPublicKey,
) -> None:
    qdrant_url = _qdrant_url()
    s3_endpoint = os.getenv("S3_TEST_ENDPOINT_URL")
    if not s3_endpoint:
        pytest.skip("S3_TEST_ENDPOINT_URL is not set")
    collection = f"rag_real_{uuid4().hex[:10]}"
    settings = make_settings(
        database_url=test_database_url,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        log_level="WARNING",
        rag_enabled=True,
        qdrant_url=qdrant_url,
        qdrant_local_mode=False,
        qdrant_collection=collection,
        object_storage_backend="s3",
        s3_endpoint_url=s3_endpoint,
        s3_bucket=f"marketscope-rag-{uuid4().hex[:10]}",
        s3_access_key=os.getenv("S3_TEST_ACCESS_KEY", "test-key"),
        s3_secret_key=os.getenv("S3_TEST_SECRET_KEY", "test-secret"),
        s3_use_ssl=False,
        s3_addressing_style="path",
        s3_auto_create_bucket=True,
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
    token = real_seed.tokens["workspace_editor_a"]
    async with open_client(application) as client:
        response = await client.post(
            f"/api/v1/workspaces/{real_seed.workspace_a}/documents",
            headers=_auth(token),
            files={
                "file": (
                    "real.txt",
                    b"Alpha beta gamma.\n\nSecond paragraph.",
                    "text/plain",
                )
            },
            data={"title": "Real RAG"},
        )
        assert response.status_code == 201, response.text
        document = response.json()
        version = document["versions"][0]
        indexed = await client.post(
            f"/api/v1/documents/{document['id']}/versions/{version['id']}/index",
            headers=_auth(token),
        )
        assert indexed.status_code == 200, indexed.text
        assert indexed.json()["status"] == "indexed"
        search = await client.post(
            f"/api/v1/workspaces/{real_seed.workspace_a}/search",
            headers=_auth(token),
            json={"query": "alpha", "limit": 5},
        )
        assert search.status_code == 200, search.text
        results = search.json()["results"]
        assert results
        assert results[0]["document_id"] == document["id"]
        assert "Alpha" in results[0]["content"]
    QdrantClient(url=qdrant_url).delete_collection(collection)
