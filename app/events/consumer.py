"""Kafka 消费者：消费者侧幂等与数据库死信记录。"""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from aiokafka import AIOKafkaConsumer, ConsumerRecord
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.context import tenant_context
from app.db.models import EventConsumerFailure, ProcessedEvent
from app.events.enums import (
    EVENT_SCHEMA_VERSION,
    ConsumerFailureStatus,
)
from app.events.schemas import DomainEventEnvelope

logger = logging.getLogger(__name__)


class EventHandler(Protocol):
    async def handle(self, session: AsyncSession, envelope: DomainEventEnvelope) -> None: ...


class EventHandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}
        self._wildcard: list[EventHandler] = []

    def register(self, event_type: str, handler: EventHandler) -> None:
        if event_type == "*":
            self._wildcard.append(handler)
            return
        self._handlers.setdefault(event_type, []).append(handler)

    def for_event(self, event_type: str) -> list[EventHandler]:
        return [*self._handlers.get(event_type, []), *self._wildcard]


class EventConsumer:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        handlers: EventHandlerRegistry,
        consumer_name: str | None = None,
        topic: str | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._handlers = handlers
        self._consumer_name = consumer_name or settings.event_consumer_group
        self._topic = topic or settings.event_topic
        self._consumer: AIOKafkaConsumer | None = None

    async def start(self) -> None:
        if not self._settings.event_consumer_enabled:
            return
        ssl_context = None
        if self._settings.kafka_ssl_cafile:
            ssl_context = ssl.create_default_context(cafile=self._settings.kafka_ssl_cafile)
        self._consumer = AIOKafkaConsumer(
            self._topic,
            bootstrap_servers=list(self._settings.kafka_bootstrap_servers),
            group_id=self._settings.event_consumer_group,
            client_id=self._settings.kafka_client_id,
            auto_offset_reset="earliest",
            enable_auto_commit=False,
            security_protocol=self._settings.kafka_security_protocol,
            sasl_mechanism=self._settings.kafka_sasl_mechanism,
            sasl_plain_username=self._settings.kafka_sasl_username,
            sasl_plain_password=(
                self._settings.kafka_sasl_password.get_secret_value()
                if self._settings.kafka_sasl_password is not None
                else None
            ),
            ssl_context=ssl_context,
        )
        await self._consumer.start()

    async def run_forever(self) -> None:
        if self._consumer is None:
            await self.start()
        if self._consumer is None:
            return
        try:
            while True:
                batches = await self._consumer.getmany(
                    timeout_ms=self._settings.event_consumer_poll_timeout_ms,
                )
                processed = 0
                for messages in batches.values():
                    for message in messages:
                        await self._handle_message(message)
                        processed += 1
                if processed:
                    await self._consumer.commit()
                else:
                    await asyncio.sleep(0.1)
        finally:
            await self.aclose()

    async def consume_once(self, *, timeout_ms: int | None = None) -> int:
        if self._consumer is None:
            await self.start()
        if self._consumer is None:
            return 0
        batches = await self._consumer.getmany(
            timeout_ms=timeout_ms or self._settings.event_consumer_poll_timeout_ms,
        )
        processed = 0
        for messages in batches.values():
            for message in messages:
                await self._handle_message(message)
                processed += 1
        if processed:
            await self._consumer.commit()
        return processed

    async def process_envelope(
        self,
        envelope: DomainEventEnvelope,
    ) -> bool:
        """Process one event exactly once per consumer name.

        Handler side effects and the ProcessedEvent marker share a transaction.
        Handler failure rolls both back and records a dead-letter candidate.
        """
        if envelope.event_version > EVENT_SCHEMA_VERSION:
            await self._record_failure(
                envelope,
                "unsupported_event_version",
                terminal=True,
            )
            return False
        handlers = self._handlers.for_event(envelope.event_type)
        for attempt in range(1, self._settings.event_consumer_max_attempts + 1):
            with tenant_context(envelope.tenant_id):
                try:
                    async with self._session_factory() as session, session.begin():
                        existing = await session.scalar(
                            select(ProcessedEvent).where(
                                ProcessedEvent.tenant_id == envelope.tenant_id,
                                ProcessedEvent.consumer == self._consumer_name,
                                ProcessedEvent.event_id == envelope.event_id,
                            )
                        )
                        if existing is not None:
                            return True
                        if not handlers:
                            logger.debug(
                                "domain_event_unhandled",
                                extra={
                                    "context": {
                                        "event_type": envelope.event_type,
                                        "consumer": self._consumer_name,
                                    }
                                },
                            )
                        for handler in handlers:
                            await handler.handle(session, envelope)
                        session.add(
                            ProcessedEvent(
                                tenant_id=envelope.tenant_id,
                                consumer=self._consumer_name,
                                event_id=envelope.event_id,
                                event_type=envelope.event_type,
                            )
                        )
                        await session.flush()
                    await self._resolve_failure(envelope)
                    return True
                except IntegrityError:
                    return True
                except Exception as exc:
                    logger.exception(
                        "domain_event_handler_failed",
                        extra={
                            "context": {
                                "event_id": str(envelope.event_id),
                                "event_type": envelope.event_type,
                                "consumer": self._consumer_name,
                                "attempt": attempt,
                            }
                        },
                    )
                    await self._record_failure(envelope, type(exc).__name__)
                    if attempt < self._settings.event_consumer_max_attempts:
                        await asyncio.sleep(min(1.0, 0.1 * attempt))
        return False

    async def _record_failure(
        self,
        envelope: DomainEventEnvelope,
        error_code: str,
        *,
        terminal: bool = False,
    ) -> None:
        with tenant_context(envelope.tenant_id):
            async with self._session_factory() as session, session.begin():
                failure = await session.scalar(
                    select(EventConsumerFailure)
                    .where(
                        EventConsumerFailure.tenant_id == envelope.tenant_id,
                        EventConsumerFailure.consumer == self._consumer_name,
                        EventConsumerFailure.event_id == envelope.event_id,
                    )
                    .with_for_update()
                )
                if failure is None:
                    max_attempts = self._settings.event_consumer_max_attempts
                    failure = EventConsumerFailure(
                        tenant_id=envelope.tenant_id,
                        consumer=self._consumer_name,
                        event_id=envelope.event_id,
                        event_type=envelope.event_type,
                        status=(
                            ConsumerFailureStatus.DEAD_LETTER.value
                            if terminal or max_attempts <= 1
                            else ConsumerFailureStatus.OPEN.value
                        ),
                        attempts=1,
                        max_attempts=max_attempts,
                        payload=envelope.model_dump(mode="json"),
                        headers=envelope.headers,
                        last_error=error_code[:500],
                    )
                    session.add(failure)
                else:
                    failure.attempts += 1
                    failure.last_error = error_code[:500]
                    if terminal or failure.attempts >= failure.max_attempts:
                        failure.status = ConsumerFailureStatus.DEAD_LETTER.value
                await session.flush()

    async def _resolve_failure(self, envelope: DomainEventEnvelope) -> None:
        with tenant_context(envelope.tenant_id):
            async with self._session_factory() as session, session.begin():
                failure = await session.scalar(
                    select(EventConsumerFailure)
                    .where(
                        EventConsumerFailure.tenant_id == envelope.tenant_id,
                        EventConsumerFailure.consumer == self._consumer_name,
                        EventConsumerFailure.event_id == envelope.event_id,
                    )
                    .with_for_update()
                )
                if failure is None:
                    return
                failure.status = ConsumerFailureStatus.RESOLVED.value
                failure.resolved_at = datetime.now(UTC)
                await session.flush()

    async def _handle_message(self, message: ConsumerRecord) -> None:
        try:
            envelope = DomainEventEnvelope.model_validate_json(message.value)
        except Exception:
            logger.error(
                "domain_event_invalid_message",
                extra={
                    "context": {
                        "topic": message.topic,
                        "partition": message.partition,
                        "offset": message.offset,
                    }
                },
            )
            return
        await self.process_envelope(envelope)

    async def aclose(self) -> None:
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None


def merge_registries(registries: Sequence[EventHandlerRegistry]) -> EventHandlerRegistry:
    merged = EventHandlerRegistry()
    for registry in registries:
        for event_type, handlers in registry._handlers.items():
            for handler in handlers:
                merged.register(event_type, handler)
        for handler in registry._wildcard:
            merged.register("*", handler)
    return merged


__all__ = [
    "EventConsumer",
    "EventHandler",
    "EventHandlerRegistry",
    "merge_registries",
]
