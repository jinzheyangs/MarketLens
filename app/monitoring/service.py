"""Monitoring subscriptions, notification channels, changes, and deliveries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.enums import AuditAction, ResourceType
from app.auth.principal import Principal
from app.collector.service import CollectorServices
from app.core.config import Settings
from app.core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.db.models import (
    MonitorAlertRule,
    MonitorChange,
    MonitorRun,
    MonitorSubscription,
    NotificationChannel,
    NotificationDelivery,
)
from app.db.tenant_scope import tenant_scoped_select
from app.governance.quotas import QuotaResource, ensure_quota
from app.monitoring.crypto import ChannelSecretCipher
from app.monitoring.enums import (
    MonitorStatus,
    NotificationChannelKind,
    Severity,
    severity_rank,
)
from app.monitoring.errors import ChannelSecretError
from app.monitoring.webhook import SafeWebhookClient
from app.services.audit import record_event


@dataclass(slots=True)
class MonitoringServices:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    collector_services: CollectorServices | None
    webhook: SafeWebhookClient

    async def aclose(self) -> None:
        return None


def build_monitoring_services(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    collector_services: CollectorServices | None,
) -> MonitoringServices:
    return MonitoringServices(
        settings=settings,
        session_factory=session_factory,
        collector_services=collector_services,
        webhook=SafeWebhookClient(settings),
    )


def require_monitoring_services(
    services: MonitoringServices | None,
) -> MonitoringServices:
    if services is None:
        raise ServiceUnavailableError("Monitoring is not configured.")
    return services


def _validate_signing_secret(value: str) -> str:
    if not 8 <= len(value) <= 256:
        raise BadRequestError("signing_secret must be 8-256 characters")
    return value


async def create_notification_channel(
    session: AsyncSession,
    *,
    settings: Settings,
    webhook: SafeWebhookClient,
    workspace_id: UUID,
    principal: Principal,
    name: str,
    kind: NotificationChannelKind,
    url: str | None,
    signing_secret: str | None,
) -> NotificationChannel:
    encrypted: str | None = None
    if kind is NotificationChannelKind.WEBHOOK:
        if not url:
            raise BadRequestError("webhook URL is required")
        webhook.validate_target(url)
        config: dict[str, str] = {"url": url}
        if signing_secret:
            config["signing_secret"] = _validate_signing_secret(signing_secret)
        try:
            encrypted = ChannelSecretCipher(settings).encrypt(config)
        except ChannelSecretError as exc:
            raise ServiceUnavailableError("Notification encryption is not configured.") from exc
    channel = NotificationChannel(
        id=uuid4(),
        tenant_id=principal.tenant_id,
        workspace_id=workspace_id,
        name=name.strip(),
        kind=kind.value,
        encrypted_config=encrypted,
        created_by=principal.user_id,
    )
    try:
        async with session.begin_nested():
            session.add(channel)
            await session.flush()
    except IntegrityError as exc:
        raise ConflictError("A notification channel with this name already exists.") from exc
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.NOTIFICATION_CHANNEL_CREATED,
        resource_type=ResourceType.NOTIFICATION_CHANNEL,
        resource_id=channel.id,
        details={"kind": channel.kind},
    )
    await session.refresh(channel)
    return channel


async def update_notification_channel(
    session: AsyncSession,
    *,
    settings: Settings,
    webhook: SafeWebhookClient,
    channel: NotificationChannel,
    principal: Principal,
    name: str | None,
    url: str | None,
    signing_secret: str | None,
    clear_signing_secret: bool = False,
) -> NotificationChannel:
    kind = NotificationChannelKind(channel.kind)
    candidate_name = name.strip() if name is not None else None
    if candidate_name is not None and (not candidate_name or len(candidate_name) > 120):
        raise BadRequestError("channel name is invalid")

    config_changed = False
    if kind is NotificationChannelKind.WEBHOOK:
        needs_config = url is not None or signing_secret is not None or clear_signing_secret
        if needs_config:
            if channel.encrypted_config is None:
                raise ConflictError("The channel has no stored configuration.")
            try:
                config = ChannelSecretCipher(settings).decrypt(channel.encrypted_config)
            except ChannelSecretError as exc:
                raise ConflictError(
                    "The channel secret cannot be decrypted with the configured keys."
                ) from exc
            if url is not None:
                webhook.validate_target(url)
                config["url"] = url
                config_changed = True
            if signing_secret is not None and clear_signing_secret:
                raise BadRequestError("cannot set and clear signing_secret together")
            if signing_secret is not None:
                config["signing_secret"] = _validate_signing_secret(signing_secret)
                config_changed = True
            elif clear_signing_secret:
                config.pop("signing_secret", None)
                config_changed = True
            if config_changed:
                channel.encrypted_config = ChannelSecretCipher(settings).encrypt(config)
    elif url is not None or signing_secret is not None or clear_signing_secret:
        raise BadRequestError("log channels do not accept webhook configuration")

    if candidate_name is not None:
        channel.name = candidate_name
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        raise ConflictError("A notification channel with this name already exists.") from exc
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.NOTIFICATION_CHANNEL_UPDATED,
        resource_type=ResourceType.NOTIFICATION_CHANNEL,
        resource_id=channel.id,
        details={
            "name_changed": candidate_name is not None,
            "config_changed": config_changed,
        },
    )
    await session.refresh(channel)
    return channel


async def list_notification_channels(
    session: AsyncSession,
    *,
    workspace_id: UUID,
) -> list[NotificationChannel]:
    return list(
        (
            await session.scalars(
                tenant_scoped_select(NotificationChannel)
                .where(NotificationChannel.workspace_id == workspace_id)
                .order_by(NotificationChannel.created_at, NotificationChannel.id)
            )
        ).all()
    )


async def get_notification_channel(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    channel_id: UUID,
) -> NotificationChannel | None:
    return cast(
        NotificationChannel | None,
        await session.scalar(
            tenant_scoped_select(NotificationChannel).where(
                NotificationChannel.workspace_id == workspace_id,
                NotificationChannel.id == channel_id,
            )
        ),
    )


async def disable_notification_channel(
    session: AsyncSession,
    *,
    channel: NotificationChannel,
    principal: Principal,
) -> NotificationChannel:
    channel.enabled = False
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.NOTIFICATION_CHANNEL_DISABLED,
        resource_type=ResourceType.NOTIFICATION_CHANNEL,
        resource_id=channel.id,
        details={"enabled": False},
    )
    await session.flush()
    await session.refresh(channel)
    return channel


def _normalize_alerts(
    alerts: list[tuple[UUID, Severity]],
) -> list[tuple[UUID, Severity]]:
    selected: dict[UUID, Severity] = {}
    for channel_id, severity in alerts:
        existing = selected.get(channel_id)
        if existing is None or severity_rank(severity.value) > severity_rank(existing.value):
            selected[channel_id] = severity
    return list(selected.items())


async def create_subscription(
    session: AsyncSession,
    *,
    settings: Settings,
    principal: Principal,
    workspace_id: UUID,
    project_id: UUID | None,
    name: str,
    kind: str,
    query: str,
    cadence_minutes: int,
    alerts: list[tuple[UUID, Severity]],
) -> MonitorSubscription:
    candidate_name = name.strip()
    candidate_query = query.strip()
    if not candidate_name or len(candidate_name) > 120:
        raise BadRequestError("subscription name is invalid")
    if not candidate_query or len(candidate_query) > 500:
        raise BadRequestError("subscription query is invalid")
    if not (
        settings.monitoring_min_cadence_minutes
        <= cadence_minutes
        <= settings.monitoring_max_cadence_minutes
    ):
        raise BadRequestError("cadence_minutes is outside the configured range")
    await ensure_quota(
        session,
        tenant_id=principal.tenant_id,
        resource=QuotaResource.MONITOR_SUBSCRIPTIONS,
        settings=settings,
    )
    alerts = _normalize_alerts(alerts)
    channel_ids = [channel_id for channel_id, _ in alerts]
    if channel_ids:
        channels = list(
            (
                await session.scalars(
                    tenant_scoped_select(NotificationChannel).where(
                        NotificationChannel.workspace_id == workspace_id,
                        NotificationChannel.id.in_(channel_ids),
                        NotificationChannel.enabled.is_(True),
                    )
                )
            ).all()
        )
        if len({channel.id for channel in channels}) != len(set(channel_ids)):
            raise NotFoundError("One or more notification channels were not found.")
    subscription = MonitorSubscription(
        id=uuid4(),
        tenant_id=principal.tenant_id,
        workspace_id=workspace_id,
        project_id=project_id,
        name=candidate_name,
        kind=kind,
        query=candidate_query,
        status=MonitorStatus.ACTIVE.value,
        cadence_minutes=cadence_minutes,
        next_run_at=datetime.now(UTC),
        created_by=principal.user_id,
    )
    try:
        async with session.begin_nested():
            session.add(subscription)
            await session.flush()
    except IntegrityError as exc:
        raise ConflictError("A monitoring subscription with this name already exists.") from exc
    for channel_id, min_severity in alerts:
        session.add(
            MonitorAlertRule(
                tenant_id=principal.tenant_id,
                subscription_id=subscription.id,
                channel_id=channel_id,
                min_severity=min_severity.value,
            )
        )
    await session.flush()
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.MONITOR_SUBSCRIPTION_CREATED,
        resource_type=ResourceType.MONITOR_SUBSCRIPTION,
        resource_id=subscription.id,
        details={
            "workspace_id": str(workspace_id),
            "project_id": str(project_id) if project_id else None,
            "kind": kind,
            "cadence_minutes": cadence_minutes,
            "alert_channels": len(alerts),
        },
    )
    await session.refresh(subscription)
    return subscription


async def list_subscriptions(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    include_deleted: bool = False,
) -> list[MonitorSubscription]:
    statement = tenant_scoped_select(MonitorSubscription).where(
        MonitorSubscription.workspace_id == workspace_id
    )
    if not include_deleted:
        statement = statement.where(MonitorSubscription.status != MonitorStatus.DELETED.value)
    statement = statement.order_by(
        MonitorSubscription.created_at,
        MonitorSubscription.id,
    )
    return list((await session.scalars(statement)).all())


async def get_subscription(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    subscription_id: UUID,
) -> MonitorSubscription | None:
    return cast(
        MonitorSubscription | None,
        await session.scalar(
            tenant_scoped_select(MonitorSubscription).where(
                MonitorSubscription.workspace_id == workspace_id,
                MonitorSubscription.id == subscription_id,
            )
        ),
    )


async def update_subscription(
    session: AsyncSession,
    *,
    settings: Settings,
    subscription: MonitorSubscription,
    principal: Principal,
    query: str | None,
    cadence_minutes: int | None,
    alerts: list[tuple[UUID, Severity]] | None,
) -> MonitorSubscription:
    if query is not None:
        candidate = query.strip()
        if not candidate or len(candidate) > 500:
            raise BadRequestError("subscription query is invalid")
        subscription.query = candidate
    if cadence_minutes is not None:
        if not (
            settings.monitoring_min_cadence_minutes
            <= cadence_minutes
            <= settings.monitoring_max_cadence_minutes
        ):
            raise BadRequestError("cadence_minutes is outside the configured range")
        subscription.cadence_minutes = cadence_minutes
    if alerts is not None:
        alerts = _normalize_alerts(alerts)
        channel_ids = [channel_id for channel_id, _ in alerts]
        if channel_ids:
            channels = list(
                (
                    await session.scalars(
                        tenant_scoped_select(NotificationChannel).where(
                            NotificationChannel.workspace_id == subscription.workspace_id,
                            NotificationChannel.id.in_(channel_ids),
                            NotificationChannel.enabled.is_(True),
                        )
                    )
                ).all()
            )
            if len({channel.id for channel in channels}) != len(set(channel_ids)):
                raise NotFoundError("One or more notification channels were not found.")
        existing = list(
            (
                await session.scalars(
                    tenant_scoped_select(MonitorAlertRule).where(
                        MonitorAlertRule.subscription_id == subscription.id
                    )
                )
            ).all()
        )
        for rule in existing:
            await session.delete(rule)
        for channel_id, min_severity in alerts:
            session.add(
                MonitorAlertRule(
                    tenant_id=principal.tenant_id,
                    subscription_id=subscription.id,
                    channel_id=channel_id,
                    min_severity=min_severity.value,
                )
            )
    await session.flush()
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.MONITOR_SUBSCRIPTION_UPDATED,
        resource_type=ResourceType.MONITOR_SUBSCRIPTION,
        resource_id=subscription.id,
        details={"alerts_replaced": alerts is not None},
    )
    await session.refresh(subscription)
    return subscription


async def set_subscription_status(
    session: AsyncSession,
    *,
    subscription: MonitorSubscription,
    principal: Principal,
    status: MonitorStatus,
) -> MonitorSubscription:
    subscription.status = status.value
    if status is MonitorStatus.ACTIVE:
        subscription.next_run_at = datetime.now(UTC)
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=(
            AuditAction.MONITOR_SUBSCRIPTION_DELETED
            if status is MonitorStatus.DELETED
            else AuditAction.MONITOR_SUBSCRIPTION_UPDATED
        ),
        resource_type=ResourceType.MONITOR_SUBSCRIPTION,
        resource_id=subscription.id,
        details={"status": status.value},
    )
    await session.flush()
    await session.refresh(subscription)
    return subscription


async def trigger_subscription(
    session: AsyncSession,
    *,
    subscription: MonitorSubscription,
    principal: Principal,
) -> MonitorSubscription:
    if subscription.status != MonitorStatus.ACTIVE.value:
        raise ConflictError("Only an active subscription can be triggered.")
    subscription.next_run_at = datetime.now(UTC)
    record_event(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action=AuditAction.MONITOR_SUBSCRIPTION_UPDATED,
        resource_type=ResourceType.MONITOR_SUBSCRIPTION,
        resource_id=subscription.id,
        details={"manual_trigger": True},
    )
    await session.flush()
    await session.refresh(subscription)
    return subscription


async def list_runs(
    session: AsyncSession,
    *,
    subscription_id: UUID,
    limit: int,
    offset: int,
) -> list[MonitorRun]:
    return list(
        (
            await session.scalars(
                tenant_scoped_select(MonitorRun)
                .where(MonitorRun.subscription_id == subscription_id)
                .order_by(MonitorRun.created_at.desc(), MonitorRun.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )


async def list_changes(
    session: AsyncSession,
    *,
    subscription_id: UUID,
    limit: int,
    offset: int,
) -> list[MonitorChange]:
    return list(
        (
            await session.scalars(
                tenant_scoped_select(MonitorChange)
                .where(MonitorChange.subscription_id == subscription_id)
                .order_by(MonitorChange.detected_at.desc(), MonitorChange.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )


async def list_deliveries(
    session: AsyncSession,
    *,
    subscription_id: UUID,
    limit: int,
    offset: int,
) -> list[NotificationDelivery]:
    return list(
        (
            await session.scalars(
                tenant_scoped_select(NotificationDelivery)
                .where(NotificationDelivery.subscription_id == subscription_id)
                .order_by(NotificationDelivery.created_at.desc(), NotificationDelivery.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )


async def get_alert_rules(
    session: AsyncSession,
    *,
    subscription_id: UUID,
) -> list[MonitorAlertRule]:
    return list(
        (
            await session.scalars(
                tenant_scoped_select(MonitorAlertRule).where(
                    MonitorAlertRule.subscription_id == subscription_id,
                    MonitorAlertRule.enabled.is_(True),
                )
            )
        ).all()
    )


def severity_at_least(value: str, minimum: str) -> bool:
    known = {severity.value for severity in Severity}
    if value not in known or minimum not in known:
        return False
    return severity_rank(value) >= severity_rank(minimum)


__all__ = [
    "MonitoringServices",
    "build_monitoring_services",
    "create_notification_channel",
    "create_subscription",
    "disable_notification_channel",
    "get_alert_rules",
    "get_notification_channel",
    "get_subscription",
    "list_changes",
    "list_deliveries",
    "list_notification_channels",
    "list_runs",
    "list_subscriptions",
    "require_monitoring_services",
    "set_subscription_status",
    "severity_at_least",
    "trigger_subscription",
    "update_notification_channel",
    "update_subscription",
]
