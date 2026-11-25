"""事件发布抽象：Kafka 适配器与本地日志适配器。"""

from __future__ import annotations

import ssl
from typing import Protocol

from aiokafka import AIOKafkaProducer

from app.core.config import Settings
from app.core.logging import get_logger
from app.events.schemas import DomainEventEnvelope

logger = get_logger(__name__)


class EventPublisherError(Exception):
    """Safe publisher failure; details stay in logs."""


class EventPublisher(Protocol):
    async def start(self) -> None: ...

    async def publish(self, envelope: DomainEventEnvelope) -> None: ...

    async def aclose(self) -> None: ...


class LoggingEventPublisher:
    """Development adapter: logs metadata only and acknowledges the event."""

    async def start(self) -> None:
        return None

    async def publish(self, envelope: DomainEventEnvelope) -> None:
        logger.info(
            "domain_event_emitted",
            extra={
                "context": {
                    "event_id": str(envelope.event_id),
                    "event_type": envelope.event_type,
                    "event_version": envelope.event_version,
                    "tenant_id": str(envelope.tenant_id),
                    "aggregate_type": envelope.aggregate_type,
                }
            },
        )

    async def aclose(self) -> None:
        return None


class KafkaEventPublisher:
    def __init__(self, settings: Settings) -> None:
        self._topic = settings.event_topic
        ssl_context = None
        if settings.kafka_ssl_cafile:
            ssl_context = ssl.create_default_context(cafile=settings.kafka_ssl_cafile)
        self._producer = AIOKafkaProducer(
            bootstrap_servers=list(settings.kafka_bootstrap_servers),
            client_id=settings.kafka_client_id,
            acks="all",
            enable_idempotence=True,
            security_protocol=settings.kafka_security_protocol,
            sasl_mechanism=settings.kafka_sasl_mechanism,
            sasl_plain_username=settings.kafka_sasl_username,
            sasl_plain_password=(
                settings.kafka_sasl_password.get_secret_value()
                if settings.kafka_sasl_password is not None
                else None
            ),
            ssl_context=ssl_context,
        )
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        try:
            await self._producer.start()
        except Exception as exc:
            raise EventPublisherError("Kafka producer could not start.") from exc
        self._started = True

    async def publish(self, envelope: DomainEventEnvelope) -> None:
        if not self._started:
            await self.start()
        try:
            await self._producer.send_and_wait(
                self._topic,
                key=envelope.partition_key.encode("utf-8"),
                value=envelope.model_dump_json().encode("utf-8"),
                headers=[
                    ("event_type", envelope.event_type.encode("utf-8")),
                    ("event_version", str(envelope.event_version).encode("utf-8")),
                ],
            )
        except Exception as exc:
            raise EventPublisherError("Kafka event publication failed.") from exc

    async def aclose(self) -> None:
        if self._started:
            await self._producer.stop()
            self._started = False


def build_event_publisher(settings: Settings) -> EventPublisher:
    if settings.event_publisher == "kafka":
        return KafkaEventPublisher(settings)
    return LoggingEventPublisher()


__all__ = [
    "EventPublisher",
    "EventPublisherError",
    "KafkaEventPublisher",
    "LoggingEventPublisher",
    "build_event_publisher",
]
