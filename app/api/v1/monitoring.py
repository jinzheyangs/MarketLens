"""监测订阅、通知渠道、运行记录与变更接口。"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.api.v1.research_scope import require_project, require_workspace
from app.auth.authorization import AuthorizationService
from app.auth.dependencies import get_authorization, get_principal, get_settings_dep
from app.auth.enums import Permission
from app.auth.principal import Principal
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.db.models import MonitorSubscription
from app.monitoring.enums import MonitorStatus, Severity
from app.monitoring.service import (
    create_notification_channel,
    create_subscription,
    disable_notification_channel,
    get_notification_channel,
    get_subscription,
    list_changes,
    list_deliveries,
    list_notification_channels,
    list_runs,
    list_subscriptions,
    set_subscription_status,
    trigger_subscription,
    update_notification_channel,
    update_subscription,
)
from app.monitoring.webhook import SafeWebhookClient
from app.schemas.monitoring import (
    MonitorChangeRead,
    MonitorRunRead,
    MonitorSubscriptionCreate,
    MonitorSubscriptionRead,
    MonitorSubscriptionUpdate,
    NotificationChannelCreate,
    NotificationChannelRead,
    NotificationChannelUpdate,
    NotificationDeliveryRead,
)
from app.services.tenancy import project_context, workspace_context

router = APIRouter(tags=["monitoring"])


async def _workspace_authz(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    permission: Permission,
    authz: AuthorizationService,
) -> None:
    workspace, organization = await require_workspace(session, workspace_id)
    await authz.require(permission, workspace_context(workspace, organization))


async def _subscription(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    subscription_id: UUID,
    permission: Permission,
    authz: AuthorizationService,
) -> MonitorSubscription:
    subscription = await get_subscription(
        session,
        workspace_id=workspace_id,
        subscription_id=subscription_id,
    )
    if subscription is None or subscription.status == MonitorStatus.DELETED.value:
        raise NotFoundError("The monitoring subscription was not found.")
    if subscription.project_id is not None:
        bundle = await require_project(
            session,
            workspace_id=workspace_id,
            project_id=subscription.project_id,
        )
        await authz.require(permission, project_context(*bundle))
    else:
        await _workspace_authz(
            session,
            workspace_id,
            permission=permission,
            authz=authz,
        )
    return subscription


@router.get(
    "/workspaces/{workspace_id}/notification-channels",
    response_model=list[NotificationChannelRead],
    summary="List notification channels",
)
async def get_notification_channels(
    workspace_id: UUID,
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> list[NotificationChannelRead]:
    await _workspace_authz(
        session,
        workspace_id,
        permission=Permission.NOTIFICATION_CHANNEL_MANAGE,
        authz=authz,
    )
    channels = await list_notification_channels(session, workspace_id=workspace_id)
    return [NotificationChannelRead.model_validate(channel) for channel in channels]


@router.post(
    "/workspaces/{workspace_id}/notification-channels",
    response_model=NotificationChannelRead,
    status_code=201,
    summary="Create a notification channel",
)
async def post_notification_channel(
    workspace_id: UUID,
    payload: NotificationChannelCreate,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> NotificationChannelRead:
    await _workspace_authz(
        session,
        workspace_id,
        permission=Permission.NOTIFICATION_CHANNEL_MANAGE,
        authz=authz,
    )
    channel = await create_notification_channel(
        session,
        settings=settings,
        webhook=SafeWebhookClient(settings),
        workspace_id=workspace_id,
        principal=principal,
        name=payload.name,
        kind=payload.kind,
        url=payload.url,
        signing_secret=(
            payload.signing_secret.get_secret_value() if payload.signing_secret else None
        ),
        email_to=(
            [str(value) for value in payload.email_to] if payload.email_to is not None else None
        ),
    )
    return NotificationChannelRead.model_validate(channel)


@router.patch(
    "/workspaces/{workspace_id}/notification-channels/{channel_id}",
    response_model=NotificationChannelRead,
    summary="Update a notification channel",
)
async def patch_notification_channel(
    workspace_id: UUID,
    channel_id: UUID,
    payload: NotificationChannelUpdate,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> NotificationChannelRead:
    await _workspace_authz(
        session,
        workspace_id,
        permission=Permission.NOTIFICATION_CHANNEL_MANAGE,
        authz=authz,
    )
    channel = await get_notification_channel(
        session,
        workspace_id=workspace_id,
        channel_id=channel_id,
    )
    if channel is None:
        raise NotFoundError("The notification channel was not found.")
    channel = await update_notification_channel(
        session,
        settings=settings,
        webhook=SafeWebhookClient(settings),
        channel=channel,
        principal=principal,
        name=payload.name,
        url=payload.url,
        signing_secret=(
            payload.signing_secret.get_secret_value()
            if payload.signing_secret is not None
            else None
        ),
        clear_signing_secret=payload.clear_signing_secret,
        email_to=(
            [str(value) for value in payload.email_to] if payload.email_to is not None else None
        ),
    )
    return NotificationChannelRead.model_validate(channel)


@router.delete(
    "/workspaces/{workspace_id}/notification-channels/{channel_id}",
    response_model=NotificationChannelRead,
    summary="Disable a notification channel",
)
async def delete_notification_channel(
    workspace_id: UUID,
    channel_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> NotificationChannelRead:
    await _workspace_authz(
        session,
        workspace_id,
        permission=Permission.NOTIFICATION_CHANNEL_MANAGE,
        authz=authz,
    )
    channel = await get_notification_channel(
        session,
        workspace_id=workspace_id,
        channel_id=channel_id,
    )
    if channel is None:
        raise NotFoundError("The notification channel was not found.")
    channel = await disable_notification_channel(
        session,
        channel=channel,
        principal=principal,
    )
    return NotificationChannelRead.model_validate(channel)


@router.get(
    "/workspaces/{workspace_id}/monitors",
    response_model=list[MonitorSubscriptionRead],
    summary="List monitoring subscriptions",
)
async def get_monitors(
    workspace_id: UUID,
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> list[MonitorSubscriptionRead]:
    await _workspace_authz(
        session,
        workspace_id,
        permission=Permission.MONITOR_READ,
        authz=authz,
    )
    subscriptions = await list_subscriptions(session, workspace_id=workspace_id)
    return [MonitorSubscriptionRead.model_validate(subscription) for subscription in subscriptions]


@router.post(
    "/workspaces/{workspace_id}/monitors",
    response_model=MonitorSubscriptionRead,
    status_code=201,
    summary="Create a monitoring subscription",
)
async def post_monitor(
    workspace_id: UUID,
    payload: MonitorSubscriptionCreate,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> MonitorSubscriptionRead:
    if payload.project_id is not None:
        bundle = await require_project(
            session,
            workspace_id=workspace_id,
            project_id=payload.project_id,
        )
        await authz.require(Permission.MONITOR_MANAGE, project_context(*bundle))
    else:
        await _workspace_authz(
            session,
            workspace_id,
            permission=Permission.MONITOR_MANAGE,
            authz=authz,
        )
    subscription = await create_subscription(
        session,
        settings=settings,
        principal=principal,
        workspace_id=workspace_id,
        project_id=payload.project_id,
        name=payload.name,
        kind=payload.kind.value,
        query=payload.query,
        cadence_minutes=payload.cadence_minutes or settings.monitoring_default_cadence_minutes,
        alerts=[(alert.channel_id, Severity(alert.min_severity.value)) for alert in payload.alerts],
    )
    return MonitorSubscriptionRead.model_validate(subscription)


@router.get(
    "/workspaces/{workspace_id}/monitors/{subscription_id}",
    response_model=MonitorSubscriptionRead,
    summary="Get a monitoring subscription",
)
async def get_monitor(
    workspace_id: UUID,
    subscription_id: UUID,
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> MonitorSubscriptionRead:
    subscription = await _subscription(
        session,
        workspace_id=workspace_id,
        subscription_id=subscription_id,
        permission=Permission.MONITOR_READ,
        authz=authz,
    )
    return MonitorSubscriptionRead.model_validate(subscription)


@router.patch(
    "/workspaces/{workspace_id}/monitors/{subscription_id}",
    response_model=MonitorSubscriptionRead,
    summary="Update a monitoring subscription",
)
async def patch_monitor(
    workspace_id: UUID,
    subscription_id: UUID,
    payload: MonitorSubscriptionUpdate,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> MonitorSubscriptionRead:
    subscription = await _subscription(
        session,
        workspace_id=workspace_id,
        subscription_id=subscription_id,
        permission=Permission.MONITOR_MANAGE,
        authz=authz,
    )
    subscription = await update_subscription(
        session,
        settings=settings,
        subscription=subscription,
        principal=principal,
        query=payload.query,
        cadence_minutes=payload.cadence_minutes,
        alerts=(
            [(alert.channel_id, alert.min_severity) for alert in payload.alerts]
            if payload.alerts is not None
            else None
        ),
    )
    return MonitorSubscriptionRead.model_validate(subscription)


@router.delete(
    "/workspaces/{workspace_id}/monitors/{subscription_id}",
    response_model=MonitorSubscriptionRead,
    summary="Delete a monitoring subscription",
)
async def delete_monitor(
    workspace_id: UUID,
    subscription_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> MonitorSubscriptionRead:
    subscription = await _subscription(
        session,
        workspace_id=workspace_id,
        subscription_id=subscription_id,
        permission=Permission.MONITOR_MANAGE,
        authz=authz,
    )
    subscription = await set_subscription_status(
        session,
        subscription=subscription,
        principal=principal,
        status=MonitorStatus.DELETED,
    )
    return MonitorSubscriptionRead.model_validate(subscription)


@router.post(
    "/workspaces/{workspace_id}/monitors/{subscription_id}/trigger",
    response_model=MonitorSubscriptionRead,
    summary="Request an immediate monitoring run",
)
async def trigger_monitor(
    workspace_id: UUID,
    subscription_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> MonitorSubscriptionRead:
    subscription = await _subscription(
        session,
        workspace_id=workspace_id,
        subscription_id=subscription_id,
        permission=Permission.MONITOR_MANAGE,
        authz=authz,
    )
    subscription = await trigger_subscription(
        session,
        subscription=subscription,
        principal=principal,
    )
    return MonitorSubscriptionRead.model_validate(subscription)


async def _set_status(
    *,
    session: AsyncSession,
    workspace_id: UUID,
    subscription_id: UUID,
    principal: Principal,
    authz: AuthorizationService,
    status: MonitorStatus,
) -> MonitorSubscriptionRead:
    subscription = await _subscription(
        session,
        workspace_id=workspace_id,
        subscription_id=subscription_id,
        permission=Permission.MONITOR_MANAGE,
        authz=authz,
    )
    subscription = await set_subscription_status(
        session,
        subscription=subscription,
        principal=principal,
        status=status,
    )
    return MonitorSubscriptionRead.model_validate(subscription)


@router.post(
    "/workspaces/{workspace_id}/monitors/{subscription_id}/pause",
    response_model=MonitorSubscriptionRead,
    summary="Pause a monitoring subscription",
)
async def pause_monitor(
    workspace_id: UUID,
    subscription_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> MonitorSubscriptionRead:
    return await _set_status(
        session=session,
        workspace_id=workspace_id,
        subscription_id=subscription_id,
        principal=principal,
        authz=authz,
        status=MonitorStatus.PAUSED,
    )


@router.post(
    "/workspaces/{workspace_id}/monitors/{subscription_id}/resume",
    response_model=MonitorSubscriptionRead,
    summary="Resume a monitoring subscription",
)
async def resume_monitor(
    workspace_id: UUID,
    subscription_id: UUID,
    principal: Principal = Depends(get_principal),
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
) -> MonitorSubscriptionRead:
    return await _set_status(
        session=session,
        workspace_id=workspace_id,
        subscription_id=subscription_id,
        principal=principal,
        authz=authz,
        status=MonitorStatus.ACTIVE,
    )


@router.get(
    "/workspaces/{workspace_id}/monitors/{subscription_id}/runs",
    response_model=list[MonitorRunRead],
    summary="List monitoring runs",
)
async def get_monitor_runs(
    workspace_id: UUID,
    subscription_id: UUID,
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[MonitorRunRead]:
    await _subscription(
        session,
        workspace_id=workspace_id,
        subscription_id=subscription_id,
        permission=Permission.MONITOR_READ,
        authz=authz,
    )
    runs = await list_runs(
        session,
        subscription_id=subscription_id,
        limit=limit,
        offset=offset,
    )
    return [MonitorRunRead.model_validate(run) for run in runs]


@router.get(
    "/workspaces/{workspace_id}/monitors/{subscription_id}/changes",
    response_model=list[MonitorChangeRead],
    summary="List detected monitoring changes",
)
async def get_monitor_changes(
    workspace_id: UUID,
    subscription_id: UUID,
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[MonitorChangeRead]:
    await _subscription(
        session,
        workspace_id=workspace_id,
        subscription_id=subscription_id,
        permission=Permission.MONITOR_READ,
        authz=authz,
    )
    changes = await list_changes(
        session,
        subscription_id=subscription_id,
        limit=limit,
        offset=offset,
    )
    return [MonitorChangeRead.model_validate(change) for change in changes]


@router.get(
    "/workspaces/{workspace_id}/monitors/{subscription_id}/deliveries",
    response_model=list[NotificationDeliveryRead],
    summary="List notification deliveries",
)
async def get_monitor_deliveries(
    workspace_id: UUID,
    subscription_id: UUID,
    authz: AuthorizationService = Depends(get_authorization),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[NotificationDeliveryRead]:
    await _subscription(
        session,
        workspace_id=workspace_id,
        subscription_id=subscription_id,
        permission=Permission.MONITOR_READ,
        authz=authz,
    )
    deliveries = await list_deliveries(
        session,
        subscription_id=subscription_id,
        limit=limit,
        offset=offset,
    )
    return [NotificationDeliveryRead.model_validate(delivery) for delivery in deliveries]
