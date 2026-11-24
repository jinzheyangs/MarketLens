from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.oidc import JWTVerifier
from app.core.context import tenant_context
from app.db.models import (
    AuditEvent,
    CollectionTask,
    DomainEvent,
    EventConsumerFailure,
    ProcessedEvent,
    ResearchRun,
)
from app.factory import create_app
from app.ops.retention import run_retention
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
async def operations_client(
    seed: Seed,
    test_database_url: str,
    jwt_public_key: rsa.RSAPublicKey,
) -> Any:
    settings = make_settings(
        database_url=test_database_url,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        log_level="WARNING",
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


async def _seed_cost_runs(seed: Seed) -> None:
    now = datetime.now(UTC)
    with tenant_context(seed.tenant_a):
        async with seed.session_factory() as session, session.begin():
            for index in range(2):
                session.add(
                    ResearchRun(
                        id=uuid4(),
                        tenant_id=seed.tenant_a,
                        workspace_id=seed.workspace_a,
                        project_id=seed.project_a,
                        requested_by=seed.user_ids["workspace_editor_a"],
                        objective=f"Cost attribution {index}",
                        model_provider="deterministic",
                        model_name="integration-test",
                        graph_version="v1",
                        status="completed",
                        max_rounds=1,
                        max_attempts=1,
                        usage={
                            "input_tokens": 100 + index,
                            "output_tokens": 50 + index,
                            "cost_micros": 1_000 + index,
                            "tool_calls": 2,
                        },
                        created_at=now,
                    )
                )


async def test_cost_attribution_endpoint(operations_client: Any) -> None:
    client, seed, _, _ = operations_client
    await _seed_cost_runs(seed)

    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    until = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    response = await client.get(
        "/api/v1/usage/costs",
        params={"since": since, "until": until},
        headers=_auth(seed.tokens["tenant_admin_a"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runs"] == 2
    assert body["input_tokens"] == 201
    assert body["output_tokens"] == 101
    assert body["cost_micros"] == 2_001
    assert body["tool_calls"] == 4
    assert len(body["buckets"]) == 1
    bucket = body["buckets"][0]
    assert bucket["workspace_id"] == str(seed.workspace_a)
    assert bucket["model_provider"] == "deterministic"

    forbidden = await client.get(
        "/api/v1/usage/costs",
        headers=_auth(seed.tokens["workspace_admin_a"]),
    )
    assert forbidden.status_code == 403

    bad_range = await client.get(
        "/api/v1/usage/costs",
        params={"since": "2026-01-01T00:00:00", "until": "2026-01-02T00:00:00"},
        headers=_auth(seed.tokens["tenant_admin_a"]),
    )
    assert bad_range.status_code == 400

    too_wide = await client.get(
        "/api/v1/usage/costs",
        params={
            "since": "2020-01-01T00:00:00+00:00",
            "until": "2026-01-01T00:00:00+00:00",
        },
        headers=_auth(seed.tokens["tenant_admin_a"]),
    )
    assert too_wide.status_code == 400


async def test_retention_dry_run_and_execute_are_bounded(
    operations_client: Any,
) -> None:
    _, seed, application, settings = operations_client
    session_factory = application.state.session_factory
    now = datetime.now(UTC)
    old = now - timedelta(days=400)
    fresh = now

    processed_old = uuid4()
    processed_new = uuid4()
    failure_old = uuid4()
    failure_new = uuid4()
    event_old = uuid4()
    event_new = uuid4()
    with tenant_context(seed.tenant_a):
        async with session_factory() as session, session.begin():
            session.add(
                ProcessedEvent(
                    id=processed_old,
                    tenant_id=seed.tenant_a,
                    consumer="test-consumer",
                    event_id=uuid4(),
                    event_type="test.old",
                    processed_at=old,
                )
            )
            session.add(
                ProcessedEvent(
                    id=processed_new,
                    tenant_id=seed.tenant_a,
                    consumer="test-consumer",
                    event_id=uuid4(),
                    event_type="test.new",
                    processed_at=fresh,
                )
            )
            session.add(
                EventConsumerFailure(
                    id=failure_old,
                    tenant_id=seed.tenant_a,
                    consumer="test-consumer",
                    event_id=uuid4(),
                    event_type="test.failed",
                    status="resolved",
                    attempts=1,
                    max_attempts=3,
                    payload={},
                    resolved_at=old,
                )
            )
            session.add(
                EventConsumerFailure(
                    id=failure_new,
                    tenant_id=seed.tenant_a,
                    consumer="test-consumer",
                    event_id=uuid4(),
                    event_type="test.open",
                    status="open",
                    attempts=1,
                    max_attempts=3,
                    payload={},
                    resolved_at=old,
                )
            )
            for event_id, event_type, status, published_at in (
                (event_old, "test.old", "published", old),
                (event_new, "test.new", "published", fresh),
            ):
                session.add(
                    DomainEvent(
                        id=uuid4(),
                        event_id=event_id,
                        tenant_id=seed.tenant_a,
                        event_type=event_type,
                        event_version=1,
                        aggregate_type="tenant",
                        aggregate_id=seed.tenant_a,
                        payload={},
                        headers={},
                        partition_key="test",
                        idempotency_key=f"test:{event_id}",
                        status=status,
                        attempts=1,
                        max_attempts=3,
                        published_at=published_at,
                    )
                )

    dry = await run_retention(
        settings=settings,
        session_factory=session_factory,
        tenant_ids=[seed.tenant_a],
        execute=False,
        now=now,
    )
    dry_counts = {(result.tenant_id, result.table): result.rows for result in dry}
    assert dry_counts[(seed.tenant_a, "processed_events")] == 1
    assert dry_counts[(seed.tenant_a, "resolved_consumer_failures")] == 1
    assert dry_counts[(seed.tenant_a, "published_domain_events")] == 1

    with tenant_context(seed.tenant_a):
        async with session_factory() as session:
            assert await session.get(ProcessedEvent, processed_old) is not None

    executed = await run_retention(
        settings=settings,
        session_factory=session_factory,
        tenant_ids=[seed.tenant_a],
        execute=True,
        now=now,
    )
    assert sum(result.rows for result in executed) >= 3

    with tenant_context(seed.tenant_a):
        async with session_factory() as session:
            assert await session.get(ProcessedEvent, processed_old) is None
            assert await session.get(ProcessedEvent, processed_new) is not None
            assert await session.get(EventConsumerFailure, failure_old) is None
            assert await session.get(EventConsumerFailure, failure_new) is not None
            remaining_event_ids = set(
                (
                    await session.scalars(
                        select(DomainEvent.event_id).where(
                            DomainEvent.event_id.in_([event_old, event_new])
                        )
                    )
                ).all()
            )
            assert event_old not in remaining_event_ids
            assert event_new in remaining_event_ids
            audit_actions = list(
                (
                    await session.scalars(
                        select(AuditEvent.action).where(AuditEvent.action == "retention.purged")
                    )
                ).all()
            )
            assert audit_actions


async def test_retention_execute_requires_explicit_opt_in(
    operations_client: Any,
) -> None:
    _, seed, _, settings = operations_client
    assert settings.retention_destructive_enabled is False
    # The CLI guard must refuse destructive mode before opening the database.
    from app.ops import retention

    outcome = retention.main(["--execute", "--tenant", str(seed.tenant_a)])
    assert outcome == 2


async def test_operations_queue_snapshot(operations_client: Any) -> None:
    client, seed, application, _ = operations_client
    url = "https://example.test/queued-source"
    with tenant_context(seed.tenant_a):
        async with application.state.session_factory() as session, session.begin():
            session.add(
                CollectionTask(
                    id=uuid4(),
                    tenant_id=seed.tenant_a,
                    workspace_id=seed.workspace_a,
                    requested_by=seed.user_ids["workspace_editor_a"],
                    url=url,
                    url_hash="a" * 64,
                    domain="example.test",
                    status="pending",
                    attempts=0,
                    max_attempts=1,
                )
            )

    response = await client.get(
        "/api/v1/operations/queues",
        headers=_auth(seed.tokens["tenant_admin_a"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pending_collection_tasks"] >= 1
    for key in (
        "outbox_pending",
        "outbox_failed",
        "consumer_failures_open",
        "pending_monitor_runs",
        "pending_notification_deliveries",
        "pending_approvals",
        "generated_at",
    ):
        assert key in body

    forbidden = await client.get(
        "/api/v1/operations/queues",
        headers=_auth(seed.tokens["workspace_admin_a"]),
    )
    assert forbidden.status_code == 403
