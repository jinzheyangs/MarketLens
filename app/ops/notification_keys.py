"""用当前主密钥对通知渠道密钥重新加密。"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.enums import AuditAction, ResourceType
from app.core.config import Settings, get_settings
from app.core.context import tenant_context
from app.core.logging import configure_logging, get_logger
from app.db.models import NotificationChannel, Tenant
from app.db.rls import install_rls_context
from app.db.session import create_engine, create_session_factory
from app.db.tenant_guard import install_tenant_guard
from app.db.tenant_scope import tenant_scoped_select
from app.monitoring.crypto import ChannelSecretCipher
from app.monitoring.errors import ChannelSecretError
from app.services.audit import record_event

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RotationResult:
    tenant_id: UUID
    rotated: int
    failed: int


async def rotate_tenant_keys(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    tenant_id: UUID,
    execute: bool,
) -> RotationResult:
    cipher = ChannelSecretCipher(settings)
    rotated = 0
    failed = 0
    with tenant_context(tenant_id):
        async with session_factory() as session, session.begin():
            channels = list(
                (
                    await session.scalars(
                        tenant_scoped_select(NotificationChannel).where(
                            NotificationChannel.encrypted_config.is_not(None)
                        )
                    )
                ).all()
            )
            for channel in channels:
                if channel.encrypted_config is None:  # pragma: no cover - query filter
                    continue
                try:
                    config = cipher.decrypt(channel.encrypted_config)
                except ChannelSecretError:
                    failed += 1
                    logger.warning(
                        "notification_key_rotation_failed",
                        extra={
                            "context": {
                                "tenant_id": str(tenant_id),
                                "channel_id": str(channel.id),
                            }
                        },
                    )
                    continue
                rotated += 1
                if execute:
                    channel.encrypted_config = cipher.encrypt(config)
            if execute and (rotated or failed):
                record_event(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=None,
                    action=AuditAction.NOTIFICATION_KEYS_ROTATED,
                    resource_type=ResourceType.NOTIFICATION_CHANNEL,
                    resource_id=None,
                    details={"rotated": rotated, "failed": failed},
                )
    return RotationResult(tenant_id=tenant_id, rotated=rotated, failed=failed)


async def rotate_notification_keys(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tenant_ids: Sequence[UUID] | None = None,
    execute: bool = False,
) -> list[RotationResult]:
    async with session_factory() as session:
        statement = select(Tenant.id).where(Tenant.status == "active")
        if tenant_ids is not None:
            statement = statement.where(Tenant.id.in_(tenant_ids))
        active = list((await session.scalars(statement)).all())
    return [
        await rotate_tenant_keys(
            session_factory,
            settings=settings,
            tenant_id=tenant_id,
            execute=execute,
        )
        for tenant_id in active
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rotate notification channel encryption keys")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="write re-encrypted secrets (default is a dry-run verification)",
    )
    parser.add_argument("--tenant", action="append", help="restrict to a tenant UUID")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)
    if settings.notification_encryption_key is None:
        logger.error("notification_encryption_key_missing")
        return 2
    if settings.notification_previous_encryption_key is None:
        logger.warning("notification_previous_key_missing")

    tenant_ids = None
    if args.tenant:
        try:
            tenant_ids = [UUID(value) for value in args.tenant]
        except ValueError:
            logger.error("notification_key_rotation_invalid_tenant")
            return 2

    install_tenant_guard()
    install_rls_context()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    async def run() -> list[RotationResult]:
        try:
            return await rotate_notification_keys(
                settings=settings,
                session_factory=session_factory,
                tenant_ids=tenant_ids,
                execute=args.execute,
            )
        finally:
            await engine.dispose()

    results = asyncio.run(run())
    for result in results:
        logger.info(
            "notification_key_rotation",
            extra={
                "context": {
                    "tenant_id": str(result.tenant_id),
                    "rotated": result.rotated,
                    "failed": result.failed,
                    "execute": args.execute,
                }
            },
        )
    return 1 if any(result.failed for result in results) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
