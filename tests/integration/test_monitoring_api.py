from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.oidc import JWTVerifier
from app.core.context import tenant_context
from app.db.models import (
    CollectionTask,
    MonitorAlertRule,
    MonitorChange,
    MonitorRun,
    MonitorSubscription,
    NotificationChannel,
    NotificationDelivery,
    Report,
    ReportVersion,
    ResearchRun,
    SourceSnapshot,
    WebSource,
)
from app.factory import create_app
from app.monitoring.crypto import ChannelSecretCipher
from app.monitoring.service import MonitoringServices
from app.monitoring.worker import MonitoringWorker
from app.ops.notification_keys import rotate_notification_keys
from tests.conftest import make_settings, open_client
from tests.integration.test_api_stage2 import (
    AUDIENCE,
    ISSUER,
    Seed,
    _auth,
    _seed_database,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def seed(
    session_factory: async_sessionmaker[AsyncSession],
    jwt_private_key: rsa.RSAPrivateKey,
) -> Seed:
    return await _seed_database(session_factory, jwt_private_key)


@pytest.fixture
async def monitoring_client(
    seed: Seed,
    test_database_url: str,
    jwt_public_key: rsa.RSAPublicKey,
) -> Any:
    settings = make_settings(
        database_url=test_database_url,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        log_level="WARNING",
        monitoring_enabled=True,
        notification_encryption_key=Fernet.generate_key().decode(),
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
        yield client, seed, application, settings


async def _channel(
    client: AsyncClient,
    seed: Seed,
    *,
    name: str = "ops-webhook",
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/workspaces/{seed.workspace_a}/notification-channels",
        headers=_auth(seed.tokens["workspace_admin_a"]),
        json={
            "name": name,
            "kind": "webhook",
            "url": "https://hooks.example.test/marketlens",
            "signing_secret": "test-signing-secret",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _monitor(
    client: AsyncClient,
    seed: Seed,
    *,
    name: str = "competitor-watch",
    token: str | None = None,
    alerts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/workspaces/{seed.workspace_a}/monitors",
        headers=_auth(token or seed.tokens["workspace_editor_a"]),
        json={
            "name": name,
            "kind": "keyword",
            "query": "market lens pricing",
            "alerts": alerts or [],
        },
    )
    return {"status": response.status_code, "body": response.json()}


async def test_monitoring_channel_and_subscription_lifecycle(
    monitoring_client: Any,
) -> None:
    client, seed, _, settings = monitoring_client
    viewer = _auth(seed.tokens["workspace_viewer_a"])
    editor = _auth(seed.tokens["workspace_editor_a"])

    channel_response = await client.post(
        f"/api/v1/workspaces/{seed.workspace_a}/notification-channels",
        headers=_auth(seed.tokens["workspace_admin_a"]),
        json={
            "name": "ops-webhook",
            "kind": "webhook",
            "url": "https://hooks.example.test/marketlens",
            "signing_secret": "test-signing-secret",
        },
    )
    assert channel_response.status_code == 201, channel_response.text
    channel = channel_response.json()
    assert "url" not in channel
    assert "encrypted_config" not in channel
    assert "signing_secret" not in channel

    updated_channel = await client.patch(
        f"/api/v1/workspaces/{seed.workspace_a}/notification-channels/{channel['id']}",
        headers=_auth(seed.tokens["workspace_admin_a"]),
        json={
            "name": "ops-webhook-rotated",
            "url": "https://hooks.example.test/marketlens-v2",
            "signing_secret": "rotated-signing-secret",
        },
    )
    assert updated_channel.status_code == 200, updated_channel.text
    assert updated_channel.json()["name"] == "ops-webhook-rotated"
    assert "encrypted_config" not in updated_channel.json()
    with tenant_context(seed.tenant_a):
        async with seed.session_factory() as session:
            stored = await session.get(NotificationChannel, UUID(channel["id"]))
            assert stored is not None
            config = ChannelSecretCipher(settings).decrypt(stored.encrypted_config or "")
            assert config["url"] == "https://hooks.example.test/marketlens-v2"
            assert config["signing_secret"] == "rotated-signing-secret"

    log_channel = await client.post(
        f"/api/v1/workspaces/{seed.workspace_a}/notification-channels",
        headers=_auth(seed.tokens["workspace_admin_a"]),
        json={"name": "audit-log", "kind": "log"},
    )
    assert log_channel.status_code == 201, log_channel.text
    invalid_log_update = await client.patch(
        f"/api/v1/workspaces/{seed.workspace_a}/notification-channels/{log_channel.json()['id']}",
        headers=_auth(seed.tokens["workspace_admin_a"]),
        json={"url": "https://hooks.example.test/not-allowed"},
    )
    assert invalid_log_update.status_code == 400

    forbidden_channel = await client.post(
        f"/api/v1/workspaces/{seed.workspace_a}/notification-channels",
        headers=editor,
        json={"name": "editor-channel", "kind": "log"},
    )
    assert forbidden_channel.status_code == 403

    create = await _monitor(
        client,
        seed,
        alerts=[{"channel_id": channel["id"], "min_severity": "high"}],
    )
    assert create["status"] == 201, create["body"]
    subscription = create["body"]
    assert subscription["status"] == "active"
    assert subscription["cadence_minutes"] == 60

    viewer_create = await _monitor(
        client,
        seed,
        name="viewer-attempt",
        token=seed.tokens["workspace_viewer_a"],
    )
    assert viewer_create["status"] == 403

    listing = await client.get(
        f"/api/v1/workspaces/{seed.workspace_a}/monitors",
        headers=viewer,
    )
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()] == [subscription["id"]]

    outsider = await client.get(
        f"/api/v1/workspaces/{seed.workspace_a}/monitors",
        headers=_auth(seed.tokens["outsider_a"]),
    )
    assert outsider.status_code == 403

    cross_tenant = await client.get(
        f"/api/v1/workspaces/{seed.workspace_a}/monitors",
        headers=_auth(seed.tokens["tenant_admin_b"]),
    )
    assert cross_tenant.status_code == 404

    trigger = await client.post(
        f"/api/v1/workspaces/{seed.workspace_a}/monitors/{subscription['id']}/trigger",
        headers=editor,
    )
    assert trigger.status_code == 200, trigger.text
    assert trigger.json()["next_run_at"] is not None

    patch = await client.patch(
        f"/api/v1/workspaces/{seed.workspace_a}/monitors/{subscription['id']}",
        headers=editor,
        json={"query": "updated query", "cadence_minutes": 30},
    )
    assert patch.status_code == 200
    assert patch.json()["query"] == "updated query"
    assert patch.json()["cadence_minutes"] == 30

    paused = await client.post(
        f"/api/v1/workspaces/{seed.workspace_a}/monitors/{subscription['id']}/pause",
        headers=editor,
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    resumed = await client.post(
        f"/api/v1/workspaces/{seed.workspace_a}/monitors/{subscription['id']}/resume",
        headers=editor,
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "active"

    delete = await client.delete(
        f"/api/v1/workspaces/{seed.workspace_a}/monitors/{subscription['id']}",
        headers=editor,
    )
    assert delete.status_code == 200
    assert delete.json()["status"] == "deleted"

    missing = await client.get(
        f"/api/v1/workspaces/{seed.workspace_a}/monitors/{subscription['id']}",
        headers=viewer,
    )
    assert missing.status_code == 404


async def test_monitor_subscription_quota_is_enforced(
    monitoring_client: Any,
) -> None:
    client, seed, _, _ = monitoring_client
    admin = _auth(seed.tokens["tenant_admin_a"])
    quota_patch = await client.patch(
        "/api/v1/quotas",
        headers=admin,
        json={"max_monitor_subscriptions": 1},
    )
    assert quota_patch.status_code == 200, quota_patch.text

    first = await _monitor(client, seed, name="first-monitor")
    assert first["status"] == 201, first["body"]
    second = await _monitor(client, seed, name="second-monitor")
    assert second["status"] == 429
    assert second["body"]["error"]["code"] == "quota_exceeded"

    snapshot = await client.get("/api/v1/quotas", headers=admin)
    assert snapshot.status_code == 200
    assert snapshot.json()["max_monitor_subscriptions"] == 1
    assert snapshot.json()["monitor_subscriptions"] == 1


async def test_project_comments_and_approval_nodes(
    monitoring_client: Any,
) -> None:
    client, seed, _, _ = monitoring_client
    viewer = _auth(seed.tokens["workspace_viewer_a"])
    editor = _auth(seed.tokens["workspace_editor_a"])
    admin = _auth(seed.tokens["workspace_admin_a"])

    create = await client.post(
        f"/api/v1/projects/{seed.project_a}/comments",
        headers=viewer,
        json={"body": "Please verify the pricing evidence."},
    )
    assert create.status_code == 201, create.text
    comment = create.json()

    reply = await client.post(
        f"/api/v1/projects/{seed.project_a}/comments",
        headers=editor,
        json={"body": "Verifying now.", "parent_id": comment["id"]},
    )
    assert reply.status_code == 201, reply.text

    editor_edit = await client.patch(
        f"/api/v1/projects/{seed.project_a}/comments/{comment['id']}",
        headers=editor,
        json={"body": "edited by someone else"},
    )
    assert editor_edit.status_code == 403

    own_edit = await client.patch(
        f"/api/v1/projects/{seed.project_a}/comments/{comment['id']}",
        headers=viewer,
        json={"body": "Please verify the updated pricing evidence."},
    )
    assert own_edit.status_code == 200
    assert own_edit.json()["body"].startswith("Please verify the updated")

    listing = await client.get(
        f"/api/v1/projects/{seed.project_a}/comments",
        headers=viewer,
    )
    assert listing.status_code == 200
    assert len(listing.json()) == 2

    deleted = await client.delete(
        f"/api/v1/projects/{seed.project_a}/comments/{comment['id']}",
        headers=admin,
    )
    assert deleted.status_code == 204
    listing = await client.get(
        f"/api/v1/projects/{seed.project_a}/comments",
        headers=viewer,
    )
    assert len(listing.json()) == 1

    version_id = await _seed_report_version(seed)
    approval = await client.post(
        f"/api/v1/projects/{seed.project_a}/approval-requests",
        headers=editor,
        json={
            "resource_type": "report_version",
            "resource_id": str(version_id),
            "assignee_user_id": str(seed.user_ids["workspace_admin_a"]),
            "request_note": "Please approve before delivery.",
        },
    )
    assert approval.status_code == 201, approval.text
    approval_id = approval.json()["id"]

    duplicate = await client.post(
        f"/api/v1/projects/{seed.project_a}/approval-requests",
        headers=editor,
        json={
            "resource_type": "report_version",
            "resource_id": str(version_id),
            "assignee_user_id": str(seed.user_ids["workspace_admin_a"]),
        },
    )
    assert duplicate.status_code == 409

    forbidden = await client.post(
        f"/api/v1/projects/{seed.project_a}/approval-requests/{approval_id}/approve",
        headers=viewer,
        json={"note": "not my approval"},
    )
    assert forbidden.status_code == 403

    approved = await client.post(
        f"/api/v1/projects/{seed.project_a}/approval-requests/{approval_id}/approve",
        headers=admin,
        json={"note": "looks good"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    again = await client.post(
        f"/api/v1/projects/{seed.project_a}/approval-requests/{approval_id}/reject",
        headers=admin,
        json={},
    )
    assert again.status_code == 409

    second = await client.post(
        f"/api/v1/projects/{seed.project_a}/approval-requests",
        headers=editor,
        json={
            "resource_type": "report_version",
            "resource_id": str(version_id),
            "assignee_user_id": str(seed.user_ids["project_editor_a"]),
        },
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]
    filtered = await client.get(
        f"/api/v1/projects/{seed.project_a}/approval-requests",
        headers=viewer,
        params={
            "status": "pending",
            "assignee_user_id": str(seed.user_ids["project_editor_a"]),
        },
    )
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.json()] == [second_id]

    canceled = await client.post(
        f"/api/v1/projects/{seed.project_a}/approval-requests/{second_id}/cancel",
        headers=editor,
        json={"note": "superseded"},
    )
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "canceled"
    cancel_again = await client.post(
        f"/api/v1/projects/{seed.project_a}/approval-requests/{second_id}/cancel",
        headers=editor,
        json={},
    )
    assert cancel_again.status_code == 409

    approvals = await client.get(
        f"/api/v1/projects/{seed.project_a}/approval-requests",
        headers=viewer,
    )
    assert approvals.status_code == 200
    by_id = {item["id"]: item for item in approvals.json()}
    assert by_id[approval_id]["status"] == "approved"
    assert by_id[second_id]["status"] == "canceled"


async def _seed_report_version(seed: Seed) -> UUID:
    run_id = uuid4()
    report_id = uuid4()
    version_id = uuid4()
    now = datetime.now(UTC)
    with tenant_context(seed.tenant_a):
        async with seed.session_factory() as session, session.begin():
            session.add(
                ResearchRun(
                    id=run_id,
                    tenant_id=seed.tenant_a,
                    workspace_id=seed.workspace_a,
                    project_id=seed.project_a,
                    requested_by=seed.user_ids["workspace_editor_a"],
                    objective="Stage 8 approval integration",
                    model_provider="deterministic",
                    model_name="integration-test",
                    graph_version="v1",
                    status="completed",
                    max_rounds=1,
                    max_attempts=1,
                )
            )
            await session.flush()
            session.add(
                Report(
                    id=report_id,
                    tenant_id=seed.tenant_a,
                    root_run_id=run_id,
                    workspace_id=seed.workspace_a,
                    project_id=seed.project_a,
                    status="draft",
                    created_by=seed.user_ids["workspace_editor_a"],
                )
            )
            await session.flush()
            session.add(
                ReportVersion(
                    id=version_id,
                    tenant_id=seed.tenant_a,
                    report_id=report_id,
                    version_number=1,
                    status="draft",
                    title="Stage 8 report",
                    content_markdown="# Stage 8 report\n\nApproval test.",
                    structured={"title": "Stage 8 report"},
                    source_list=[],
                    citation_index=[],
                    checksum_sha256=hashlib.sha256(b"stage-8").hexdigest(),
                    created_by=seed.user_ids["workspace_editor_a"],
                    created_at=now,
                )
            )
    return version_id


async def test_monitoring_worker_detects_change_and_delivers(
    monitoring_client: Any,
) -> None:
    _client, seed, application, settings = monitoring_client
    services = application.state.monitoring_services
    assert isinstance(services, MonitoringServices)

    now = datetime.now(UTC)
    subscription_id = uuid4()
    channel_id = uuid4()
    source_id = uuid4()
    snapshot_id = uuid4()
    task_id = uuid4()
    run_id = uuid4()
    url = "https://example.test/competitor-pricing"
    content_hash = hashlib.sha256(b"pricing v1").hexdigest()
    with tenant_context(seed.tenant_a):
        async with seed.session_factory() as session, session.begin():
            session.add(
                NotificationChannel(
                    id=channel_id,
                    tenant_id=seed.tenant_a,
                    workspace_id=seed.workspace_a,
                    name="worker-log-channel",
                    kind="log",
                    encrypted_config=None,
                    enabled=True,
                    created_by=seed.user_ids["workspace_admin_a"],
                )
            )
            session.add(
                MonitorSubscription(
                    id=subscription_id,
                    tenant_id=seed.tenant_a,
                    workspace_id=seed.workspace_a,
                    project_id=None,
                    name="worker-monitor",
                    kind="keyword",
                    query="competitor pricing",
                    status="active",
                    cadence_minutes=60,
                    next_run_at=now + timedelta(hours=1),
                    created_by=seed.user_ids["workspace_editor_a"],
                )
            )
            await session.flush()
            session.add(
                MonitorAlertRule(
                    tenant_id=seed.tenant_a,
                    subscription_id=subscription_id,
                    channel_id=channel_id,
                    min_severity="low",
                )
            )
            session.add(
                WebSource(
                    id=source_id,
                    tenant_id=seed.tenant_a,
                    workspace_id=seed.workspace_a,
                    url=url,
                    url_hash=hashlib.sha256(url.encode()).hexdigest(),
                    domain="example.test",
                    title="Competitor pricing",
                    status="active",
                    content_sha256=content_hash,
                    latest_snapshot_id=snapshot_id,
                    first_seen_at=now,
                    last_fetched_at=now,
                    created_by=seed.user_ids["workspace_editor_a"],
                )
            )
            await session.flush()
            session.add(
                SourceSnapshot(
                    id=snapshot_id,
                    tenant_id=seed.tenant_a,
                    workspace_id=seed.workspace_a,
                    source_id=source_id,
                    status="active",
                    object_key="monitor/snapshot-1",
                    content_type="text/html",
                    size_bytes=9,
                    content_sha256=content_hash,
                    final_url=url,
                    http_status=200,
                    extracted_text="Pricing v1",
                    extracted_char_count=10,
                    fetched_at=now,
                )
            )
            session.add(
                CollectionTask(
                    id=task_id,
                    tenant_id=seed.tenant_a,
                    workspace_id=seed.workspace_a,
                    requested_by=seed.user_ids["workspace_editor_a"],
                    url=url,
                    url_hash=hashlib.sha256(url.encode()).hexdigest(),
                    domain="example.test",
                    status="succeeded",
                    attempts=1,
                    max_attempts=1,
                    next_attempt_at=now,
                    source_id=source_id,
                )
            )
            session.add(
                MonitorRun(
                    id=run_id,
                    tenant_id=seed.tenant_a,
                    subscription_id=subscription_id,
                    triggered_by="schedule",
                    status="pending",
                    task_ids=[str(task_id)],
                    attempts=0,
                    max_attempts=1,
                    next_attempt_at=now,
                )
            )

    worker = MonitoringWorker(
        settings=settings,
        services=services,
        session_factory=seed.session_factory,
    )
    scheduled, runs, deliveries = await worker.run_once(tenant_ids=[seed.tenant_a])
    assert scheduled == 0
    assert runs == 1
    assert deliveries == 1

    with tenant_context(seed.tenant_a):
        async with seed.session_factory() as session:
            run = await session.get(MonitorRun, run_id)
            assert run is not None
            assert run.status == "completed"
            assert run.change_count == 1
            changes = list(
                (
                    await session.scalars(
                        select(MonitorChange).where(MonitorChange.run_id == run_id)
                    )
                ).all()
            )
            assert len(changes) == 1
            assert changes[0].change_type == "new_source"
            delivery = await session.scalar(
                select(NotificationDelivery).where(
                    NotificationDelivery.subscription_id == subscription_id
                )
            )
            assert delivery is not None
            assert delivery.status == "sent"
            # The webhook URL and signing secret must never be stored in the
            # delivery payload that leaves the system.
            assert "signing_secret" not in delivery.payload
            assert delivery.payload["untrusted"] is True
            assert delivery.payload["source_url"] == url


async def test_notification_key_rotation_rewrites_channel_secrets(
    monitoring_client: Any,
) -> None:
    _, seed, application, _ = monitoring_client
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    old_settings = make_settings(notification_encryption_key=old_key)
    rotating_settings = make_settings(
        notification_encryption_key=new_key,
        notification_previous_encryption_key=old_key,
    )
    new_only_settings = make_settings(notification_encryption_key=new_key)
    channel_id = uuid4()
    secret = {"url": "https://hooks.example.test/rotate-me"}
    with tenant_context(seed.tenant_a):
        async with seed.session_factory() as session, session.begin():
            session.add(
                NotificationChannel(
                    id=channel_id,
                    tenant_id=seed.tenant_a,
                    workspace_id=seed.workspace_a,
                    name="rotation-channel",
                    kind="webhook",
                    encrypted_config=ChannelSecretCipher(old_settings).encrypt(secret),
                    enabled=True,
                    created_by=seed.user_ids["workspace_admin_a"],
                )
            )

    results = await rotate_notification_keys(
        settings=rotating_settings,
        session_factory=application.state.session_factory,
        tenant_ids=[seed.tenant_a],
        execute=True,
    )
    assert len(results) == 1
    assert results[0].rotated == 1
    assert results[0].failed == 0

    with tenant_context(seed.tenant_a):
        async with seed.session_factory() as session:
            channel = await session.get(NotificationChannel, channel_id)
            assert channel is not None
            assert channel.encrypted_config is not None
            rotated = ChannelSecretCipher(new_only_settings).decrypt(channel.encrypted_config)
            assert rotated == secret
