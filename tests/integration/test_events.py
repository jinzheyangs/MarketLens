from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.enums import ResearchRunKind, ResearchRunStatus
from app.auth.enums import AuditAction, ResourceType
from app.auth.principal import resolve_principal_for_user
from app.core.context import tenant_context
from app.db.models import (
    DomainEvent,
    EventConsumerFailure,
    ProcessedEvent,
    ResearchRun,
)
from app.events.consumer import EventConsumer, EventHandlerRegistry
from app.events.enums import OutboxStatus
from app.events.outbox import OutboxDispatcher
from app.events.publisher import EventPublisherError
from app.events.schemas import DomainEventEnvelope
from app.events.service import retry_failed_event
from app.services.audit import record_event
from app.workflow.starter import TemporalStarter
from tests.conftest import make_settings
from tests.integration.test_api_stage2 import Seed, _seed_database

pytestmark = pytest.mark.integration


@dataclass
class RecordingPublisher:
    events: list[DomainEventEnvelope] = field(default_factory=list)

    async def start(self) -> None:
        return None

    async def publish(self, envelope: DomainEventEnvelope) -> None:
        self.events.append(envelope)

    async def aclose(self) -> None:
        return None


@dataclass
class FailingPublisher:
    attempts: int = 0

    async def start(self) -> None:
        return None

    async def publish(self, envelope: DomainEventEnvelope) -> None:
        del envelope
        self.attempts += 1
        raise EventPublisherError("forced publish failure")

    async def aclose(self) -> None:
        return None


class CountingHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def handle(self, session: AsyncSession, envelope: DomainEventEnvelope) -> None:
        del session, envelope
        self.calls += 1


class FailingHandler:
    async def handle(self, session: AsyncSession, envelope: DomainEventEnvelope) -> None:
        del session, envelope
        raise RuntimeError("forced handler failure")


class FakeTemporalClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def start_workflow(self, workflow: Any, payload: dict[str, Any], **kwargs: Any) -> None:
        del workflow
        self.calls.append({"payload": payload, **kwargs})


@pytest.fixture
async def seed(
    session_factory: async_sessionmaker[AsyncSession],
    jwt_private_key: rsa.RSAPrivateKey,
) -> Seed:
    return await _seed_database(session_factory, jwt_private_key)


def _settings(test_database_url: str, **overrides: Any):
    values = {
        "database_url": test_database_url,
        "log_level": "WARNING",
        "event_outbox_retry_base_seconds": 0.1,
        "event_outbox_retry_max_seconds": 1.0,
        "event_outbox_poll_interval_seconds": 0.1,
    }
    values.update(overrides)
    return make_settings(**values)


async def _create_audit_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    resource_id: UUID,
    action: AuditAction = AuditAction.RESEARCH_RUN_CREATED,
) -> None:
    with tenant_context(tenant_id):
        async with session_factory() as session, session.begin():
            record_event(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action=action,
                resource_type=ResourceType.RESEARCH_RUN,
                resource_id=resource_id,
                details={"test": True},
            )


async def _create_run(
    session_factory: async_sessionmaker[AsyncSession],
    seed: Seed,
    *,
    run_kind: str = ResearchRunKind.PRIMARY.value,
    root_run_id: UUID | None = None,
    followup_round: int = 0,
) -> ResearchRun:
    run_id = uuid4()
    with tenant_context(seed.tenant_a):
        async with session_factory() as session, session.begin():
            run = ResearchRun(
                id=run_id,
                tenant_id=seed.tenant_a,
                workspace_id=seed.workspace_a,
                project_id=None,
                parent_run_id=root_run_id,
                root_run_id=root_run_id,
                run_kind=run_kind,
                followup_round=followup_round,
                requested_by=seed.user_ids["workspace_editor_a"],
                objective="Event test research",
                model_provider="deterministic",
                model_name="test",
                graph_version="stage5-v1",
                status=ResearchRunStatus.PENDING.value,
                max_rounds=1,
                max_attempts=1,
            )
            session.add(run)
            await session.flush()
            await session.refresh(run)
    return run


async def test_outbox_publishes_once_and_tracks_state(
    seed: Seed,
    session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
) -> None:
    event_id = uuid4()
    await _create_audit_event(
        session_factory,
        tenant_id=seed.tenant_a,
        actor_user_id=seed.user_ids["workspace_editor_a"],
        resource_id=event_id,
    )
    publisher = RecordingPublisher()
    dispatcher = OutboxDispatcher(
        settings=_settings(test_database_url),
        publisher=publisher,
        session_factory=session_factory,
    )

    assert await dispatcher.dispatch_once(tenant_ids=[seed.tenant_a]) == 1
    assert await dispatcher.dispatch_once(tenant_ids=[seed.tenant_a]) == 0
    assert len(publisher.events) == 1
    envelope = publisher.events[0]
    assert envelope.event_type == AuditAction.RESEARCH_RUN_CREATED.value
    assert envelope.partition_key.startswith(str(seed.tenant_a))

    with tenant_context(seed.tenant_a):
        async with session_factory() as session:
            event = await session.scalar(
                select(DomainEvent).where(DomainEvent.aggregate_id == event_id)
            )
    assert event is not None
    assert event.status == OutboxStatus.PUBLISHED.value
    assert event.published_at is not None


async def test_outbox_dead_letter_and_admin_replay(
    seed: Seed,
    session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
) -> None:
    event_id = uuid4()
    await _create_audit_event(
        session_factory,
        tenant_id=seed.tenant_a,
        actor_user_id=seed.user_ids["workspace_editor_a"],
        resource_id=event_id,
    )
    failing = FailingPublisher()
    dispatcher = OutboxDispatcher(
        settings=_settings(test_database_url, event_outbox_max_attempts=1),
        publisher=failing,
        session_factory=session_factory,
    )
    assert await dispatcher.dispatch_once(tenant_ids=[seed.tenant_a]) == 1
    assert failing.attempts == 1

    with tenant_context(seed.tenant_a):
        async with session_factory() as session, session.begin():
            event = await session.scalar(
                select(DomainEvent).where(DomainEvent.aggregate_id == event_id)
            )
            assert event is not None
            assert event.status == OutboxStatus.FAILED.value
            principal = await resolve_principal_for_user(
                session,
                tenant_id=seed.tenant_a,
                user_id=seed.user_ids["tenant_admin_a"],
            )
            event = await retry_failed_event(
                session,
                event_id=event.id,
                principal=principal,
            )
            assert event.status == OutboxStatus.PENDING.value

    publisher = RecordingPublisher()
    replay_dispatcher = OutboxDispatcher(
        settings=_settings(test_database_url, event_outbox_max_attempts=1),
        publisher=publisher,
        session_factory=session_factory,
    )
    assert await replay_dispatcher.dispatch_once(tenant_ids=[seed.tenant_a]) >= 1
    assert any(
        envelope.event_type == AuditAction.RESEARCH_RUN_CREATED.value
        for envelope in publisher.events
    )
    assert any(
        envelope.event_type == AuditAction.EVENT_REPLAYED.value for envelope in publisher.events
    )


async def test_consumer_is_idempotent_and_records_dead_letter(
    seed: Seed,
    session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
) -> None:
    settings = _settings(test_database_url, event_consumer_max_attempts=1)
    envelope = DomainEventEnvelope(
        event_id=uuid4(),
        event_type="research_run.completed",
        event_version=1,
        tenant_id=seed.tenant_a,
        aggregate_type="research_run",
        aggregate_id=uuid4(),
        actor_user_id=seed.user_ids["tenant_admin_a"],
        occurred_at=datetime.now(UTC),
        partition_key=str(seed.tenant_a),
        payload={"status": "completed"},
    )
    handler = CountingHandler()
    registry = EventHandlerRegistry()
    registry.register("research_run.completed", handler)
    consumer = EventConsumer(
        settings=settings,
        session_factory=session_factory,
        handlers=registry,
        consumer_name="test-consumer",
    )
    assert await consumer.process_envelope(envelope) is True
    assert await consumer.process_envelope(envelope) is True
    assert handler.calls == 1
    with tenant_context(seed.tenant_a):
        async with session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ProcessedEvent)
                .where(ProcessedEvent.event_id == envelope.event_id)
            )
    assert count == 1

    failing_envelope = DomainEventEnvelope(
        **{
            **envelope.model_dump(),
            "event_id": uuid4(),
            "event_type": "research_run.failed",
        }
    )
    failing_registry = EventHandlerRegistry()
    failing_registry.register("research_run.failed", FailingHandler())
    failing_consumer = EventConsumer(
        settings=settings,
        session_factory=session_factory,
        handlers=failing_registry,
        consumer_name="failing-consumer",
    )
    assert await failing_consumer.process_envelope(failing_envelope) is False
    with tenant_context(seed.tenant_a):
        async with session_factory() as session:
            failure = await session.scalar(
                select(EventConsumerFailure).where(
                    EventConsumerFailure.event_id == failing_envelope.event_id
                )
            )
    assert failure is not None
    assert failure.status == "dead_letter"
    assert failure.attempts == 1


async def test_temporal_starter_is_idempotent_and_skips_followups(
    seed: Seed,
    session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
) -> None:
    primary = await _create_run(session_factory, seed)
    followup = await _create_run(
        session_factory,
        seed,
        run_kind=ResearchRunKind.FOLLOWUP.value,
        root_run_id=primary.id,
        followup_round=1,
    )
    await _create_audit_event(
        session_factory,
        tenant_id=seed.tenant_a,
        actor_user_id=seed.user_ids["workspace_editor_a"],
        resource_id=primary.id,
    )
    await _create_audit_event(
        session_factory,
        tenant_id=seed.tenant_a,
        actor_user_id=seed.user_ids["workspace_editor_a"],
        resource_id=followup.id,
    )

    client = FakeTemporalClient()
    starter = TemporalStarter(
        settings=_settings(
            test_database_url,
            temporal_enabled=True,
            temporal_host="temporal:7233",
            temporal_poll_interval_seconds=1,
        ),
        client=cast(Any, client),
        session_factory=session_factory,
    )
    assert await starter.run_once(tenant_ids=[seed.tenant_a]) == 1
    assert len(client.calls) == 1
    assert client.calls[0]["id"] == f"research-lifecycle-{primary.id}"
    assert await starter.run_once(tenant_ids=[seed.tenant_a]) == 0
    assert len(client.calls) == 1


async def test_outbox_recovers_expired_publishing_lease(
    seed: Seed,
    session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
) -> None:
    event_id = uuid4()
    await _create_audit_event(
        session_factory,
        tenant_id=seed.tenant_a,
        actor_user_id=seed.user_ids["workspace_editor_a"],
        resource_id=event_id,
    )
    with tenant_context(seed.tenant_a):
        async with session_factory() as session, session.begin():
            event = await session.scalar(
                select(DomainEvent).where(DomainEvent.aggregate_id == event_id)
            )
            assert event is not None
            event.status = OutboxStatus.PUBLISHING.value
            event.attempts = 1
            event.worker_id = "dead-worker"
            event.lease_expires_at = datetime(2020, 1, 1, tzinfo=UTC)

    publisher = RecordingPublisher()
    dispatcher = OutboxDispatcher(
        settings=_settings(test_database_url),
        publisher=publisher,
        session_factory=session_factory,
    )
    assert await dispatcher.dispatch_once(tenant_ids=[seed.tenant_a]) == 1
    assert len(publisher.events) == 1
    with tenant_context(seed.tenant_a):
        async with session_factory() as session:
            event = await session.scalar(
                select(DomainEvent).where(DomainEvent.aggregate_id == event_id)
            )
    assert event is not None
    assert event.status == OutboxStatus.PUBLISHED.value
