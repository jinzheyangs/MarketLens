from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.oidc import JWTVerifier
from app.collector.errors import FetchConnectionError
from app.collector.network import FetchResult
from app.collector.rate_limit import DomainRateLimiter
from app.collector.search import SearchHit
from app.collector.service import CollectorServices
from app.collector.worker import CollectorWorker
from app.core.context import tenant_context
from app.db.models import SourceSnapshot, WebSource
from app.factory import create_app
from app.rag.errors import ObjectStorageError
from tests.conftest import make_settings, open_client
from tests.integration.test_api_stage2 import (
    AUDIENCE,
    ISSUER,
    Seed,
    _auth,
    _seed_database,
)

pytestmark = pytest.mark.integration


class FakeSearchProvider:
    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        return self.hits[:limit]

    async def aclose(self) -> None:
        return None


class FailingStorage:
    async def delete(self, key: str) -> None:
        raise ObjectStorageError("forced storage deletion failure")

    async def aclose(self) -> None:
        return None


class FakeFetcher:
    def __init__(self) -> None:
        self.fail = False
        self.calls = 0

    async def fetch(self, url: str, *, content_types=None, max_bytes=None):
        self.calls += 1
        if self.fail:
            raise FetchConnectionError("forced fetch failure")
        body = (
            b"<html><head><title>Source title</title></head><body><p>Article body</p></body></html>"
        )
        return FetchResult(
            requested_url=url,
            final_url=url,
            status_code=200,
            content_type="text/html",
            charset="utf-8",
            body=body,
            content_sha256=hashlib.sha256(body).hexdigest(),
            redirects=(),
        )


@dataclass
class CollectorHarness:
    client: AsyncClient
    seed: Seed
    app: FastAPI
    services: CollectorServices


@pytest.fixture
async def seed(
    session_factory: async_sessionmaker[AsyncSession],
    jwt_private_key: rsa.RSAPrivateKey,
) -> Seed:
    return await _seed_database(session_factory, jwt_private_key)


@pytest.fixture
async def collector_api(
    seed: Seed,
    test_database_url: str,
    jwt_public_key: rsa.RSAPublicKey,
    tmp_path: Path,
) -> CollectorHarness:
    settings = make_settings(
        database_url=test_database_url,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        log_level="WARNING",
        collector_enabled=True,
        object_storage_backend="local",
        object_storage_local_path=str(tmp_path / "collector_objects"),
        collector_respect_robots=False,
        collector_domain_min_interval_seconds=0,
        collector_retry_base_seconds=0.1,
        collector_retry_max_seconds=1,
        collector_max_attempts=2,
        collector_poll_interval_seconds=0.1,
        collector_blocked_domains=["blocked.example"],
        web_search_provider="http",
        web_search_api_url="https://search.example/query",
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
        services = application.state.collector_services
        assert isinstance(services, CollectorServices)
        if services.search_provider is not None:
            await services.search_provider.aclose()
        services.search_provider = FakeSearchProvider(
            [
                SearchHit(
                    url="https://example.com/result",
                    title="Result",
                    snippet="Snippet",
                ),
                SearchHit(
                    url="https://blocked.example/result",
                    title="Blocked",
                    snippet=None,
                ),
                SearchHit(
                    url="http://127.0.0.1/",
                    title="Private",
                    snippet=None,
                ),
            ]
        )
        yield CollectorHarness(
            client=client,
            seed=seed,
            app=application,
            services=services,
        )


async def _enqueue(
    api: CollectorHarness,
    token: str,
    urls: list[str],
    *,
    project_id: UUID | None = None,
):
    payload: dict[str, object] = {"urls": urls}
    if project_id is not None:
        payload["project_id"] = str(project_id)
    return await api.client.post(
        f"/api/v1/workspaces/{api.seed.workspace_a}/collection-tasks",
        headers=_auth(token),
        json=payload,
    )


def _worker(api: CollectorHarness, fetcher: FakeFetcher) -> CollectorWorker:
    api.services.fetcher = fetcher  # type: ignore[assignment]
    return CollectorWorker(
        settings=api.app.state.settings,
        services=api.services,
        session_factory=api.seed.session_factory,
    )


async def test_web_search_returns_only_policy_valid_untrusted_results(
    collector_api: CollectorHarness,
) -> None:
    editor = collector_api.seed.tokens["workspace_editor_a"]
    response = await collector_api.client.post(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/web-search",
        headers=_auth(editor),
        json={"query": "market", "limit": 10},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "http"
    assert body["untrusted"] is True
    assert [hit["url"] for hit in body["results"]] == ["https://example.com/result"]
    assert body["results"][0]["untrusted"] is True

    viewer = collector_api.seed.tokens["workspace_viewer_a"]
    denied = await collector_api.client.post(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/web-search",
        headers=_auth(viewer),
        json={"query": "market"},
    )
    assert denied.status_code == 403

    provider = collector_api.services.search_provider
    collector_api.services.search_provider = None
    try:
        unavailable = await collector_api.client.post(
            f"/api/v1/workspaces/{collector_api.seed.workspace_a}/web-search",
            headers=_auth(editor),
            json={"query": "market"},
        )
    finally:
        collector_api.services.search_provider = provider
    assert unavailable.status_code == 503


async def test_enqueue_worker_snapshot_dedup_and_source_detail(
    collector_api: CollectorHarness,
) -> None:
    editor = collector_api.seed.tokens["workspace_editor_a"]
    first = await _enqueue(collector_api, editor, ["https://example.com/article"])
    assert first.status_code == 202
    task = first.json()[0]
    assert task["status"] == "pending"

    duplicate = await _enqueue(collector_api, editor, ["https://example.com/article"])
    assert duplicate.status_code == 202
    assert duplicate.json()[0]["id"] == task["id"]

    fetcher = FakeFetcher()
    worker = _worker(collector_api, fetcher)
    assert await worker.run_once(tenant_ids=[collector_api.seed.tenant_a]) == 1

    task_read = await collector_api.client.get(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/collection-tasks/{task['id']}",
        headers=_auth(editor),
    )
    assert task_read.status_code == 200
    assert task_read.json()["status"] == "succeeded"
    assert task_read.json()["source_id"]

    sources = await collector_api.client.get(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/sources",
        headers=_auth(editor),
    )
    assert sources.status_code == 200
    assert len(sources.json()) == 1
    source = sources.json()[0]
    assert source["domain"] == "example.com"
    assert source["untrusted"] is True

    detail = await collector_api.client.get(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/sources/{source['id']}",
        headers=_auth(editor),
    )
    assert detail.status_code == 200
    assert "Article body" in detail.json()["latest_text"]
    assert len(detail.json()["snapshots"]) == 1

    with tenant_context(collector_api.seed.tenant_a):
        async with collector_api.seed.session_factory() as session:
            snapshot = await session.scalar(
                select(SourceSnapshot).where(
                    SourceSnapshot.tenant_id == collector_api.seed.tenant_a,
                    SourceSnapshot.source_id == UUID(source["id"]),
                )
            )
    assert snapshot is not None
    assert await collector_api.services.storage.get(snapshot.object_key)

    second = await _enqueue(collector_api, editor, ["https://example.com/article"])
    assert second.status_code == 202
    assert second.json()[0]["id"] != task["id"]
    assert await worker.run_once(tenant_ids=[collector_api.seed.tenant_a]) == 1
    assert fetcher.calls == 2

    with tenant_context(collector_api.seed.tenant_a):
        async with collector_api.seed.session_factory() as session:
            source_count = await session.scalar(
                select(func.count())
                .select_from(WebSource)
                .where(WebSource.tenant_id == collector_api.seed.tenant_a)
            )
            snapshot_count = await session.scalar(
                select(func.count())
                .select_from(SourceSnapshot)
                .where(SourceSnapshot.tenant_id == collector_api.seed.tenant_a)
            )
    assert source_count == 1
    assert snapshot_count == 1


async def test_failed_collection_retries_then_succeeds_via_api(
    collector_api: CollectorHarness,
) -> None:
    editor = collector_api.seed.tokens["workspace_editor_a"]
    enqueued = await _enqueue(collector_api, editor, ["https://example.com/fail"])
    task_id = enqueued.json()[0]["id"]
    fetcher = FakeFetcher()
    fetcher.fail = True
    worker = _worker(collector_api, fetcher)

    assert await worker.run_once(tenant_ids=[collector_api.seed.tenant_a]) == 1
    first = await collector_api.client.get(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/collection-tasks/{task_id}",
        headers=_auth(editor),
    )
    assert first.json()["status"] == "retry"
    await asyncio.sleep(0.15)
    assert await worker.run_once(tenant_ids=[collector_api.seed.tenant_a]) == 1
    failed = await collector_api.client.get(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/collection-tasks/{task_id}",
        headers=_auth(editor),
    )
    assert failed.json()["status"] == "failed"
    assert failed.json()["last_error_code"] == "fetch_connection_failed"

    retried = await collector_api.client.post(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/collection-tasks/{task_id}/retry",
        headers=_auth(editor),
    )
    assert retried.status_code == 200
    assert retried.json()["status"] == "pending"

    fetcher.fail = False
    assert await worker.run_once(tenant_ids=[collector_api.seed.tenant_a]) == 1
    succeeded = await collector_api.client.get(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/collection-tasks/{task_id}",
        headers=_auth(editor),
    )
    assert succeeded.json()["status"] == "succeeded"


async def test_collection_target_policy_and_access_control(
    collector_api: CollectorHarness,
) -> None:
    editor = collector_api.seed.tokens["workspace_editor_a"]
    viewer = collector_api.seed.tokens["workspace_viewer_a"]
    project_editor = collector_api.seed.tokens["project_editor_a"]
    tenant_b = collector_api.seed.tokens["tenant_admin_b"]

    private = await _enqueue(collector_api, editor, ["http://127.0.0.1/"])
    assert private.status_code == 403
    invalid = await _enqueue(collector_api, editor, ["ftp://example.com/"])
    assert invalid.status_code == 400

    denied = await _enqueue(collector_api, viewer, ["https://example.com/"])
    assert denied.status_code == 403

    project_task = await _enqueue(
        collector_api,
        editor,
        ["https://example.com/project"],
        project_id=collector_api.seed.project_a,
    )
    assert project_task.status_code == 202
    assert project_task.json()[0]["project_id"] == str(collector_api.seed.project_a)

    project_denied = await _enqueue(
        collector_api,
        project_editor,
        ["https://example.com/project-2"],
        project_id=collector_api.seed.project_a,
    )
    assert project_denied.status_code == 403

    source_denied = await collector_api.client.get(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/sources",
        headers=_auth(project_editor),
    )
    assert source_denied.status_code == 403

    task_denied = await collector_api.client.get(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/collection-tasks",
        headers=_auth(project_editor),
    )
    assert task_denied.status_code == 403

    cross_tenant = await collector_api.client.get(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/collection-tasks/"
        f"{project_task.json()[0]['id']}",
        headers=_auth(tenant_b),
    )
    assert cross_tenant.status_code == 404


async def test_source_delete_propagates_to_storage_and_audit(
    collector_api: CollectorHarness,
) -> None:
    editor = collector_api.seed.tokens["workspace_editor_a"]
    admin = collector_api.seed.tokens["workspace_admin_a"]
    enqueued = await _enqueue(collector_api, editor, ["https://example.com/delete"])
    task_id = enqueued.json()[0]["id"]
    fetcher = FakeFetcher()
    worker = _worker(collector_api, fetcher)
    assert await worker.run_once(tenant_ids=[collector_api.seed.tenant_a]) == 1

    task = await collector_api.client.get(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/collection-tasks/{task_id}",
        headers=_auth(editor),
    )
    source_id = task.json()["source_id"]
    with tenant_context(collector_api.seed.tenant_a):
        async with collector_api.seed.session_factory() as session:
            snapshot = await session.scalar(
                select(SourceSnapshot).where(
                    SourceSnapshot.tenant_id == collector_api.seed.tenant_a,
                    SourceSnapshot.source_id == UUID(source_id),
                )
            )
    assert snapshot is not None
    object_key = snapshot.object_key

    deleted = await collector_api.client.delete(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/sources/{source_id}",
        headers=_auth(admin),
    )
    assert deleted.status_code == 204
    missing = await collector_api.client.get(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/sources/{source_id}",
        headers=_auth(editor),
    )
    assert missing.status_code == 404
    listing = await collector_api.client.get(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/sources",
        headers=_auth(editor),
    )
    assert listing.json() == []
    with pytest.raises(ObjectStorageError):
        await collector_api.services.storage.get(object_key)

    audit = await collector_api.client.get(
        "/api/v1/audit-events",
        headers=_auth(collector_api.seed.tokens["tenant_admin_a"]),
        params={"limit": 100},
    )
    actions = {event["action"] for event in audit.json()}
    assert {
        "collection.task_queued",
        "collection.task_succeeded",
        "source.created",
        "source.deleted",
    } <= actions


async def test_source_delete_failure_is_retryable(
    collector_api: CollectorHarness,
) -> None:
    editor = collector_api.seed.tokens["workspace_editor_a"]
    admin = collector_api.seed.tokens["workspace_admin_a"]
    enqueued = await _enqueue(collector_api, editor, ["https://example.com/delete-fail"])
    task_id = enqueued.json()[0]["id"]
    worker = _worker(collector_api, FakeFetcher())
    assert await worker.run_once(tenant_ids=[collector_api.seed.tenant_a]) == 1
    task = await collector_api.client.get(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/collection-tasks/{task_id}",
        headers=_auth(editor),
    )
    source_id = task.json()["source_id"]

    original_storage = collector_api.services.storage
    collector_api.services.storage = FailingStorage()  # type: ignore[assignment]
    try:
        failed = await collector_api.client.delete(
            f"/api/v1/workspaces/{collector_api.seed.workspace_a}/sources/{source_id}",
            headers=_auth(admin),
        )
    finally:
        collector_api.services.storage = original_storage
    assert failed.status_code == 503

    with tenant_context(collector_api.seed.tenant_a):
        async with collector_api.seed.session_factory() as session:
            row = await session.scalar(
                select(WebSource).where(
                    WebSource.tenant_id == collector_api.seed.tenant_a,
                    WebSource.id == UUID(source_id),
                )
            )
    assert row is not None
    assert row.status == "delete_failed"

    retried = await collector_api.client.delete(
        f"/api/v1/workspaces/{collector_api.seed.workspace_a}/sources/{source_id}",
        headers=_auth(admin),
    )
    assert retried.status_code == 204


async def test_domain_rate_limiter_reserves_shared_slots(
    seed: Seed,
) -> None:
    limiter = DomainRateLimiter()
    with tenant_context(seed.tenant_a):
        async with seed.session_factory() as session, session.begin():
            first = await limiter.reserve(
                session,
                tenant_id=seed.tenant_a,
                domain="example.com",
                interval_seconds=5,
            )
            second = await limiter.reserve(
                session,
                tenant_id=seed.tenant_a,
                domain="example.com",
                interval_seconds=5,
            )
    assert first <= datetime.now(UTC)
    assert second >= first + timedelta(seconds=4)
